"""Telegram callback + text handlers.

Inline-keyboard taps land in `callback_handler`. Free-text replies in the
configured chat are routed through `text_message_handler`, which dispatches
to the per-user state machine for active edit / "other" reject flows.

State machine:
  bot_data["wire_pending_state"][user_id] = ("edit", draft_id, deadline_ts)
                                           | ("reject_other", draft_id, deadline_ts)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from difflib import SequenceMatcher

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from wire.config import ReposFile, WireConfig
from wire.db import session as db_session
from wire.db.models import Decision, Draft, Post, utc_now
from wire.db.models import Session as SessionRow
from wire.telegram import chat as chat_mod
from wire.telegram.voice import say
from wire.util.repo_names import display_name_for

log = structlog.get_logger()

EDIT_TIMEOUT_SECONDS = 600  # 10 minutes per spec


def _now_ts() -> float:
    return datetime.now(UTC).timestamp()


def _set_state(context: ContextTypes.DEFAULT_TYPE, user_id: int, kind: str, draft_id: int) -> None:
    states = context.bot_data.setdefault("wire_pending_state", {})
    states[user_id] = (kind, draft_id, _now_ts() + EDIT_TIMEOUT_SECONDS)


def _pop_state(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    states = context.bot_data.get("wire_pending_state") or {}
    return states.pop(user_id, None)


def _peek_state(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    states = context.bot_data.get("wire_pending_state") or {}
    state = states.get(user_id)
    if state is None:
        return None
    kind, draft_id, deadline = state
    if _now_ts() > deadline:
        states.pop(user_id, None)
        return None
    return state


# ---------------- callback router -------------------------------------------


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()  # ack the click immediately

    data = query.data
    parts = data.split(":", 2)
    action = parts[0]

    try:
        if action == "approve":
            await _on_approve(update, context, int(parts[1]))
        elif action == "reject":
            await _on_reject_open(update, context, int(parts[1]))
        elif action == "reject_reason":
            await _on_reject_reason(update, context, int(parts[1]), parts[2])
        elif action == "edit":
            await _on_edit_open(update, context, int(parts[1]))
        elif action == "save":
            await _on_save(update, context, int(parts[1]))
        else:
            log.warning("wire.telegram.unknown_callback", data=data)
    except Exception:
        log.exception("wire.telegram.callback_failed", data=data)


# ---------------- approve ----------------------------------------------------


async def _on_approve(update: Update, context: ContextTypes.DEFAULT_TYPE, draft_id: int) -> None:
    twitter = context.bot_data.get("wire_twitter")
    snap = _draft_snapshot(draft_id)
    if snap is None:
        await _reply(update, say("draft_not_found"))
        return
    text, original_text = snap
    was_revised = original_text is not None and original_text != text

    if twitter is None:
        await _reply(update, say("post_dry_run"))
        _finalize_decision(draft_id, was_revised, original_text, text)
        return

    try:
        result = await twitter.post(text)
    except Exception as e:
        log.exception("wire.twitter.post_failed", draft_id=draft_id, error=str(e))
        await _reply(update, say("post_failed", error=str(e)))
        return

    _finalize_decision(draft_id, was_revised, original_text, text)
    _record_post(draft_id, twitter_id=result.tweet_id, text=result.posted_text)

    url = getattr(result, "url", None)
    log.info(
        "wire.twitter.posted",
        draft_id=draft_id,
        tweet_id=str(result.tweet_id),
        url=url,
        was_revised=was_revised,
    )
    msg = say("post_success_with_url", url=url) if url else say("post_success_no_url")
    await _reply(update, msg)


# ---------------- chat-agent entry points -----------------------------------
#
# These wrap the inline-keyboard handlers above so the chat agent can drive
# them from natural language ("publish that draft", "kill #51 too internal",
# "make 52 shorter and ship it"). The chat agent calls these directly; the
# state machine is bypassed (no edit-state required).


async def approve_draft(update: Update, context: ContextTypes.DEFAULT_TYPE, draft_id: int) -> None:
    """Publish a pending draft to X — same code path as the ✅ Post button."""
    await _on_approve(update, context, draft_id)


async def reject_draft(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    draft_id: int,
    reason: str,
) -> None:
    """Reject a pending draft with a reason. Mirrors the keyboard's reject
    flow but skips the inline-button menu — the LLM has already extracted
    the reason from natural language."""
    if not _draft_exists(draft_id):
        await _reply(update, say("draft_not_found"))
        return
    cleaned = (reason or "via_chat").strip()[:200] or "via_chat"
    _record_decision(draft_id, decision="rejected", reject_reason=cleaned)
    _set_status(draft_id, "rejected")
    await _reply(update, say("rejected", reason=cleaned))


async def edit_draft_via_chat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    draft_id: int,
    instruction: str,
) -> None:
    """Apply an NL revision instruction to a draft without going through
    the ✏️ button + state-machine round-trip. Same revise → re-send flow
    as `_commit_edit`."""
    await _commit_edit(update, context, draft_id, instruction)


def _draft_exists(draft_id: int) -> bool:
    with db_session.session_scope() as sa:
        return sa.get(Draft, draft_id) is not None


def _finalize_decision(
    draft_id: int,
    was_revised: bool,
    original_text: str | None,
    final_text: str,
) -> None:
    """If the draft text was revised via the NL edit flow, record the
    decision as 'edited' (with diff) so the recent-decisions learning block
    sees the iteration. Otherwise record a plain 'approved'."""
    if was_revised and original_text is not None:
        diff_json = json.dumps(_diff_opcodes(original_text, final_text))
        _record_decision(
            draft_id,
            decision="edited",
            edited_text=final_text,
            edit_diff=diff_json,
        )
        _set_status(draft_id, "edited")
    else:
        _record_decision(draft_id, decision="approved")
        _set_status(draft_id, "approved")


# ---------------- reject -----------------------------------------------------


async def _on_reject_open(
    update: Update, context: ContextTypes.DEFAULT_TYPE, draft_id: int
) -> None:
    from wire.telegram.bot import reject_reason_keyboard

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=say("reject_prompt", draft_id=draft_id),
        reply_markup=reject_reason_keyboard(draft_id),
    )


async def _on_reject_reason(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    draft_id: int,
    reason_key: str,
) -> None:
    if reason_key == "other":
        # Switch to free-text reply mode.
        user = update.effective_user
        if user is not None:
            _set_state(context, user.id, "reject_other", draft_id)
        await _reply(update, say("reject_other_prompt"))
        return

    _record_decision(draft_id, decision="rejected", reject_reason=reason_key)
    _set_status(draft_id, "rejected")
    await _reply(update, say("rejected", reason=reason_key))


# ---------------- edit -------------------------------------------------------


async def _on_edit_open(update: Update, context: ContextTypes.DEFAULT_TYPE, draft_id: int) -> None:
    user = update.effective_user
    if user is not None:
        _set_state(context, user.id, "edit", draft_id)
    await _reply(update, say("edit_prompt"))


async def _on_save(update: Update, context: ContextTypes.DEFAULT_TYPE, draft_id: int) -> None:
    # Save = leave as pending. /saved lists them. expiry is handled by a
    # background sweep; nothing to do beyond an ack here.
    await _reply(update, say("saved"))


# ---------------- text message handler --------------------------------------


async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Free-text reply.

    Order of dispatch — do not reorder:
      1. State machine (edit / reject_other) wins. If the user just tapped
         ✏️ Edit, their next message is the revision instruction, not a
         conversation turn.
      2. Authorization check — only the configured chat is allowed past
         the state-machine gate.
      3. Chat agent: free-form conversation with bot-state context and
         action tools. See `wire.telegram.chat`.
    """
    user = update.effective_user
    if user is None or update.message is None or update.message.text is None:
        return
    text = update.message.text.strip()
    if not text:
        return

    # Pending state wins (edit revision or "other" reject reason).
    state = _peek_state(context, user.id)
    if state is not None:
        kind, draft_id, _deadline = state
        if kind == "edit":
            await _commit_edit(update, context, draft_id, text)
            return
        if kind == "reject_other":
            _pop_state(context, user.id)
            _record_decision(draft_id, decision="rejected", reject_reason=f"other:{text[:200]}")
            _set_status(draft_id, "rejected")
            await _reply(update, say("rejected_custom"))
            return

    # Auth check — same gate the slash commands use.
    expected_chat_id = context.bot_data.get("wire_chat_id")
    chat = update.effective_chat
    if chat is None or expected_chat_id is None or chat.id != expected_chat_id:
        return

    await chat_mod.handle_message(text, update, context)


async def _commit_edit(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    draft_id: int,
    instruction: str,
) -> None:
    """NL revision: the user describes what to change ('shorter',
    'drop the emoji'); the LLM rewrites the draft. The user iterates by
    tapping ✏️ Edit again. Approval is a separate button click — this
    function does NOT post to X."""
    from wire.drafting.drafter import revise_draft

    snap = _draft_snapshot(draft_id)
    if snap is None:
        # Pop state — no point holding an edit-state on a missing draft.
        user = update.effective_user
        if user is not None:
            _pop_state(context, user.id)
        await _reply(update, say("draft_not_found"))
        return
    current_text, _existing_original = snap

    repos: ReposFile | None = context.bot_data.get("wire_repos")
    repo_raw = _draft_repo(draft_id)
    repo_display = display_name_for(repo_raw, repos) if repo_raw else "?"

    cfg: WireConfig | None = context.bot_data.get("wire_config")
    provider = context.bot_data.get("wire_provider")
    if cfg is None or provider is None:
        # Provider not wired — this surface needs the LLM. Don't pop state
        # so the user can retry once it's restored.
        await _reply(update, say("edit_revision_failed", error="llm not wired"))
        return

    try:
        revised = await revise_draft(
            current_text,
            instruction,
            repo_display=repo_display,
            provider=provider,
        )
    except Exception as e:  # noqa: BLE001 — surface to user, keep edit-state alive
        log.warning("wire.telegram.revise_failed", draft_id=draft_id, error=str(e))
        await _reply(update, say("edit_revision_failed", error=str(e)))
        return

    # Lazy-fill original_text on first revision; replace working text.
    _apply_revision(draft_id, revised)

    # Edit successful → exit edit-state. The user taps ✏️ again on the
    # newly-sent draft to iterate further.
    user = update.effective_user
    if user is not None:
        _pop_state(context, user.id)

    await _reply(update, say("edit_revised"))

    # Re-send the draft so it appears with a fresh approve/edit/reject
    # keyboard. The previous message stays as conversation history.
    try:
        from wire.telegram.bot import send_draft

        if context.application is not None:
            await send_draft(context.application, draft_id)
    except Exception:
        log.exception("wire.telegram.revise_send_failed", draft_id=draft_id)


def _diff_opcodes(before: str, after: str) -> dict:
    sm = SequenceMatcher(a=before, b=after)
    ops = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        ops.append(
            {
                "tag": tag,
                "before": before[i1:i2],
                "after": after[j1:j2],
            }
        )
    return {"opcodes": ops, "ratio": sm.ratio(), "before_len": len(before), "after_len": len(after)}


# ---------------- DB helpers -------------------------------------------------


def _draft_text(draft_id: int) -> str | None:
    with db_session.session_scope() as sa:
        d = sa.get(Draft, draft_id)
        return d.text if d else None


def _draft_snapshot(draft_id: int) -> tuple[str, str | None] | None:
    """Return (current_text, original_text) for `draft_id`, or None if the
    draft is missing. `original_text` is NULL on drafts that have never
    been revised through the NL edit flow."""
    with db_session.session_scope() as sa:
        d = sa.get(Draft, draft_id)
        if d is None:
            return None
        return d.text, d.original_text


def _draft_repo(draft_id: int) -> str | None:
    """Lookup the repo string for the session that owns this draft."""
    with db_session.session_scope() as sa:
        d = sa.get(Draft, draft_id)
        if d is None or d.session_id is None:
            return None
        sess = sa.get(SessionRow, d.session_id)
        return sess.repo if sess else None


def _apply_revision(draft_id: int, revised: str) -> None:
    """Lazy-fill `original_text` on first revision, then replace `text`."""
    with db_session.session_scope() as sa:
        d = sa.get(Draft, draft_id)
        if d is None:
            return
        if d.original_text is None:
            d.original_text = d.text
        d.text = revised


def _set_status(draft_id: int, status: str) -> None:
    with db_session.session_scope() as sa:
        d = sa.get(Draft, draft_id)
        if d is not None:
            d.status = status


def _record_decision(
    draft_id: int,
    *,
    decision: str,
    reject_reason: str | None = None,
    edited_text: str | None = None,
    edit_diff: str | None = None,
) -> None:
    with db_session.session_scope() as sa:
        sa.add(
            Decision(
                draft_id=draft_id,
                decision=decision,
                reject_reason=reject_reason,
                edited_text=edited_text,
                edit_diff=edit_diff,
            )
        )


def _record_post(draft_id: int, *, twitter_id: str, text: str) -> None:
    with db_session.session_scope() as sa:
        sa.add(
            Post(
                draft_id=draft_id,
                twitter_id=str(twitter_id),
                text=text,
                posted_at=utc_now(),
            )
        )


async def _reply(update: Update, text: str) -> None:
    msg = update.effective_message
    if msg is not None:
        await msg.reply_text(text)


# ---------------- expiry sweep ----------------------------------------------


def expire_old_saved_drafts(*, max_age_hours: int = 24) -> int:
    """Background sweep: pending drafts older than max_age_hours flip to expired.
    Called from main.py's APScheduler.
    """
    from datetime import timedelta

    from sqlalchemy import select

    cutoff = utc_now() - timedelta(hours=max_age_hours)
    expired = 0
    with db_session.session_scope() as sa:
        rows = (
            sa.execute(
                select(Draft).where(Draft.status == "pending").where(Draft.created_at < cutoff)
            )
            .scalars()
            .all()
        )
        for d in rows:
            d.status = "expired"
            expired += 1
    if expired:
        log.info("wire.telegram.expired_saved", count=expired)
    return expired

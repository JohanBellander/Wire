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

from wire.db import session as db_session
from wire.db.models import Decision, Draft, Post, utc_now
from wire.telegram.voice import say

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
    text = _draft_text(draft_id)
    if text is None:
        await _reply(update, say("draft_not_found"))
        return

    if twitter is None:
        await _reply(update, say("post_dry_run"))
        _record_decision(draft_id, decision="approved")
        _set_status(draft_id, "approved")
        return

    try:
        result = await twitter.post(text)
    except Exception as e:
        log.exception("wire.twitter.post_failed", draft_id=draft_id, error=str(e))
        await _reply(update, say("post_failed", error=str(e)))
        return

    _record_decision(draft_id, decision="approved")
    _set_status(draft_id, "approved")
    _record_post(draft_id, twitter_id=result.tweet_id, text=result.posted_text)

    url = getattr(result, "url", None)
    msg = say("post_success_with_url", url=url) if url else say("post_success_no_url")
    await _reply(update, msg)


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
    """Free-text reply: dispatch to the right state, if any."""
    user = update.effective_user
    if user is None or update.message is None or update.message.text is None:
        return
    state = _peek_state(context, user.id)
    if state is None:
        return  # not in any waiting state — ignore

    kind, draft_id, _deadline = state
    text = update.message.text.strip()

    if kind == "edit":
        await _commit_edit(update, context, draft_id, text)
    elif kind == "reject_other":
        _pop_state(context, user.id)
        _record_decision(draft_id, decision="rejected", reject_reason=f"other:{text[:200]}")
        _set_status(draft_id, "rejected")
        await _reply(update, say("rejected_custom"))


async def _commit_edit(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    draft_id: int,
    edited_text: str,
) -> None:
    user = update.effective_user
    if user is not None:
        _pop_state(context, user.id)

    original = _draft_text(draft_id)
    if original is None:
        await _reply(update, say("draft_not_found"))
        return

    diff_json = json.dumps(_diff_opcodes(original, edited_text))
    twitter = context.bot_data.get("wire_twitter")
    if twitter is None:
        _record_decision(draft_id, decision="edited", edited_text=edited_text, edit_diff=diff_json)
        _set_status(draft_id, "edited")
        await _reply(update, say("edit_dry_run"))
        return

    try:
        result = await twitter.post(edited_text)
    except Exception as e:
        log.exception("wire.twitter.edit_post_failed", draft_id=draft_id, error=str(e))
        await _reply(update, say("edit_post_failed", error=str(e)))
        return

    _record_decision(draft_id, decision="edited", edited_text=edited_text, edit_diff=diff_json)
    _set_status(draft_id, "edited")
    _record_post(draft_id, twitter_id=result.tweet_id, text=result.posted_text)
    url = getattr(result, "url", None)
    msg = say("edit_success_with_url", url=url) if url else say("edit_success_no_url")
    await _reply(update, msg)


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

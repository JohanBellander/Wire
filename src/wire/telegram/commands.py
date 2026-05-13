"""Telegram slash commands. SPEC §7.4 lists the names; behavior here.

Commands:
  /status   bot health, last ingestion, queue size, current month spend
  /budget   spend vs cap
  /pause [hours]  pause drafting
  /resume   resume drafting
  /saved    list pending (saved) drafts
  /digest   force-send the weekly digest now
  /repos    list current allowlist (read-only)
  /extend [usd]   raise the monthly cap by N USD (default 5)
  /help     short cheat-sheet
"""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog
from sqlalchemy import desc, select
from sqlalchemy.orm import Session as SASession
from telegram import Update
from telegram.ext import ContextTypes

from wire.config import ReposFile, WireConfig
from wire.db import session as db_session
from wire.db.models import BotState, Draft, Event, Session, utc_now
from wire.drafting.drafter import (
    BudgetPausedError,
    EventNotFoundError,
    force_draft_for_event,
)
from wire.events.format import format_event_message
from wire.health import get_state as get_health_state
from wire.llm.budget import compute_fallback_stats, compute_status, record_extension
from wire.telegram.voice import say
from wire.util.repo_names import display_name_for

log = structlog.get_logger()


def _is_authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Only respond in the configured chat — defence-in-depth in case the bot
    is added to other chats by accident."""
    expected = context.bot_data.get("wire_chat_id")
    chat = update.effective_chat
    return chat is not None and expected is not None and chat.id == expected


async def _reply(update: Update, text: str) -> None:
    msg = update.effective_message
    if msg is not None:
        await msg.reply_text(text)


# ---------------- bot state helpers ----------------------------------------


def _get_state(key: str) -> str | None:
    with db_session.session_scope() as sa:
        row = sa.get(BotState, key)
        return row.value if row else None


def _set_state(key: str, value: str) -> None:
    with db_session.session_scope() as sa:
        row = sa.get(BotState, key)
        if row is None:
            sa.add(BotState(key=key, value=value))
        else:
            row.value = value
            row.updated_at = utc_now()


def is_drafting_paused() -> tuple[bool, datetime | None]:
    """Returns (paused, paused_until_dt). paused_until None = indefinite pause."""
    until_str = _get_state("paused_until")
    if until_str is None:
        return False, None
    if until_str == "":
        return True, None
    try:
        until = datetime.fromisoformat(until_str)
    except ValueError:
        return False, None
    if until <= utc_now():
        return False, until
    return True, until


# ---------------- commands -------------------------------------------------


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, context):
        return
    cfg: WireConfig = context.bot_data["wire_config"]
    health = get_health_state()
    paused, until = is_drafting_paused()
    pause_line = "no"
    if paused:
        pause_line = f"yes (until {until.isoformat()})" if until else "yes (indefinite)"

    with db_session.session_scope() as sa:
        spend = compute_status(sa, cfg.llm.monthly_budget_usd, cfg.llm.budget_alert_threshold)
        fb = compute_fallback_stats(sa, hours=24)

    text = (
        f"{say('status_header')}\n"
        f"version: {health.version}\n"
        f"last_ingestion_at: {health.last_ingestion_at or 'never'}\n"
        f"pending drafts: {health.queue_size}\n"
        f"paused: {pause_line}\n"
        f"month spend: ${spend.spend_usd:.2f} / ${spend.cap_usd:.2f} ({spend.pct * 100:.1f}%)\n"
        "\n"
        f"{_format_brain(cfg, health, fb)}"
    )
    await _reply(update, text)


def _format_brain(cfg: WireConfig, health, fb) -> str:
    """Render the LLM-backend status block for /status. Shape mirrors
    Helmsman's: primary, fallback, last used, fallback rate over the window."""
    header = say("brain_header")
    if cfg.llm.provider == "claude":
        primary_label = f"claude (drafting={cfg.llm.claude.drafting})"
        return f"{header}\nprimary:  {primary_label}\nfallback: (none — claude only)"
    # provider == llamacpp → fallback to claude
    assert cfg.llm.llamacpp is not None
    primary_label = f"llamacpp ({cfg.llm.llamacpp.model})"
    fallback_label = f"claude ({cfg.llm.claude.drafting} / {cfg.llm.claude.triage})"
    last_used = health.last_used_provider or "(no calls yet)"
    rate_pct = fb.fallback_rate * 100
    rate_line = (
        f"fallback rate ({fb.window_hours}h): "
        f"{rate_pct:.0f}% ({fb.fallback_count} / {fb.total_calls})"
        if fb.total_calls > 0
        else f"fallback rate ({fb.window_hours}h): no LLM calls yet"
    )
    return (
        f"{header}\n"
        f"primary:  {primary_label}\n"
        f"fallback: {fallback_label}\n"
        f"last used: {last_used}\n"
        f"{rate_line}"
    )


async def budget_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, context):
        return
    cfg: WireConfig = context.bot_data["wire_config"]
    with db_session.session_scope() as sa:
        s = compute_status(sa, cfg.llm.monthly_budget_usd, cfg.llm.budget_alert_threshold)
    text = (
        f"{say('budget_header', month=s.month)}\n"
        f"spend:     ${s.spend_usd:.2f}\n"
        f"cap:       ${s.cap_usd:.2f}  (base ${cfg.llm.monthly_budget_usd:.2f}"
        f" + extensions ${s.extension_usd:.2f})\n"
        f"pct:       {s.pct * 100:.1f}%\n"
        f"remaining: ${s.remaining_usd:.2f}\n"
        f"status:    {'PAUSED' if s.paused else ('WARN' if s.warning else 'ok')}"
    )
    await _reply(update, text)


async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, context):
        return
    args = context.args or []
    until_iso = ""
    if args:
        try:
            hours = float(args[0])
        except ValueError:
            await _reply(update, say("pause_usage"))
            return
        until = utc_now() + timedelta(hours=hours)
        until_iso = until.isoformat()
        _set_state("paused_until", until_iso)
        await _reply(update, say("paused_until", until=until.isoformat()))
    else:
        _set_state("paused_until", "")
        await _reply(update, say("paused_indefinite"))


async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, context):
        return
    _set_state("paused_until", "")  # transition through indefinite pause then clear
    with db_session.session_scope() as sa:
        row = sa.get(BotState, "paused_until")
        if row is not None:
            sa.delete(row)
    await _reply(update, say("resumed"))


async def saved_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, context):
        return
    with db_session.session_scope() as sa:
        rows = (
            sa.execute(
                select(Draft).where(Draft.status == "pending").order_by(Draft.created_at.desc())
            )
            .scalars()
            .all()
        )
    if not rows:
        await _reply(update, say("saved_empty"))
        return
    lines = [say("saved_header")]
    for d in rows[:30]:
        snippet = (d.text or "")[:80].replace("\n", " ")
        lines.append(f'#{d.id} {d.created_at.date()}  "{snippet}"')
    await _reply(update, "\n".join(lines))


async def digest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, context):
        return
    builder = context.bot_data.get("wire_digest_builder")
    if builder is None:
        await _reply(update, say("digest_not_wired"))
        return
    text = await builder.build_text()
    await context.bot.send_message(chat_id=context.bot_data["wire_chat_id"], text=text)


async def repos_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, context):
        return
    repos: ReposFile = context.bot_data["wire_repos"]
    if not repos.repos:
        await _reply(update, say("repos_empty"))
        return
    lines = [say("repos_header")]
    for r in repos.repos:
        display = display_name_for(r.name, repos)
        lines.append(f"- {display}  ({r.visibility})  {r.notes}")
    await _reply(update, "\n".join(lines))


async def extend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, context):
        return
    args = context.args or []
    amount = 5.0
    if args:
        try:
            amount = float(args[0])
        except ValueError:
            await _reply(update, say("extend_usage"))
            return
    if amount <= 0:
        await _reply(update, say("extend_non_positive"))
        return
    with db_session.session_scope() as sa:
        record_extension(sa, amount, reason=f"telegram /extend by user {update.effective_user.id}")
    cfg: WireConfig = context.bot_data["wire_config"]
    with db_session.session_scope() as sa:
        s = compute_status(sa, cfg.llm.monthly_budget_usd, cfg.llm.budget_alert_threshold)
    await _reply(
        update,
        say(
            "budget_extended",
            amount=amount,
            cap=s.cap_usd,
            spend=s.spend_usd,
            pct=s.pct * 100,
        ),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, context):
        return
    await _reply(update, say("help_text"))


# ---------------- /last + /draft -------------------------------------------


def _outcome_for_event(sa: SASession, event: Event) -> str:
    """Return the per-event outcome string for /last.

    Precedence (matches docs/feature-last-draft-commands.md):
      1. drafted #N (status)         — session has any Draft
      2. LLM said skip: <reason>     — drafted_at set, no drafts, skip_reason known
      3. below-threshold skip        — drafted_at set, no drafts, no skip_reason
      4. pending session close       — session.ended_at IS NULL
      5. no session                  — event.session_id IS NULL
    """
    if event.session_id is None:
        return "no session"
    sess = sa.get(Session, event.session_id)
    if sess is None:
        return "no session"
    latest_draft = sa.execute(
        select(Draft).where(Draft.session_id == sess.id).order_by(desc(Draft.created_at)).limit(1)
    ).scalar_one_or_none()
    if latest_draft is not None:
        return f"drafted #{latest_draft.id} ({latest_draft.status})"
    if sess.drafted_at is not None:
        if sess.skip_reason:
            return f"LLM said skip: {sess.skip_reason}"
        return "below-threshold skip"
    if sess.ended_at is None:
        return "pending session close"
    # Session is closed but drafting hasn't run yet on it — still pending.
    return "pending session close"


async def last_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, context):
        return
    args = context.args or []
    n = 5
    if args:
        try:
            n = int(args[0])
        except ValueError:
            await _reply(update, say("last_usage"))
            return
    n = max(1, min(n, 50))

    repos: ReposFile | None = context.bot_data.get("wire_repos")
    with db_session.session_scope() as sa:
        events = (
            sa.execute(select(Event).order_by(desc(Event.occurred_at)).limit(n)).scalars().all()
        )
        if not events:
            await _reply(update, say("last_empty"))
            return
        lines = [say("last_header", count=len(events))]
        for e in events:
            score = f"{e.triage_score:.2f}" if e.triage_score is not None else "?"
            msg = format_event_message(e)
            snippet = f' "{msg[:60]}"' if msg else ""
            outcome = _outcome_for_event(sa, e)
            display = display_name_for(e.repo, repos)
            lines.append(f"[{e.id}] {display}/{e.event_type}{snippet} triage={score} → {outcome}")
    await _reply(update, "\n".join(lines))


async def draft_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, context):
        return
    args = context.args or []
    if not args:
        await _reply(update, say("draft_usage"))
        return
    try:
        event_id = int(args[0])
    except ValueError:
        await _reply(update, say("draft_usage_int"))
        return

    cfg: WireConfig = context.bot_data["wire_config"]
    repos: ReposFile = context.bot_data["wire_repos"]
    provider = context.bot_data.get("wire_provider")
    if provider is None:
        await _reply(update, say("provider_not_wired"))
        return

    try:
        draft_id, skip_reason = await force_draft_for_event(event_id, cfg, repos, provider)
    except EventNotFoundError:
        await _reply(update, say("event_not_found", event_id=event_id))
        return
    except BudgetPausedError as e:
        await _reply(update, say("budget_blocked", detail=str(e)))
        return
    except Exception as e:  # noqa: BLE001 — surface unexpected failures to the user
        log.exception("wire.telegram.draft_cmd_failed", event_id=event_id)
        await _reply(update, say("force_failed", error_type=type(e).__name__, error=str(e)))
        return

    if draft_id is None:
        reason = skip_reason or "(no reason given)"
        await _reply(update, say("force_skip_reason", reason=reason))
        return

    # Fire the standard send_draft path so the approval keyboard appears.
    from wire.telegram.bot import send_draft

    try:
        await send_draft(context.application, draft_id)
    except Exception:
        log.exception("wire.telegram.draft_cmd_send_failed", draft_id=draft_id)
        await _reply(update, say("force_send_failed", draft_id=draft_id))
        return
    await _reply(update, say("force_success", draft_id=draft_id, event_id=event_id))

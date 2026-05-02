"""Fresh bot-state snapshot for the chat agent.

The chat agent sees this on every turn and narrates from it. Aim for
roughly 300-600 tokens — enough that "show me the last events" /
"what's pending" / "any rejections lately" can be answered without a
tool call, while staying small enough that it caches nothing.
"""

from __future__ import annotations

import structlog
from sqlalchemy import desc, select

from wire.config import ReposFile, WireConfig
from wire.db import session as db_session
from wire.db.models import BotState, Decision, Draft, Event, Session
from wire.events.format import format_event_message
from wire.health import get_state as get_health_state
from wire.llm.budget import compute_status
from wire.util.repo_names import display_name_for

log = structlog.get_logger()

# How much of each list to include. Keep tight — the snapshot recomputes
# every turn and lives in the user message (uncached).
RECENT_EVENTS_N = 10
SAVED_DRAFTS_N = 5
RECENT_DECISIONS_N = 5


def build_state_snapshot(cfg: WireConfig, repos: ReposFile | None) -> str:
    """Compose the snapshot block. Returns a multi-section plain-text
    string. Sections are omitted (not just empty) when there's nothing to
    show — the LLM doesn't need an empty header."""
    lines: list[str] = ["─── bot state ───"]

    # paused state — lives in BotState.
    paused_line = "no"
    with db_session.session_scope() as sa:
        row = sa.get(BotState, "paused_until")
        if row is not None:
            paused_line = f"yes ({row.value})" if row.value else "yes (indefinite)"
    lines.append(f"paused: {paused_line}")

    # spend + health
    health = get_health_state()
    with db_session.session_scope() as sa:
        spend = compute_status(sa, cfg.llm.monthly_budget_usd, cfg.llm.budget_alert_threshold)
    lines.append(
        f"month spend: ${spend.spend_usd:.2f} / ${spend.cap_usd:.2f} ({spend.pct * 100:.1f}%)"
    )
    lines.append(f"last ingestion: {health.last_ingestion_at or 'never'}")
    lines.append(f"pending drafts in queue: {health.queue_size}")

    # recent events with outcome
    events_block = _events_section(repos)
    if events_block:
        lines.append("")
        lines.append(events_block)

    # saved drafts (status=pending)
    saved_block = _saved_drafts_section()
    if saved_block:
        lines.append("")
        lines.append(saved_block)

    # recent decisions
    decisions_block = _recent_decisions_section()
    if decisions_block:
        lines.append("")
        lines.append(decisions_block)

    # allowlisted repos (helps the LLM answer "what repos are you watching")
    if repos is not None and repos.repos:
        lines.append("")
        lines.append("─── allowlisted repos ───")
        for r in repos.repos:
            display = display_name_for(r.name, repos)
            note = f" — {r.notes}" if r.notes else ""
            lines.append(f"- {display} ({r.visibility}){note}")

    return "\n".join(lines)


def _events_section(repos: ReposFile | None) -> str | None:
    with db_session.session_scope() as sa:
        events = (
            sa.execute(select(Event).order_by(desc(Event.occurred_at)).limit(RECENT_EVENTS_N))
            .scalars()
            .all()
        )
        if not events:
            return None
        out = [f"─── recent events (last {len(events)}) ───"]
        for e in events:
            score = f"{e.triage_score:.2f}" if e.triage_score is not None else "?"
            msg = format_event_message(e) or ""
            snippet = f' "{msg[:60]}"' if msg else ""
            outcome = _outcome_for_event(sa, e)
            display = display_name_for(e.repo, repos)
            out.append(f"[{e.id}] {display}/{e.event_type}{snippet} triage={score} → {outcome}")
    return "\n".join(out)


def _saved_drafts_section() -> str | None:
    with db_session.session_scope() as sa:
        rows = (
            sa.execute(
                select(Draft)
                .where(Draft.status == "pending")
                .order_by(desc(Draft.created_at))
                .limit(SAVED_DRAFTS_N)
            )
            .scalars()
            .all()
        )
        if not rows:
            return None
        out = [f"─── pending drafts (last {len(rows)}) ───"]
        for d in rows:
            snippet = (d.text or "")[:80].replace("\n", " ")
            out.append(f'#{d.id} {d.created_at.date()} "{snippet}"')
    return "\n".join(out)


def _recent_decisions_section() -> str | None:
    with db_session.session_scope() as sa:
        rows = (
            sa.execute(
                select(Decision).order_by(desc(Decision.decided_at)).limit(RECENT_DECISIONS_N)
            )
            .scalars()
            .all()
        )
        if not rows:
            return None
        out = [f"─── recent decisions (last {len(rows)}) ───"]
        for d in rows:
            draft_text = ""
            if d.draft is not None:
                draft_text = (d.draft.original_text or d.draft.text or "")[:80]
            if d.decision == "approved":
                out.append(f'✅ APPROVED: "{draft_text}"')
            elif d.decision == "rejected":
                out.append(f'❌ REJECTED: "{draft_text}" — reason: "{d.reject_reason or "?"}"')
            elif d.decision == "edited":
                edited = (d.edited_text or "")[:80]
                out.append(f'✏️ EDITED: "{draft_text}" → "{edited}"')
    return "\n".join(out)


def _outcome_for_event(sa, event: Event) -> str:
    """Mirrors `commands._outcome_for_event` — duplicated here to avoid
    a telegram-internal circular import. Keep behavior in sync."""
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
    return "pending session close"

"""Drafting orchestration. Per SPEC.MD §7.3 + §8.

For each closed, undrafted session:
  1. Skip if all events triage_score < 0.3 (no LLM call).
  2. Skip if currently in quiet hours (defer LLM call until after quiet).
  3. Build the cached/uncached prompt blocks.
  4. Call the drafting LLM with structured output (DraftResponse).
  5. Persist drafts; mark session.drafted_at.

Already-existing drafts that were generated before quiet hours are sent the
moment quiet hours end — that's the Telegram bot's responsibility, not the
drafter's.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import time as dt_time
from pathlib import Path

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import asc, desc, select

from wire.config import QuietHoursConfig, ReposFile, WireConfig
from wire.db import session as db_session
from wire.db.models import (
    Decision,
    Draft,
    Event,
    Metric,
    Post,
    Session,
    VoiceProfile,
    utc_now,
)
from wire.llm.alerts import is_drafting_blocked_by_budget
from wire.llm.budget import log_llm_call as _log_llm_call
from wire.llm.caching import text_block
from wire.llm.provider import LLMError, LLMProvider, parse_json_lenient

log = structlog.get_logger()

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "llm" / "prompts"


def _system_prompt() -> str:
    return (PROMPTS_DIR / "drafting.txt").read_text(encoding="utf-8")


# --- pydantic schemas --------------------------------------------------------


class DraftItem(BaseModel):
    text: str = Field(min_length=1)
    reasoning: str = ""
    confidence: float = Field(ge=0.0, le=1.0)


class DraftResponse(BaseModel):
    skip_reason: str | None = None
    drafts: list[DraftItem] = Field(default_factory=list)


# --- quiet hours -------------------------------------------------------------


def is_in_quiet_hours(qh: QuietHoursConfig, *, now: datetime | None = None) -> bool:
    if now is None:
        now = datetime.now(UTC)
    local = (
        now.astimezone(qh.tzinfo) if now.tzinfo else now.replace(tzinfo=UTC).astimezone(qh.tzinfo)
    )
    t = local.time()
    return _between(t, qh.start, qh.end)


def _between(t: dt_time, start: dt_time, end: dt_time) -> bool:
    """Time-of-day range, supports wrap-around midnight."""
    if start <= end:
        return start <= t < end
    # wraps midnight: e.g. start=22:00, end=07:00 → in-range is t>=22 OR t<07
    return t >= start or t < end


# --- prompt assembly ---------------------------------------------------------


@dataclass
class PromptBlocks:
    system_blocks: list[dict]  # for the system parameter, with cache_control
    user_message: str  # the variable session-events block


def _format_event_line(e: Event) -> str:
    raw = (e.payload or {}).get("raw_payload", {})
    score = f"{e.triage_score:.2f}" if e.triage_score is not None else "?"
    bits = [f"[{score}]", e.event_type, f"by={e.actor or '?'}", f"at={e.occurred_at.isoformat()}"]
    if e.event_type == "PushEvent":
        commits = raw.get("commits") or []
        if commits:
            first = commits[0].get("message", "").splitlines()[0][:140]
            bits.append(
                f'"{first}" (+{len(commits) - 1} more)' if len(commits) > 1 else f'"{first}"'
            )
    elif e.event_type == "PullRequestEvent":
        pr = raw.get("pull_request") or {}
        title = (pr.get("title") or "")[:140]
        bits.append(f'PR "{title}" merged={pr.get("merged")}')
    elif e.event_type == "ReleaseEvent":
        rel = raw.get("release") or {}
        bits.append(f'Release {rel.get("tag_name", "")} "{(rel.get("name") or "")[:80]}"')
    elif e.event_type == "IssuesEvent":
        issue = raw.get("issue") or {}
        bits.append(f'Issue "{(issue.get("title") or "")[:140]}" {raw.get("action")}')
    elif e.event_type == "CreateEvent":
        bits.append(f'Create {raw.get("ref_type")} "{raw.get("ref")}"')
    elif e.event_type == "DeleteEvent":
        bits.append(f'Delete {raw.get("ref_type")} "{raw.get("ref")}"')
    elif e.event_type == "IssueCommentEvent":
        issue = raw.get("issue") or {}
        comment = raw.get("comment") or {}
        bits.append(
            f'Comment on "{(issue.get("title") or "")[:80]}": {(comment.get("body") or "")[:140]}'
        )
    if e.triage_reason:
        bits.append(f'reason="{e.triage_reason}"')
    return " ".join(bits)


def _format_session_events(session: Session, repo_notes: str, repo_visibility: str) -> str:
    duration = ""
    if session.ended_at and session.started_at:
        d = session.ended_at - session.started_at
        duration = f"{d.total_seconds() / 60:.0f} min"
    lines = [
        "─── Session events ───",
        f"Repo: {session.repo} ({repo_visibility})",
        f"Repo notes: {repo_notes}",
        f"Session duration: {duration}",
        f"Closed reason: {session.closed_reason or 'open'}",
        "Events:",
    ]
    for e in sorted(session.events, key=lambda x: x.occurred_at):
        lines.append(f"  - {_format_event_line(e)}")
    lines.append("")
    lines.append("─── Task ───")
    lines.append(
        "Draft 0–3 posts about this session, or return skip_reason if nothing here "
        "warrants a post. Each draft must reference only events from the session above. "
        "Return JSON matching the DraftResponse schema."
    )
    return "\n".join(lines)


def _voice_profile_text(sa) -> str:
    row = sa.execute(
        select(VoiceProfile).order_by(VoiceProfile.generated_at.desc()).limit(1)
    ).scalar_one_or_none()
    if row is None:
        return "(no voice profile yet — match the user's matter-of-fact, lowercase developer tone)"
    return row.profile_text


def _recent_settled_posts(sa, n: int, settle_days: int) -> str:
    """Posts ≥ settle_days old, with the latest metric snapshot inline."""
    # Most recent N posts older than settle_days.
    from datetime import timedelta as _td

    cutoff_dt = utc_now() - _td(days=settle_days)
    rows = (
        sa.execute(
            select(Post).where(Post.posted_at <= cutoff_dt).order_by(desc(Post.posted_at)).limit(n)
        )
        .scalars()
        .all()
    )
    if not rows:
        return "(no settled posts yet)"
    lines = []
    for p in rows:
        latest = sa.execute(
            select(Metric).where(Metric.post_id == p.id).order_by(desc(Metric.fetched_at)).limit(1)
        ).scalar_one_or_none()
        m = (
            f"impr={latest.impressions or 0} likes={latest.likes or 0} "
            f"rt={latest.retweets or 0} rep={latest.replies or 0}"
            if latest
            else "metrics=none"
        )
        lines.append(f'- [{p.posted_at.date()}] "{p.text[:200]}"  ({m})')
    return "\n".join(lines)


def _recent_decisions(sa, n: int) -> str:
    rows = sa.execute(select(Decision).order_by(desc(Decision.decided_at)).limit(n)).scalars().all()
    if not rows:
        return "(no decisions yet)"
    lines = []
    for d in rows:
        draft_text = ""
        if d.draft is not None:
            draft_text = d.draft.text[:200]
        if d.decision == "approved":
            lines.append(f'✅ APPROVED: "{draft_text}"')
        elif d.decision == "rejected":
            lines.append(f'❌ REJECTED: "{draft_text}" — reason: "{d.reject_reason or "?"}"')
        elif d.decision == "edited":
            edited = (d.edited_text or "")[:200]
            lines.append(f'✏️ EDITED: "{draft_text}" → "{edited}"')
    return "\n".join(lines)


def _median_metric_summary(sa) -> str:
    # Quick rolling-30-day medians for impressions and likes.
    from datetime import timedelta as _td

    cutoff = utc_now() - _td(days=30)
    rows = sa.execute(select(Post.id).where(Post.posted_at >= cutoff)).scalars().all()
    if not rows:
        return "no recent posts"
    impressions = []
    likes = []
    for pid in rows:
        latest = sa.execute(
            select(Metric).where(Metric.post_id == pid).order_by(desc(Metric.fetched_at)).limit(1)
        ).scalar_one_or_none()
        if latest is None:
            continue
        if latest.impressions is not None:
            impressions.append(latest.impressions)
        if latest.likes is not None:
            likes.append(latest.likes)

    def med(xs: list[int]) -> str:
        if not xs:
            return "?"
        xs_sorted = sorted(xs)
        n = len(xs_sorted)
        return str(xs_sorted[n // 2])

    return f"~{med(impressions)} impressions, {med(likes)} likes"


def build_prompt_blocks(
    session: Session,
    config: WireConfig,
    repos_file: ReposFile,
) -> PromptBlocks:
    """Assemble the cached + variable blocks for one drafting call.

    Cache strategy (SPEC §8):
      Block 1 (1h): system + voice profile
      Block 2 (5m): recent posts + recent decisions + metric summary
      Block 3 (variable, never cached): session events
    """
    repo_entry = repos_file.get(session.repo)
    repo_notes = repo_entry.notes if repo_entry else ""
    repo_visibility = repo_entry.visibility if repo_entry else "?"

    with db_session.session_scope() as sa:
        voice = _voice_profile_text(sa)
        recent_posts = _recent_settled_posts(
            sa, n=config.learning.recent_posts_n, settle_days=config.metrics.posts_settle_days
        )
        recent_decisions = _recent_decisions(sa, n=config.learning.recent_decisions_n)
        median_summary = _median_metric_summary(sa)

    system_text = _system_prompt() + "\n\nVoice profile:\n" + voice

    learning_text = "\n".join(
        [
            "─── Recent posts (last 30, settled ≥ 7d, with performance) ───",
            recent_posts,
            "",
            f"Reference: trailing 30-day median is {median_summary}.",
            "",
            "─── Recent decisions (last 20) ───",
            recent_decisions,
        ]
    )

    cached_caching = config.llm.prompt_caching
    system_blocks = [
        text_block(system_text, cache_ttl="1h" if cached_caching else None),
        text_block(learning_text, cache_ttl="5m" if cached_caching else None),
    ]

    user_message = _format_session_events(session, repo_notes, repo_visibility)

    return PromptBlocks(system_blocks=system_blocks, user_message=user_message)


# --- DB helpers --------------------------------------------------------------


def _closed_undrafted_sessions(min_score: float) -> list[int]:
    with db_session.session_scope() as sa:
        rows = (
            sa.execute(
                select(Session)
                .where(Session.closed_reason.is_not(None))
                .where(Session.drafted_at.is_(None))
                .order_by(asc(Session.ended_at))
            )
            .scalars()
            .all()
        )
        return [s.id for s in rows]


def _all_events_below_threshold(session_id: int, threshold: float) -> bool:
    with db_session.session_scope() as sa:
        events = sa.execute(select(Event).where(Event.session_id == session_id)).scalars().all()
        if not events:
            return True
        return all((e.triage_score or 0.0) < threshold for e in events)


# --- main entrypoint ---------------------------------------------------------


@dataclass
class DraftingResult:
    session_id: int
    drafts_created: int
    skip_reason: str | None
    deferred_quiet_hours: bool


async def draft_pending_sessions(
    config: WireConfig,
    repos_file: ReposFile,
    provider: LLMProvider,
    *,
    triage_threshold: float = 0.3,
    now: datetime | None = None,
) -> list[DraftingResult]:
    results: list[DraftingResult] = []

    if is_in_quiet_hours(config.quiet_hours, now=now):
        log.info("wire.drafting.quiet_hours_defer")
        for sid in _closed_undrafted_sessions(min_score=triage_threshold):
            results.append(DraftingResult(sid, 0, None, deferred_quiet_hours=True))
        return results

    if is_drafting_blocked_by_budget(config):
        log.info("wire.drafting.budget_paused")
        return results

    for sid in _closed_undrafted_sessions(min_score=triage_threshold):
        if _all_events_below_threshold(sid, triage_threshold):
            with db_session.session_scope() as sa:
                s = sa.get(Session, sid)
                if s is not None and s.drafted_at is None:
                    s.drafted_at = utc_now()
            log.info("wire.drafting.below_threshold_skip", session_id=sid)
            results.append(DraftingResult(sid, 0, "below_threshold", False))
            continue

        # Load session with events for prompt assembly.
        with db_session.session_scope() as sa:
            session_obj = sa.get(Session, sid)
            if session_obj is None:
                continue
            # Eager-load events so we can pass session_obj outside the txn.
            _ = list(session_obj.events)
            sa.expunge_all()

        blocks = build_prompt_blocks(session_obj, config, repos_file)

        try:
            resp = await provider.complete(
                task="drafting",
                system=blocks.system_blocks,
                messages=[{"role": "user", "content": blocks.user_message}],
                response_format=DraftResponse,
                max_tokens=1500,
            )
        except LLMError as e:
            log.warning("wire.drafting.llm_failed", session_id=sid, error=str(e))
            continue

        _log_llm_call(resp)

        try:
            parsed = DraftResponse.model_validate(parse_json_lenient(resp.content))
        except Exception as e:
            log.warning("wire.drafting.bad_output", session_id=sid, error=str(e))
            continue

        with db_session.session_scope() as sa:
            s = sa.get(Session, sid)
            if s is None:
                continue
            s.drafted_at = utc_now()
            for item in parsed.drafts:
                sa.add(
                    Draft(
                        session_id=sid,
                        text=item.text,
                        reasoning=item.reasoning,
                    )
                )

        results.append(
            DraftingResult(
                session_id=sid,
                drafts_created=len(parsed.drafts),
                skip_reason=parsed.skip_reason,
                deferred_quiet_hours=False,
            )
        )
        log.info(
            "wire.drafting.session_done",
            session_id=sid,
            drafts=len(parsed.drafts),
            skip_reason=parsed.skip_reason,
        )

    return results

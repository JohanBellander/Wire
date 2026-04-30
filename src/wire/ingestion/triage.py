"""Per-event triage: cheap LLM call (Haiku) that scores 0..1 + brief reason.

Stored on the event row (triage_score, triage_reason). Per the plan
clarification, this is a per-event call, not batched.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import select

from wire.db import session as db_session
from wire.db.models import Event, LLMCall
from wire.llm.provider import LLMError, LLMProvider, parse_json_lenient

log = structlog.get_logger()

PROMPT_PATH = Path(__file__).resolve().parents[1] / "llm" / "prompts" / "triage.txt"


def _system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


class TriageResponse(BaseModel):
    score: float = Field(ge=0.0, le=1.0)
    reason: str = Field(max_length=200)


def _summarize_event(event: Event) -> str:
    """Compact representation for the user message."""
    payload = event.payload or {}
    raw = payload.get("raw_payload") or {}
    parts = [
        f"repo: {event.repo}",
        f"type: {event.event_type}",
        f"actor: {event.actor or '?'}",
        f"occurred_at: {event.occurred_at.isoformat()}",
    ]
    if event.event_type == "PushEvent":
        commits = raw.get("commits") or []
        parts.append(f"commit_count: {len(commits)}")
        for c in commits[:5]:
            parts.append(f"  - {c.get('message', '').splitlines()[0][:140]}")
    elif event.event_type == "PullRequestEvent":
        pr = raw.get("pull_request") or {}
        parts.append(f"action: {raw.get('action')}")
        parts.append(f"merged: {pr.get('merged')}")
        parts.append(f"title: {pr.get('title', '')[:140]}")
    elif event.event_type == "ReleaseEvent":
        rel = raw.get("release") or {}
        parts.append(f"action: {raw.get('action')}")
        parts.append(f"name: {rel.get('name', '')[:140]}")
        parts.append(f"tag: {rel.get('tag_name', '')}")
    elif event.event_type == "IssuesEvent":
        issue = raw.get("issue") or {}
        parts.append(f"action: {raw.get('action')}")
        parts.append(f"title: {issue.get('title', '')[:140]}")
    elif event.event_type == "CreateEvent":
        # ref_type ∈ {branch, tag, repository}; very different signal levels
        parts.append(f"ref_type: {raw.get('ref_type')}")
        parts.append(f"ref: {raw.get('ref')}")
        if raw.get("description"):
            parts.append(f"description: {raw.get('description', '')[:140]}")
    elif event.event_type == "DeleteEvent":
        parts.append(f"ref_type: {raw.get('ref_type')}")
        parts.append(f"ref: {raw.get('ref')}")
    elif event.event_type == "IssueCommentEvent":
        issue = raw.get("issue") or {}
        comment = raw.get("comment") or {}
        parts.append(f"issue_title: {issue.get('title', '')[:120]}")
        parts.append(f"comment: {comment.get('body', '')[:200]}")
    return "\n".join(parts)


@dataclass
class TriageResult:
    event_id: int
    score: float
    reason: str
    fallback_used: bool


async def triage_event(event: Event, provider: LLMProvider) -> TriageResult:
    user_msg = _summarize_event(event)
    resp = await provider.complete(
        task="triage",
        system=_system_prompt(),
        messages=[{"role": "user", "content": user_msg}],
        response_format=TriageResponse,
        max_tokens=120,
    )
    parsed = TriageResponse.model_validate(parse_json_lenient(resp.content))
    return TriageResult(
        event_id=event.id,
        score=parsed.score,
        reason=parsed.reason,
        fallback_used=resp.fallback_used,
    )


async def triage_pending_events(provider: LLMProvider) -> int:
    """Score every event with triage_score IS NULL. Returns the count scored."""
    with db_session.session_scope() as sa:
        rows = list(sa.execute(
            select(Event).where(Event.triage_score.is_(None)).order_by(Event.occurred_at.asc())
        ).scalars())

    scored = 0
    for e in rows:
        try:
            result = await triage_event(e, provider)
        except LLMError as err:
            log.warning("wire.triage.failed", event_id=e.id, error=str(err))
            continue

        with db_session.session_scope() as sa:
            row = sa.get(Event, e.id)
            if row is None:
                continue
            row.triage_score = result.score
            row.triage_reason = result.reason

        # Log the LLM call too — bookkeeping shared with budget tracking.
        # (Real provider returns LLMResponse with cost; here we only have score
        # back. The provider already logged latency/cost internally is a future
        # refinement; for now the triage path is best-effort.)
        scored += 1
    return scored


def log_llm_call(
    *,
    task: str,
    provider: str,
    model: str | None,
    fallback: bool,
    input_tokens: int | None,
    output_tokens: int | None,
    cost_usd: float | None,
    latency_ms: int | None,
) -> None:
    """Write one row to llm_calls. Used by every caller of provider.complete()."""
    with db_session.session_scope() as sa:
        sa.add(LLMCall(
            task=task,
            provider=provider,
            model=model,
            fallback=fallback,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
        ))

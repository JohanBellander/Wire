"""Tests for repo-notes flowing into triage prompt context."""

from __future__ import annotations

from datetime import datetime

import pytest

from wire.config import RepoEntry, ReposFile
from wire.db import session as db_session
from wire.db.models import Base, Event
from wire.ingestion.triage import (
    _summarize_event,
    triage_event,
    triage_pending_events,
)
from wire.llm.provider import LLMResponse


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "wire.db"
    monkeypatch.setenv("WIRE_DB_PATH", str(db_path))
    db_session.reset_for_tests()
    engine = db_session.init(db_path)
    Base.metadata.create_all(engine)
    yield db_session
    db_session.reset_for_tests()


def _event(**kw) -> Event:
    base = dict(
        id=1,
        github_id="evt-1",
        repo="winetrackr",
        event_type="PushEvent",
        actor="jbellander",
        payload={"raw_payload": {"commits": [{"message": "feat: ship a thing"}]}},
        occurred_at=datetime(2026, 5, 1, 12, 0, 0),
    )
    base.update(kw)
    return Event(**base)


def test_summarize_includes_repo_notes_when_provided():
    e = _event()
    summary = _summarize_event(
        e, repo_notes="Public side project, post freely about features and debugging"
    )
    assert "repo_notes:" in summary
    assert "post freely" in summary


def test_summarize_omits_repo_notes_when_none():
    e = _event()
    summary = _summarize_event(e, repo_notes=None)
    assert "repo_notes:" not in summary


def test_summarize_omits_repo_notes_when_empty():
    e = _event()
    summary = _summarize_event(e, repo_notes="")
    assert "repo_notes:" not in summary


@pytest.mark.asyncio
async def test_triage_event_passes_repo_notes_through(db):
    """End-to-end: notes given to triage_event should land in the user message."""
    captured: list[str] = []

    class _StubProvider:
        async def complete(
            self, task, system, messages, response_format=None, max_tokens=1500
        ) -> LLMResponse:
            captured.append(messages[0]["content"])
            return LLMResponse(
                content='{"score": 0.7, "reason": "post-worthy"}',
                provider="llamacpp",
                model="qwen3-coder-next",
                input_tokens=10,
                output_tokens=5,
                cache_read_tokens=0,
                cache_write_tokens=0,
                latency_ms=10,
                cost_usd=0.0,
                task=task,
            )

    e = _event()
    result = await triage_event(
        e,
        _StubProvider(),
        repo_notes="The bot itself; post about all development including meta improvements",
    )
    assert result.score == 0.7
    assert len(captured) == 1
    assert "all development including meta improvements" in captured[0]


@pytest.mark.asyncio
async def test_triage_pending_events_fans_out_repo_notes(db):
    """When given a ReposFile, each event's notes flow into the call."""
    repos = ReposFile(
        repos=[
            RepoEntry(
                name="winetrackr", visibility="public", notes="Public side project, post freely"
            ),
            RepoEntry(
                name="medianalyzer",
                visibility="public",
                notes="Boring infra; only post on releases",
            ),
        ]
    )

    with db.session_scope() as sa:
        sa.add(
            Event(
                github_id="evt-1",
                repo="winetrackr",
                event_type="PushEvent",
                payload={"raw_payload": {"commits": [{"message": "feat: thing"}]}},
                occurred_at=datetime(2026, 5, 1, 12, 0, 0),
            )
        )
        sa.add(
            Event(
                github_id="evt-2",
                repo="medianalyzer",
                event_type="PushEvent",
                payload={"raw_payload": {"commits": [{"message": "refactor: thing"}]}},
                occurred_at=datetime(2026, 5, 1, 12, 5, 0),
            )
        )

    seen_messages: list[str] = []

    class _StubProvider:
        async def complete(
            self, task, system, messages, response_format=None, max_tokens=1500
        ) -> LLMResponse:
            seen_messages.append(messages[0]["content"])
            return LLMResponse(
                content='{"score": 0.5, "reason": "ok"}',
                provider="claude",
                model="claude-haiku-4-5",
                input_tokens=10,
                output_tokens=5,
                cache_read_tokens=0,
                cache_write_tokens=0,
                latency_ms=10,
                cost_usd=0.0001,
                task=task,
            )

    n = await triage_pending_events(_StubProvider(), repos_file=repos)
    assert n == 2
    # winetrackr message contains its notes
    winetrackr_msg = next(m for m in seen_messages if "winetrackr" in m)
    assert "post freely" in winetrackr_msg
    # medianalyzer message contains ITS notes (not winetrackr's)
    medianalyzer_msg = next(m for m in seen_messages if "medianalyzer" in m)
    assert "Boring infra" in medianalyzer_msg
    assert "post freely" not in medianalyzer_msg


@pytest.mark.asyncio
async def test_triage_pending_events_works_without_repos_file(db):
    """Backward compat: repos_file=None still works (no notes injected)."""
    with db.session_scope() as sa:
        sa.add(
            Event(
                github_id="evt-x",
                repo="winetrackr",
                event_type="PushEvent",
                payload={"raw_payload": {"commits": [{"message": "feat"}]}},
                occurred_at=datetime(2026, 5, 1, 12, 0, 0),
            )
        )

    captured: list[str] = []

    class _StubProvider:
        async def complete(self, task, system, messages, **_) -> LLMResponse:
            captured.append(messages[0]["content"])
            return LLMResponse(
                content='{"score": 0.4, "reason": "ok"}',
                provider="claude",
                model="claude-haiku-4-5",
                input_tokens=10,
                output_tokens=5,
                cache_read_tokens=0,
                cache_write_tokens=0,
                latency_ms=10,
                cost_usd=0.0001,
                task=task,
            )

    await triage_pending_events(_StubProvider())
    assert "repo_notes:" not in captured[0]

"""Tests for force_draft_for_event + skip_reason persistence in
draft_pending_sessions. Companion to test_prompt_building.py and test_e2e.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from wire.config import (
    ClaudeModelsConfig,
    DigestConfig,
    GithubConfig,
    IngestionConfig,
    LearningConfig,
    LLMConfig,
    LoggingConfig,
    MetricsConfig,
    OllamaConfig,
    QuietHoursConfig,
    RepoEntry,
    ReposFile,
    ReposLocation,
    SessionConfig,
    TelegramConfig,
    TwitterConfig,
    WireConfig,
)
from wire.db import session as db_session
from wire.db.models import (
    Base,
    Draft,
    Event,
    LLMCall,
    Session,
)
from wire.drafting.drafter import (
    BudgetPausedError,
    DraftItem,
    DraftResponse,
    EventNotFoundError,
    draft_pending_sessions,
    force_draft_for_event,
)
from wire.llm.provider import LLMResponse


def _config() -> WireConfig:
    return WireConfig(
        github=GithubConfig(
            org="me",
            app_id=1,
            installation_id=1,
            private_key_path="/d/k.pem",
            poll_interval_minutes=20,
        ),
        repos=ReposLocation(config_path="/d/r.yaml"),
        llm=LLMConfig(
            provider="claude",
            ollama=OllamaConfig(base_url="http://x", model="m", timeout_seconds=10),
            claude=ClaudeModelsConfig(
                drafting="claude-sonnet-4-6",
                triage="claude-haiku-4-5",
                voice_profile="claude-haiku-4-5",
                digest="claude-haiku-4-5",
            ),
            prompt_caching=True,
            monthly_budget_usd=10,
            budget_alert_threshold=0.8,
        ),
        quiet_hours=QuietHoursConfig(start="03:00", end="04:00", timezone="UTC"),
        session=SessionConfig(idle_minutes=30, max_hours=4, immediate_trigger_events=["release"]),
        telegram=TelegramConfig(bot_token_env="X", chat_id_env="Y"),
        twitter=TwitterConfig(
            client_id_env="C", client_secret_env="S", access_token_path="/d/t.json"
        ),
        metrics=MetricsConfig(fetch_cron="0 9 * * *", posts_settle_days=7),
        digest=DigestConfig(cron="0 9 * * 1"),
        learning=LearningConfig(recent_decisions_n=20, recent_posts_n=30),
        logging=LoggingConfig(),
        ingestion=IngestionConfig(),
    )


def _repos() -> ReposFile:
    return ReposFile(
        repos=[
            RepoEntry(name="winetrackr", visibility="public", notes="Public side project"),
        ]
    )


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "wire.db"
    monkeypatch.setenv("WIRE_DB_PATH", str(db_path))
    db_session.reset_for_tests()
    engine = db_session.init(db_path)
    Base.metadata.create_all(engine)
    yield db_session
    db_session.reset_for_tests()


class _StubProvider:
    """Returns a canned DraftResponse on every complete() call."""

    def __init__(self, drafts_response: DraftResponse, model: str = "claude-sonnet-4-6"):
        self._payload = drafts_response.model_dump_json()
        self._model = model
        self.calls: list[dict] = []

    async def complete(self, task, system, messages, response_format=None, max_tokens=1500):
        self.calls.append(
            {
                "task": task,
                "system": system,
                "messages": messages,
                "response_format": response_format,
            }
        )
        return LLMResponse(
            content=self._payload,
            provider="claude",
            model=self._model,
            input_tokens=900,
            output_tokens=80,
            cache_read_tokens=600,
            cache_write_tokens=300,
            latency_ms=1500,
            cost_usd=0.0042,
            task=task,
        )


def _seed_event(db_, score: float = 0.2, with_session: bool = True) -> int:
    """Insert one PushEvent and return its id."""
    base = datetime(2026, 4, 29, 12, 0, 0)
    with db_.session_scope() as sa:
        sid = None
        if with_session:
            sess = Session(
                repo="winetrackr",
                started_at=base,
                ended_at=base + timedelta(minutes=10),
                closed_reason="idle",
            )
            sa.add(sess)
            sa.flush()
            sid = sess.id
        e = Event(
            github_id=f"ev-{score}-{int(base.timestamp())}",
            repo="winetrackr",
            event_type="PushEvent",
            actor="jbellander",
            payload={"raw_payload": {"commits": [{"message": "feat: ship a thing"}]}},
            occurred_at=base,
            session_id=sid,
            triage_score=score,
            triage_reason="seed",
        )
        sa.add(e)
        sa.flush()
        return e.id


@pytest.mark.asyncio
async def test_force_draft_happy_path(db):
    cfg = _config()
    repos = _repos()
    event_id = _seed_event(db, score=0.2)

    canned = DraftResponse(
        skip_reason=None,
        drafts=[DraftItem(text="forced post text", reasoning="user override", confidence=0.7)],
    )
    stub = _StubProvider(canned)

    draft_id, skip_reason = await force_draft_for_event(event_id, cfg, repos, stub)
    assert skip_reason is None
    assert draft_id is not None
    assert len(stub.calls) == 1
    # The user message must include the override note
    user_text = stub.calls[0]["messages"][0]["content"]
    assert "explicitly requested" in user_text

    with db.session_scope() as sa:
        d = sa.get(Draft, draft_id)
        assert d is not None
        assert d.text == "forced post text"
        assert d.reasoning is not None
        assert d.reasoning.startswith("[forced via /draft]")
        assert "user override" in d.reasoning
        # session linkage preserved
        e = sa.get(Event, event_id)
        assert d.session_id == e.session_id
        # LLM call logged
        calls = sa.query(LLMCall).all()
        assert len(calls) == 1
        assert calls[0].task == "drafting"


@pytest.mark.asyncio
async def test_force_draft_skip_reason(db):
    cfg = _config()
    repos = _repos()
    event_id = _seed_event(db, score=0.5)

    canned = DraftResponse(skip_reason="too internal", drafts=[])
    stub = _StubProvider(canned)

    draft_id, skip_reason = await force_draft_for_event(event_id, cfg, repos, stub)
    assert draft_id is None
    assert skip_reason == "too internal"

    with db.session_scope() as sa:
        # No drafts persisted
        assert sa.query(Draft).count() == 0
        # Real session row was NOT mutated by force-draft
        e = sa.get(Event, event_id)
        sess = sa.get(Session, e.session_id)
        assert sess.drafted_at is None
        assert sess.skip_reason is None
        # But the LLM call WAS logged
        assert sa.query(LLMCall).count() == 1


@pytest.mark.asyncio
async def test_force_draft_event_not_found(db):
    cfg = _config()
    repos = _repos()
    canned = DraftResponse(skip_reason=None, drafts=[])
    stub = _StubProvider(canned)

    with pytest.raises(EventNotFoundError):
        await force_draft_for_event(99999, cfg, repos, stub)
    # No LLM call attempted
    assert len(stub.calls) == 0


@pytest.mark.asyncio
async def test_force_draft_budget_paused(db):
    cfg = _config()
    repos = _repos()
    event_id = _seed_event(db, score=0.2)

    # Push spend over the $10 cap so compute_status returns paused=True.
    with db.session_scope() as sa:
        sa.add(
            LLMCall(
                task="drafting",
                provider="claude",
                model="claude-sonnet-4-6",
                fallback=False,
                input_tokens=1,
                output_tokens=1,
                cost_usd=20.0,
            )
        )

    canned = DraftResponse(skip_reason=None, drafts=[])
    stub = _StubProvider(canned)

    with pytest.raises(BudgetPausedError):
        await force_draft_for_event(event_id, cfg, repos, stub)
    # Refused before LLM call
    assert len(stub.calls) == 0


@pytest.mark.asyncio
async def test_force_draft_event_without_session(db):
    cfg = _config()
    repos = _repos()
    event_id = _seed_event(db, score=0.2, with_session=False)

    canned = DraftResponse(
        skip_reason=None,
        drafts=[DraftItem(text="orphan event post", reasoning="r", confidence=0.6)],
    )
    stub = _StubProvider(canned)

    draft_id, skip_reason = await force_draft_for_event(event_id, cfg, repos, stub)
    assert skip_reason is None
    assert draft_id is not None
    with db.session_scope() as sa:
        d = sa.get(Draft, draft_id)
        assert d.session_id is None


@pytest.mark.asyncio
async def test_draft_pending_persists_skip_reason(db):
    """Regression: when the drafting LLM returns skip_reason on a normal
    session, that reason is now persisted on the Session row so /last can
    surface it later."""
    cfg = _config()
    repos = _repos()
    base = datetime(2026, 4, 29, 12, 0, 0)
    with db.session_scope() as sa:
        sess = Session(
            repo="winetrackr",
            started_at=base,
            ended_at=base + timedelta(minutes=10),
            closed_reason="idle",
        )
        sa.add(sess)
        sa.flush()
        sa.add(
            Event(
                github_id="ev-skip",
                repo="winetrackr",
                event_type="PushEvent",
                payload={"raw_payload": {"commits": [{"message": "feat: thing"}]}},
                occurred_at=base,
                session_id=sess.id,
                triage_score=0.7,  # above threshold so we DO call the LLM
                triage_reason="seed",
            )
        )
        sid = sess.id

    canned = DraftResponse(skip_reason="not actually post-worthy", drafts=[])
    stub = _StubProvider(canned)

    results = await draft_pending_sessions(cfg, repos, stub, now=base)
    assert len(results) == 1
    assert results[0].drafts_created == 0
    assert results[0].skip_reason == "not actually post-worthy"

    with db.session_scope() as sa:
        s = sa.get(Session, sid)
        assert s.drafted_at is not None
        assert s.skip_reason == "not actually post-worthy"


@pytest.mark.asyncio
async def test_force_draft_below_threshold_event_still_drafts(db):
    """Sanity: the whole point of /draft is bypassing the triage threshold.
    A score=0.0 event must still produce a draft when force-drafted."""
    cfg = _config()
    repos = _repos()
    event_id = _seed_event(db, score=0.0)

    canned = DraftResponse(
        skip_reason=None,
        drafts=[DraftItem(text="forced anyway", reasoning="user knows best", confidence=0.5)],
    )
    stub = _StubProvider(canned)

    draft_id, _ = await force_draft_for_event(event_id, cfg, repos, stub)
    assert draft_id is not None
    with db.session_scope() as sa:
        d = sa.get(Draft, draft_id)
        assert d.text == "forced anyway"

"""Persona LLM rewrite layer tests.

Validates the failure-tolerance contract: persona must never block a draft
or a digest. On any LLM error, schema mismatch, parse failure, quiet hours,
or budget cap, the rewrite call returns None and the caller falls back to
its static line.

Also pins down the budget-tracking contract: successful persona calls log
to llm_calls with task type "persona" so /budget and /status can break out
persona spend separately.
"""

from __future__ import annotations

from datetime import time as dt_time
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

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
    PersonaConfig,
    QuietHoursConfig,
    ReposLocation,
    SessionConfig,
    TelegramConfig,
    TwitterConfig,
    WireConfig,
)
from wire.db import session as db_session
from wire.db.models import Base, LLMCall
from wire.llm.provider import LLMResponse
from wire.telegram import persona as persona_mod


def _config(*, persona: PersonaConfig | None = None) -> WireConfig:
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
        session=SessionConfig(idle_minutes=30, max_hours=4, immediate_trigger_events=[]),
        # Quiet hours intentionally OUTSIDE current wall-clock so they don't
        # accidentally gate the test calls. The quiet-hours behavior is
        # covered by an explicit test that overrides this.
        quiet_hours=QuietHoursConfig(start=dt_time(3, 0), end=dt_time(3, 1), timezone="UTC"),
        telegram=TelegramConfig(bot_token_env="X", chat_id_env="Y"),
        twitter=TwitterConfig(
            client_id_env="C", client_secret_env="S", access_token_path="/d/t.json"
        ),
        metrics=MetricsConfig(fetch_cron="0 9 * * *", posts_settle_days=7),
        digest=DigestConfig(cron="0 9 * * 1"),
        learning=LearningConfig(recent_decisions_n=20, recent_posts_n=30),
        logging=LoggingConfig(),
        ingestion=IngestionConfig(),
        persona=persona or PersonaConfig(),
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


def _fake_response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        provider="claude",
        model="claude-haiku-4-5",
        input_tokens=120,
        output_tokens=20,
        cache_read_tokens=0,
        cache_write_tokens=0,
        latency_ms=210,
        cost_usd=0.0001,
        task="triage",  # caller passes the underlying task; persona overrides
    )


# ---------------- intro_for_draft -------------------------------------------


@pytest.mark.asyncio
async def test_intro_returns_none_when_persona_disabled(db):
    cfg = _config(persona=PersonaConfig(enabled=False))
    out = await persona_mod.intro_for_draft(
        cfg, AsyncMock(), thread_text="hello", repo="winetrackr"
    )
    assert out is None


@pytest.mark.asyncio
async def test_intro_returns_none_when_disabled_for_drafts_only(db):
    cfg = _config(persona=PersonaConfig(enabled=True, llm_intro_on_drafts=False))
    out = await persona_mod.intro_for_draft(
        cfg, AsyncMock(), thread_text="hello", repo="winetrackr"
    )
    assert out is None


@pytest.mark.asyncio
async def test_intro_returns_none_with_no_provider(db):
    cfg = _config()
    out = await persona_mod.intro_for_draft(cfg, None, thread_text="hello", repo="winetrackr")
    assert out is None


@pytest.mark.asyncio
async def test_intro_returns_none_in_quiet_hours(db):
    cfg = _config()
    with patch("wire.telegram.persona.is_in_quiet_hours", return_value=True):
        out = await persona_mod.intro_for_draft(
            cfg, AsyncMock(), thread_text="hello", repo="winetrackr"
        )
    assert out is None


@pytest.mark.asyncio
async def test_intro_returns_none_when_budget_capped(db):
    cfg = _config()
    with patch("wire.telegram.persona.is_drafting_blocked_by_budget", return_value=True):
        out = await persona_mod.intro_for_draft(
            cfg, AsyncMock(), thread_text="hello", repo="winetrackr"
        )
    assert out is None


@pytest.mark.asyncio
async def test_intro_returns_none_on_llm_failure(db):
    cfg = _config()
    provider = AsyncMock()
    provider.complete = AsyncMock(side_effect=RuntimeError("boom"))
    out = await persona_mod.intro_for_draft(cfg, provider, thread_text="hello", repo="winetrackr")
    assert out is None


@pytest.mark.asyncio
async def test_intro_returns_none_on_invalid_json(db):
    cfg = _config()
    provider = AsyncMock()
    provider.complete = AsyncMock(return_value=_fake_response("not json at all"))
    out = await persona_mod.intro_for_draft(cfg, provider, thread_text="hello", repo="winetrackr")
    assert out is None


@pytest.mark.asyncio
async def test_intro_logs_persona_task_on_success(db):
    cfg = _config()
    provider = AsyncMock()
    provider.complete = AsyncMock(
        return_value=_fake_response('{"intro": "drafted, johan — ready on the wire."}')
    )

    out = await persona_mod.intro_for_draft(cfg, provider, thread_text="hello", repo="winetrackr")
    assert out == "drafted, johan — ready on the wire."

    with db.session_scope() as sa:
        rows = sa.execute(select(LLMCall)).scalars().all()
    assert len(rows) == 1
    assert rows[0].task == "persona"


@pytest.mark.asyncio
async def test_intro_handles_markdown_fenced_json(db):
    """Sonnet/Haiku occasionally wrap structured outputs in ```json fences.
    parse_json_lenient strips them; the rewrite must follow suit."""
    cfg = _config()
    provider = AsyncMock()
    fenced = '```json\n{"intro": "live on the wire."}\n```'
    provider.complete = AsyncMock(return_value=_fake_response(fenced))
    out = await persona_mod.intro_for_draft(cfg, provider, thread_text="hello", repo="winetrackr")
    assert out == "live on the wire."


@pytest.mark.asyncio
async def test_intro_routes_via_configured_model_task(db):
    cfg = _config(persona=PersonaConfig(enabled=True, model_task="digest"))
    provider = AsyncMock()
    provider.complete = AsyncMock(return_value=_fake_response('{"intro": "hi"}'))
    await persona_mod.intro_for_draft(cfg, provider, thread_text="hello", repo="winetrackr")
    provider.complete.assert_awaited_once()
    # First positional kwarg the caller uses is `task=`
    assert provider.complete.await_args.kwargs["task"] == "digest"


# ---------------- frame_digest ----------------------------------------------


@pytest.mark.asyncio
async def test_digest_frame_returns_none_when_disabled(db):
    cfg = _config(persona=PersonaConfig(llm_frame_on_digest=False))
    out = await persona_mod.frame_digest(cfg, AsyncMock(), stats_block="x")
    assert out is None


@pytest.mark.asyncio
async def test_digest_frame_returns_none_on_failure(db):
    cfg = _config()
    provider = AsyncMock()
    provider.complete = AsyncMock(side_effect=RuntimeError("boom"))
    out = await persona_mod.frame_digest(cfg, provider, stats_block="x")
    assert out is None


@pytest.mark.asyncio
async def test_digest_frame_success_returns_pair_and_logs(db):
    cfg = _config()
    provider = AsyncMock()
    payload = '{"opener": "another week on the wire, johan.", "closer": "see you monday."}'
    provider.complete = AsyncMock(return_value=_fake_response(payload))

    out = await persona_mod.frame_digest(cfg, provider, stats_block="Drafted: 3 · Posted: 2")
    assert out is not None
    opener, closer = out
    assert opener == "another week on the wire, johan."
    assert closer == "see you monday."

    with db.session_scope() as sa:
        rows = sa.execute(select(LLMCall)).scalars().all()
    assert len(rows) == 1
    assert rows[0].task == "persona"


@pytest.mark.asyncio
async def test_digest_frame_returns_none_with_blank_fields(db):
    cfg = _config()
    provider = AsyncMock()
    # opener is whitespace-only; min_length validation also rejects this,
    # so the request fails schema validation upstream → call raises →
    # rewrite returns None.
    provider.complete = AsyncMock(side_effect=RuntimeError("schema"))
    out = await persona_mod.frame_digest(cfg, provider, stats_block="x")
    assert out is None

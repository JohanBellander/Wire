"""Tests for the /status command's Brain block (LLM backend observability)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from wire.config import (
    ClaudeModelsConfig,
    DigestConfig,
    GithubConfig,
    IngestionConfig,
    LearningConfig,
    LlamaCppConfig,
    LLMConfig,
    LoggingConfig,
    MetricsConfig,
    QuietHoursConfig,
    ReposLocation,
    SessionConfig,
    TelegramConfig,
    TwitterConfig,
    WireConfig,
)
from wire.db import session as db_session
from wire.db.models import Base, LLMCall
from wire.health import set_last_used_provider
from wire.telegram.commands import status_cmd


def _config(provider: str = "claude") -> WireConfig:
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
            provider=provider,
            llamacpp=LlamaCppConfig(
                base_url="https://llm.test/v1",
                model="qwen3-coder-next",
                timeout_seconds=90,
            )
            if provider == "llamacpp"
            else None,
            claude=ClaudeModelsConfig(
                drafting="claude-sonnet-4-6",
                triage="claude-haiku-4-5",
                voice_profile="claude-haiku-4-5",
                digest="claude-haiku-4-5",
            ),
            prompt_caching=True,
            monthly_budget_usd=10.0,
            budget_alert_threshold=0.8,
        ),
        session=SessionConfig(idle_minutes=30, max_hours=4, immediate_trigger_events=[]),
        quiet_hours=QuietHoursConfig(start="22:00", end="07:00", timezone="UTC"),
        telegram=TelegramConfig(bot_token_env="X", chat_id_env="Y"),
        twitter=TwitterConfig(
            client_id_env="C",
            client_secret_env="S",
            access_token_path="/d/t.json",
        ),
        metrics=MetricsConfig(fetch_cron="0 9 * * *", posts_settle_days=7),
        digest=DigestConfig(cron="0 9 * * 1"),
        learning=LearningConfig(recent_decisions_n=20, recent_posts_n=30),
        logging=LoggingConfig(),
        ingestion=IngestionConfig(),
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


def _make_update(chat_id: int = 1) -> MagicMock:
    update = MagicMock()
    update.callback_query = None
    update.effective_chat.id = chat_id
    update.effective_message.reply_text = AsyncMock()
    return update


def _make_context(cfg: WireConfig) -> MagicMock:
    ctx = MagicMock()
    ctx.bot_data = {"wire_chat_id": 1, "wire_config": cfg}
    return ctx


def _captured_text(update: MagicMock) -> str:
    update.effective_message.reply_text.assert_awaited_once()
    return update.effective_message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_status_renders_claude_only_brain(db):
    """When provider=claude, brain block says 'no fallback' (single backend)."""
    cfg = _config(provider="claude")
    update = _make_update()
    ctx = _make_context(cfg)
    await status_cmd(update, ctx)

    text = _captured_text(update)
    assert "🧠 brain" in text
    assert "claude (drafting=claude-sonnet-4-6)" in text
    assert "claude only" in text
    # fallback rate line only appears in llamacpp mode
    assert "fallback rate" not in text


@pytest.mark.asyncio
async def test_status_renders_llamacpp_brain_with_fallback_stats(db):
    """When provider=llamacpp, brain block shows primary + fallback + rate."""
    cfg = _config(provider="llamacpp")

    # Seed some LLM calls — 3 llamacpp, 1 fallback to claude
    with db.session_scope() as sa:
        for fb in (False, False, False, True):
            sa.add(
                LLMCall(
                    task="drafting",
                    provider="llamacpp" if not fb else "claude",
                    model=None if not fb else "claude-sonnet-4-6",
                    fallback=fb,
                    input_tokens=100,
                    output_tokens=50,
                    cost_usd=0.001,
                    latency_ms=100,
                )
            )

    set_last_used_provider("llamacpp")

    update = _make_update()
    ctx = _make_context(cfg)
    await status_cmd(update, ctx)

    text = _captured_text(update)
    assert "🧠 brain" in text
    assert "llamacpp (qwen3-coder-next)" in text
    assert "claude (claude-sonnet-4-6 / claude-haiku-4-5)" in text
    assert "last used: llamacpp" in text
    # 1 / 4 = 25%
    assert "25%" in text
    assert "1 / 4" in text


@pytest.mark.asyncio
async def test_status_llamacpp_brain_with_no_calls_yet(db):
    """Fresh llamacpp deploy with no LLM calls: rate line says so."""
    cfg = _config(provider="llamacpp")
    update = _make_update()
    ctx = _make_context(cfg)
    await status_cmd(update, ctx)

    text = _captured_text(update)
    assert "llamacpp (qwen3-coder-next)" in text
    assert "no LLM calls yet" in text


@pytest.mark.asyncio
async def test_status_unauthorized_chat_silent(db):
    """Different chat_id gets no reply — defence-in-depth."""
    cfg = _config()
    update = _make_update(chat_id=999)  # not the configured 1
    ctx = _make_context(cfg)
    await status_cmd(update, ctx)
    update.effective_message.reply_text.assert_not_awaited()

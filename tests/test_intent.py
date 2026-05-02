"""Tests for the natural-language intent classifier.

The classifier wraps a small LLM call. Tests stub the provider so they're
deterministic, fast, and don't burn tokens.
"""

from __future__ import annotations

import json

import pytest

from wire.config import (
    ClaudeModelsConfig,
    DigestConfig,
    GithubConfig,
    LearningConfig,
    LLMConfig,
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
from wire.db.models import Base
from wire.llm.provider import LLMResponse
from wire.telegram.intent import ClassifiedIntent, classify


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "wire.db"
    monkeypatch.setenv("WIRE_DB_PATH", str(db_path))
    db_session.reset_for_tests()
    engine = db_session.init(db_path)
    Base.metadata.create_all(engine)
    yield db_session
    db_session.reset_for_tests()


def _config() -> WireConfig:
    return WireConfig(
        github=GithubConfig(
            org="me",
            app_id=1,
            installation_id=1,
            private_key_path="/data/secrets/github-app.pem",
            poll_interval_minutes=20,
        ),
        repos=ReposLocation(config_path="/data/repos.yaml"),
        llm=LLMConfig(
            provider="ollama",
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
        quiet_hours=QuietHoursConfig(start="22:00", end="07:00", timezone="Europe/Stockholm"),
        telegram=TelegramConfig(bot_token_env="X", chat_id_env="Y"),
        twitter=TwitterConfig(
            client_id_env="C",
            client_secret_env="S",
            access_token_path="/data/secrets/twitter-token.json",
        ),
        metrics=MetricsConfig(fetch_cron="0 9 * * *", posts_settle_days=7),
        digest=DigestConfig(cron="0 9 * * 1"),
        learning=LearningConfig(recent_decisions_n=20, recent_posts_n=30),
        persona=PersonaConfig(),
    )


class _StubProvider:
    """Returns a canned JSON response. Captures the messages it was called
    with so tests can assert system/user content."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[dict] = []

    async def complete(self, task, system, messages, response_format=None, max_tokens=200):
        self.calls.append({"task": task, "system": system, "messages": messages})
        return LLMResponse(
            content=json.dumps(self.payload),
            provider="ollama",
            model="m",
            input_tokens=10,
            output_tokens=8,
            cache_read_tokens=0,
            cache_write_tokens=0,
            latency_ms=12,
            cost_usd=0.0,
            task=task,
        )


@pytest.mark.asyncio
async def test_classify_status_intent(db):
    stub = _StubProvider({"intent": "status", "args": {}, "confidence": 0.95})
    result = await classify("how are you doing", _config(), stub)
    assert isinstance(result, ClassifiedIntent)
    assert result.intent == "status"
    assert result.args == {}
    # The provider was called with the persona model_task (default: "triage").
    assert stub.calls[0]["task"] == "triage"


@pytest.mark.asyncio
async def test_classify_extracts_extend_amount(db):
    stub = _StubProvider({"intent": "extend", "args": {"usd": 20}, "confidence": 0.9})
    result = await classify("extend the budget by 20 dollars", _config(), stub)
    assert result.intent == "extend"
    assert result.args["usd"] == 20


@pytest.mark.asyncio
async def test_classify_extracts_last_n(db):
    stub = _StubProvider({"intent": "last", "args": {"n": 15}, "confidence": 0.85})
    result = await classify("show me the last 15 events", _config(), stub)
    assert result.intent == "last"
    assert result.args["n"] == 15


@pytest.mark.asyncio
async def test_classify_unknown_for_smalltalk(db):
    stub = _StubProvider({"intent": "unknown", "args": {}, "confidence": 0.2})
    result = await classify("hi wire what time is it", _config(), stub)
    assert result.intent == "unknown"


@pytest.mark.asyncio
async def test_classify_returns_unknown_on_provider_failure(db):
    class _Failing:
        async def complete(self, **kwargs):
            raise RuntimeError("network down")

    result = await classify("status", _config(), _Failing())
    assert result.intent == "unknown"


@pytest.mark.asyncio
async def test_classify_returns_unknown_on_invalid_json(db):
    class _BadJson:
        async def complete(self, task, system, messages, response_format=None, max_tokens=200):
            return LLMResponse(
                content="not json at all, sorry",
                provider="ollama",
                model="m",
                input_tokens=10,
                output_tokens=5,
                cache_read_tokens=0,
                cache_write_tokens=0,
                latency_ms=10,
                cost_usd=0.0,
                task=task,
            )

    result = await classify("status", _config(), _BadJson())
    assert result.intent == "unknown"


@pytest.mark.asyncio
async def test_classify_returns_unknown_on_schema_mismatch(db):
    """If the model returns syntactically-valid JSON but with an unknown
    intent string, pydantic Literal validation fails → unknown."""
    stub = _StubProvider({"intent": "frobnicate", "args": {}, "confidence": 0.9})
    result = await classify("status", _config(), stub)
    assert result.intent == "unknown"


@pytest.mark.asyncio
async def test_classify_no_provider_returns_unknown(db):
    result = await classify("status", _config(), None)
    assert result.intent == "unknown"


@pytest.mark.asyncio
async def test_classify_empty_input_returns_unknown(db):
    stub = _StubProvider({"intent": "status", "args": {}, "confidence": 1.0})
    # Empty / whitespace-only input shouldn't even reach the LLM.
    result = await classify("   ", _config(), stub)
    assert result.intent == "unknown"
    assert stub.calls == []


@pytest.mark.asyncio
async def test_classify_logs_llm_call(db):
    """log_llm_call must run after every provider.complete (CLAUDE.md
    'Critical conventions'). Verify a row lands in llm_calls."""
    from wire.db.models import LLMCall

    stub = _StubProvider({"intent": "status", "args": {}, "confidence": 0.9})
    await classify("status", _config(), stub)

    with db.session_scope() as sa:
        rows = sa.query(LLMCall).all()
        assert len(rows) == 1
        # We bucket intent calls under task="persona" so /budget shows them
        # alongside the other Telegram-side LLM costs.
        assert rows[0].task == "persona"

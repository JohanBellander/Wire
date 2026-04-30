"""Step 4 — LLM provider fallback tests.

Mocks both providers; no real API calls. Covers:
  - ollama success → no fallback
  - ollama timeout → claude fallback
  - ollama invalid JSON → claude fallback
  - ollama HTTP 500 → claude fallback
  - ollama auth error → NOT fallen back (re-raised)
  - claude provider when provider=claude → no ollama call
  - schema validation triggers fallback
"""

from __future__ import annotations

import json
from datetime import time as dt_time

import httpx
import pytest
from pydantic import BaseModel

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
    ReposLocation,
    SessionConfig,
    TelegramConfig,
    TwitterConfig,
    WireConfig,
)
from wire.llm.budget import estimate_cost_usd
from wire.llm.provider import (
    ClaudeProvider,
    FallbackProvider,
    LLMAuthError,
    LLMResponse,
    OllamaProvider,
    build_provider,
    parse_json_lenient,
)


def _llm_cfg(provider: str) -> LLMConfig:
    return LLMConfig(
        provider=provider,
        ollama=OllamaConfig(
            base_url="http://ollama.test:11434",
            model="qwen2.5:7b-instruct",
            timeout_seconds=10,
        ),
        claude=ClaudeModelsConfig(
            drafting="claude-sonnet-4-6",
            triage="claude-haiku-4-5",
            voice_profile="claude-haiku-4-5",
            digest="claude-haiku-4-5",
        ),
        prompt_caching=True,
        monthly_budget_usd=10.0,
        budget_alert_threshold=0.8,
    )


class _Schema(BaseModel):
    text: str
    confidence: float


# ---------- Ollama path ------------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_success_no_fallback(respx_mock):
    cfg = _llm_cfg("ollama")
    payload = {
        "message": {"content": json.dumps({"text": "ok hello world", "confidence": 0.9})},
        "prompt_eval_count": 50,
        "eval_count": 12,
    }
    respx_mock.post("http://ollama.test:11434/api/chat").mock(
        return_value=httpx.Response(200, json=payload)
    )

    # Use a fake claude provider that fails the test if invoked.
    fake_claude = _AssertNotCalledClaude(cfg)
    ollama = OllamaProvider(cfg)
    fb = FallbackProvider(primary=ollama, fallback=fake_claude)
    try:
        resp = await fb.complete(
            "drafting",
            "system",
            [{"role": "user", "content": "x"}],
            response_format=_Schema,
        )
    finally:
        await ollama.aclose()

    assert resp.provider == "ollama"
    assert resp.fallback_used is False
    assert resp.input_tokens == 50
    assert resp.output_tokens == 12
    assert fake_claude.calls == 0


@pytest.mark.asyncio
async def test_ollama_timeout_falls_back_to_claude(respx_mock):
    cfg = _llm_cfg("ollama")
    respx_mock.post("http://ollama.test:11434/api/chat").mock(
        side_effect=httpx.ReadTimeout("ollama timed out")
    )
    ollama = OllamaProvider(cfg)
    fake_claude = _StubClaude(cfg, content=json.dumps({"text": "claude saved you", "confidence": 0.7}))
    fb = FallbackProvider(primary=ollama, fallback=fake_claude)
    try:
        resp = await fb.complete(
            "drafting",
            "system",
            [{"role": "user", "content": "x"}],
            response_format=_Schema,
        )
    finally:
        await ollama.aclose()

    assert resp.provider == "claude"
    assert resp.fallback_used is True
    assert fake_claude.calls == 1
    # Same task forwarded
    assert fake_claude.last_task == "drafting"


@pytest.mark.asyncio
async def test_ollama_invalid_json_falls_back(respx_mock):
    cfg = _llm_cfg("ollama")
    respx_mock.post("http://ollama.test:11434/api/chat").mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {"content": "this is not json at all, but it is long enough"},
                "prompt_eval_count": 10,
                "eval_count": 10,
            },
        )
    )
    ollama = OllamaProvider(cfg)
    fake_claude = _StubClaude(cfg, content=json.dumps({"text": "saved", "confidence": 0.5}))
    fb = FallbackProvider(primary=ollama, fallback=fake_claude)
    try:
        resp = await fb.complete(
            "drafting", "system", [{"role": "user", "content": "x"}],
            response_format=_Schema,
        )
    finally:
        await ollama.aclose()
    assert resp.provider == "claude"
    assert resp.fallback_used is True


@pytest.mark.asyncio
async def test_ollama_http_500_falls_back(respx_mock):
    cfg = _llm_cfg("ollama")
    respx_mock.post("http://ollama.test:11434/api/chat").mock(
        return_value=httpx.Response(500, text="model crashed")
    )
    ollama = OllamaProvider(cfg)
    fake_claude = _StubClaude(cfg, content=json.dumps({"text": "ok", "confidence": 1.0}))
    fb = FallbackProvider(primary=ollama, fallback=fake_claude)
    try:
        resp = await fb.complete(
            "drafting", "system", [{"role": "user", "content": "x"}],
            response_format=_Schema,
        )
    finally:
        await ollama.aclose()
    assert resp.provider == "claude"
    assert resp.fallback_used is True


@pytest.mark.asyncio
async def test_ollama_auth_error_does_not_fall_back(respx_mock):
    cfg = _llm_cfg("ollama")
    respx_mock.post("http://ollama.test:11434/api/chat").mock(
        return_value=httpx.Response(401, text="nope")
    )
    ollama = OllamaProvider(cfg)
    fake_claude = _AssertNotCalledClaude(cfg)
    fb = FallbackProvider(primary=ollama, fallback=fake_claude)
    try:
        with pytest.raises(LLMAuthError):
            await fb.complete(
                "drafting", "system", [{"role": "user", "content": "x"}],
                response_format=_Schema,
            )
    finally:
        await ollama.aclose()
    assert fake_claude.calls == 0


@pytest.mark.asyncio
async def test_ollama_schema_mismatch_falls_back(respx_mock):
    cfg = _llm_cfg("ollama")
    # Valid JSON but wrong shape: missing required field
    respx_mock.post("http://ollama.test:11434/api/chat").mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {"content": json.dumps({"text": "missing confidence field"})},
                "prompt_eval_count": 10,
                "eval_count": 10,
            },
        )
    )
    ollama = OllamaProvider(cfg)
    fake_claude = _StubClaude(cfg, content=json.dumps({"text": "ok", "confidence": 1.0}))
    fb = FallbackProvider(primary=ollama, fallback=fake_claude)
    try:
        resp = await fb.complete(
            "drafting", "system", [{"role": "user", "content": "x"}],
            response_format=_Schema,
        )
    finally:
        await ollama.aclose()
    assert resp.provider == "claude"
    assert resp.fallback_used is True


# ---------- Claude-only path -------------------------------------------------


@pytest.mark.asyncio
async def test_claude_only_provider_skips_ollama_entirely():
    cfg = _llm_cfg("claude")
    fake_claude = _StubClaude(cfg, content=json.dumps({"text": "hello", "confidence": 0.9}))
    # We pass an OllamaProvider that would fail-test if used, but build_provider
    # for provider=claude shouldn't even reach for it.
    fb_or_claude = build_provider(cfg, claude=fake_claude)
    assert isinstance(fb_or_claude, _StubClaude)
    resp = await fb_or_claude.complete(
        "drafting", "system", [{"role": "user", "content": "x"}],
        response_format=_Schema,
    )
    assert resp.provider == "claude"
    assert resp.fallback_used is False
    assert fake_claude.calls == 1


# ---------- Build factory ----------------------------------------------------


def test_build_provider_dispatches():
    cfg_c = _llm_cfg("claude")
    cfg_o = _llm_cfg("ollama")
    p1 = build_provider(cfg_c, claude=_StubClaude(cfg_c, content=""))
    p2 = build_provider(cfg_o, claude=_StubClaude(cfg_o, content=""), ollama=OllamaProvider(cfg_o))
    assert p1.name == "claude"
    assert p2.name == "fallback"


def test_estimate_cost_basic():
    # Sonnet, no caching: 1k input + 500 output
    c = estimate_cost_usd("claude-sonnet-4-6", 1000, 500, 0, 0)
    # 1000 * 3/1M + 500 * 15/1M = 0.003 + 0.0075 = 0.0105
    assert abs(c - 0.0105) < 1e-6


def test_estimate_cost_with_cache():
    # Sonnet: 1000 in_tokens of which 800 cache_read, 200 fresh; 100 output
    c = estimate_cost_usd("claude-sonnet-4-6", 1000, 100, 800, 0)
    # fresh = 200 * 3/1M = 0.0006; cache_read = 800 * 0.30/1M = 0.00024;
    # output = 100 * 15/1M = 0.0015 → total 0.00234
    assert abs(c - 0.00234) < 1e-6


def test_estimate_cost_unknown_model_returns_zero():
    assert estimate_cost_usd("ollama-qwen", 1000, 500) == 0.0
    # haiku family fallback works
    assert estimate_cost_usd("claude-haiku-4-5", 1000, 500) > 0


# ---------- parse_json_lenient ----------------------------------------------


def test_parse_json_clean():
    assert parse_json_lenient('{"a": 1}') == {"a": 1}


def test_parse_json_strips_markdown_fences():
    payload = '```json\n{"a": 1, "b": "hi"}\n```'
    assert parse_json_lenient(payload) == {"a": 1, "b": "hi"}


def test_parse_json_strips_bare_fences():
    payload = '```\n{"a": 1}\n```'
    assert parse_json_lenient(payload) == {"a": 1}


def test_parse_json_extracts_from_prose():
    """Claude sometimes prefixes 'Here is the JSON:' or similar."""
    payload = 'Here is the response:\n{"profile_text": "lowercase, terse"}'
    result = parse_json_lenient(payload)
    assert result == {"profile_text": "lowercase, terse"}


def test_parse_json_extracts_with_trailing_prose():
    payload = '{"a": 1}\n\nThat\'s your JSON.'
    assert parse_json_lenient(payload) == {"a": 1}


def test_parse_json_handles_array():
    payload = '```json\n[{"x": 1}, {"x": 2}]\n```'
    assert parse_json_lenient(payload) == [{"x": 1}, {"x": 2}]


def test_parse_json_raises_on_unrecoverable():
    import json as _json
    with pytest.raises(_json.JSONDecodeError):
        parse_json_lenient("this is plain prose with no json at all")


# ---------- Stub Claude provider for tests -----------------------------------


class _StubClaude(ClaudeProvider):
    """ClaudeProvider with a fake messages.create — never hits the network."""

    def __init__(self, cfg: LLMConfig, content: str, in_tok: int = 100, out_tok: int = 50):
        # Skip parent __init__'s client construction by passing a dummy client
        super().__init__(cfg, client=_FakeAnthropic())
        self._content = content
        self._in = in_tok
        self._out = out_tok
        self.calls = 0
        self.last_task: str | None = None

    async def complete(  # type: ignore[override]
        self, task, system, messages, response_format=None, max_tokens=1500,
    ) -> LLMResponse:
        self.calls += 1
        self.last_task = task
        # Run the same validator that the real provider runs.
        from wire.llm.provider import _validate_output
        _validate_output(self._content, response_format)
        return LLMResponse(
            content=self._content,
            provider="claude",
            model=self._models[task],
            input_tokens=self._in,
            output_tokens=self._out,
            cache_read_tokens=0,
            cache_write_tokens=0,
            latency_ms=10,
            cost_usd=estimate_cost_usd(self._models[task], self._in, self._out, 0, 0),
            task=task,
        )


class _AssertNotCalledClaude(_StubClaude):
    def __init__(self, cfg: LLMConfig):
        super().__init__(cfg, content="should not be called")

    async def complete(self, *a, **kw):  # type: ignore[override]
        self.calls += 1
        raise AssertionError("ClaudeProvider.complete called when it should not have been")


class _FakeAnthropic:
    """Minimal stand-in for AsyncAnthropic so super().__init__ doesn't error."""

    class _Messages:
        async def create(self, **kw):
            raise AssertionError("FakeAnthropic.messages.create should never be called from stubs")

    def __init__(self):
        self.messages = self._Messages()

"""LlamaCppProvider tests.

OpenAI-compatible HTTP backend (llama.cpp / vLLM / OpenRouter / ...) with
Bearer auth and JSON-schema enforced structured output. Covers the fallback
contract: transient + schema errors fall through to Claude, auth errors
re-raise so a misconfigured key isn't silently masked.

All tests use respx — no real network calls.
"""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel

from wire.config import (
    ClaudeModelsConfig,
    LlamaCppConfig,
    LLMConfig,
)
from wire.llm.provider import (
    ClaudeProvider,
    FallbackProvider,
    LlamaCppProvider,
    LLMAuthError,
    LLMResponse,
    build_provider,
    probe_llamacpp,
)


def _llm_cfg(
    *,
    api_key_env: str = "LLM_API_KEY",
    temperature: float = 0.5,
) -> LLMConfig:
    return LLMConfig(
        provider="llamacpp",
        llamacpp=LlamaCppConfig(
            base_url="https://llm.test/v1",
            model="qwen3-coder-next",
            timeout_seconds=10,
            api_key_env=api_key_env,
            temperature=temperature,
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


def _oai_response(content: str, *, prompt_tokens: int = 50, completion_tokens: int = 12) -> dict:
    return {
        "id": "chatcmpl-x",
        "object": "chat.completion",
        "model": "qwen3-coder-next",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


# ---------- happy path ------------------------------------------------------


@pytest.mark.asyncio
async def test_llamacpp_success_no_fallback(respx_mock, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    cfg = _llm_cfg()
    payload = _oai_response(json.dumps({"text": "ok hello world", "confidence": 0.9}))
    respx_mock.post("https://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=payload)
    )

    provider = LlamaCppProvider(cfg)
    try:
        resp = await provider.complete(
            "drafting",
            "system prompt here",
            [{"role": "user", "content": "x"}],
            response_format=_Schema,
        )
    finally:
        await provider.aclose()

    assert resp.provider == "llamacpp"
    assert resp.model == "qwen3-coder-next"
    assert resp.input_tokens == 50
    assert resp.output_tokens == 12
    assert resp.cost_usd == 0.0  # local backend, no cost


@pytest.mark.asyncio
async def test_llamacpp_sends_bearer_auth_when_key_set(respx_mock, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-secret-abc")
    cfg = _llm_cfg()
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json=_oai_response(json.dumps({"text": "ok response", "confidence": 0.8})),
        )

    respx_mock.post("https://llm.test/v1/chat/completions").mock(side_effect=handler)
    provider = LlamaCppProvider(cfg)
    try:
        await provider.complete(
            "drafting", "sys", [{"role": "user", "content": "x"}], response_format=_Schema
        )
    finally:
        await provider.aclose()

    assert captured[0].headers["Authorization"] == "Bearer sk-secret-abc"


@pytest.mark.asyncio
async def test_llamacpp_no_auth_header_when_key_missing(respx_mock, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    cfg = _llm_cfg()
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json=_oai_response(json.dumps({"text": "ok response", "confidence": 0.8})),
        )

    respx_mock.post("https://llm.test/v1/chat/completions").mock(side_effect=handler)
    provider = LlamaCppProvider(cfg)
    try:
        await provider.complete(
            "drafting", "sys", [{"role": "user", "content": "x"}], response_format=_Schema
        )
    finally:
        await provider.aclose()

    # Unauth'd local servers should still work
    assert "Authorization" not in captured[0].headers


# ---------- structured output ----------------------------------------------


@pytest.mark.asyncio
async def test_llamacpp_sends_json_schema_response_format(respx_mock, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "k")
    cfg = _llm_cfg()
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=_oai_response(json.dumps({"text": "ok response", "confidence": 0.8})),
        )

    respx_mock.post("https://llm.test/v1/chat/completions").mock(side_effect=handler)
    provider = LlamaCppProvider(cfg)
    try:
        await provider.complete(
            "drafting", "sys", [{"role": "user", "content": "x"}], response_format=_Schema
        )
    finally:
        await provider.aclose()

    body = captured[0]
    assert body["model"] == "qwen3-coder-next"
    assert body["temperature"] == 0.5
    assert body["max_tokens"] == 1500
    rf = body["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "_Schema"
    schema = rf["json_schema"]["schema"]
    assert "text" in schema["properties"]
    assert "confidence" in schema["properties"]


@pytest.mark.asyncio
async def test_llamacpp_falls_back_to_json_object_on_400(respx_mock, monkeypatch):
    """Older llama.cpp builds reject json_schema. Provider retries once with
    the looser json_object form before bubbling the error."""
    monkeypatch.setenv("LLM_API_KEY", "k")
    cfg = _llm_cfg()
    captured: list[dict] = []
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        captured.append(json.loads(request.content))
        call_count += 1
        if call_count == 1:
            return httpx.Response(400, text="unsupported response_format type")
        return httpx.Response(
            200,
            json=_oai_response(json.dumps({"text": "ok response", "confidence": 0.8})),
        )

    respx_mock.post("https://llm.test/v1/chat/completions").mock(side_effect=handler)
    provider = LlamaCppProvider(cfg)
    try:
        resp = await provider.complete(
            "drafting", "sys", [{"role": "user", "content": "x"}], response_format=_Schema
        )
    finally:
        await provider.aclose()

    assert resp.provider == "llamacpp"
    assert call_count == 2
    assert captured[0]["response_format"]["type"] == "json_schema"
    assert captured[1]["response_format"]["type"] == "json_object"


@pytest.mark.asyncio
async def test_llamacpp_no_response_format_when_no_schema(respx_mock, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "k")
    cfg = _llm_cfg()
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=_oai_response("a long enough freeform reply with no json"),
        )

    respx_mock.post("https://llm.test/v1/chat/completions").mock(side_effect=handler)
    provider = LlamaCppProvider(cfg)
    try:
        await provider.complete("drafting", "sys", [{"role": "user", "content": "x"}])
    finally:
        await provider.aclose()
    assert "response_format" not in captured[0]


# ---------- error taxonomy + fallback ---------------------------------------


@pytest.mark.asyncio
async def test_llamacpp_5xx_falls_back_to_claude(respx_mock, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "k")
    cfg = _llm_cfg()
    respx_mock.post("https://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(503, text="upstream unavailable")
    )
    primary = LlamaCppProvider(cfg)
    fake_claude = _StubClaude(cfg, content=json.dumps({"text": "saved", "confidence": 0.5}))
    fb = FallbackProvider(primary=primary, fallback=fake_claude)
    try:
        resp = await fb.complete(
            "drafting", "sys", [{"role": "user", "content": "x"}], response_format=_Schema
        )
    finally:
        await primary.aclose()
    assert resp.provider == "claude"
    assert resp.fallback_used is True
    assert fake_claude.calls == 1


@pytest.mark.asyncio
async def test_llamacpp_timeout_falls_back(respx_mock, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "k")
    cfg = _llm_cfg()
    respx_mock.post("https://llm.test/v1/chat/completions").mock(
        side_effect=httpx.ReadTimeout("slow")
    )
    primary = LlamaCppProvider(cfg)
    fake_claude = _StubClaude(cfg, content=json.dumps({"text": "ok", "confidence": 1.0}))
    fb = FallbackProvider(primary=primary, fallback=fake_claude)
    try:
        resp = await fb.complete(
            "drafting", "sys", [{"role": "user", "content": "x"}], response_format=_Schema
        )
    finally:
        await primary.aclose()
    assert resp.provider == "claude"
    assert resp.fallback_used is True


@pytest.mark.asyncio
async def test_llamacpp_schema_mismatch_falls_back(respx_mock, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "k")
    cfg = _llm_cfg()
    respx_mock.post("https://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, json=_oai_response(json.dumps({"text": "missing confidence"}))
        )
    )
    primary = LlamaCppProvider(cfg)
    fake_claude = _StubClaude(cfg, content=json.dumps({"text": "saved", "confidence": 0.5}))
    fb = FallbackProvider(primary=primary, fallback=fake_claude)
    try:
        resp = await fb.complete(
            "drafting", "sys", [{"role": "user", "content": "x"}], response_format=_Schema
        )
    finally:
        await primary.aclose()
    assert resp.provider == "claude"
    assert resp.fallback_used is True


@pytest.mark.asyncio
async def test_llamacpp_auth_error_does_not_fall_back(respx_mock, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "wrong-key")
    cfg = _llm_cfg()
    respx_mock.post("https://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(401, text="invalid api key")
    )
    primary = LlamaCppProvider(cfg)
    fake_claude = _AssertNotCalledClaude(cfg)
    fb = FallbackProvider(primary=primary, fallback=fake_claude)
    try:
        with pytest.raises(LLMAuthError):
            await fb.complete(
                "drafting", "sys", [{"role": "user", "content": "x"}], response_format=_Schema
            )
    finally:
        await primary.aclose()
    assert fake_claude.calls == 0


# ---------- probe_llamacpp --------------------------------------------------


@pytest.mark.asyncio
async def test_probe_llamacpp_returns_true_on_200(respx_mock, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "k")
    cfg = _llm_cfg()
    respx_mock.get("https://llm.test/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "qwen3-coder-next"}, {"id": "another-model"}]},
        )
    )
    reachable, detail = await probe_llamacpp(cfg)
    assert reachable is True
    assert "2 models" in detail


@pytest.mark.asyncio
async def test_probe_llamacpp_returns_false_on_401(respx_mock, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "wrong")
    cfg = _llm_cfg()
    respx_mock.get("https://llm.test/v1/models").mock(
        return_value=httpx.Response(401, text="bad key")
    )
    reachable, detail = await probe_llamacpp(cfg)
    assert reachable is False
    assert "LLM_API_KEY" in detail


@pytest.mark.asyncio
async def test_probe_llamacpp_returns_false_on_timeout(respx_mock, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "k")
    cfg = _llm_cfg()
    respx_mock.get("https://llm.test/v1/models").mock(side_effect=httpx.ReadTimeout("slow"))
    reachable, detail = await probe_llamacpp(cfg, timeout_seconds=0.1)
    assert reachable is False
    assert "timeout" in detail.lower()


# ---------- build_provider --------------------------------------------------


def test_build_provider_dispatches_llamacpp(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "k")
    cfg = _llm_cfg()
    fake_claude = _StubClaude(cfg, content=json.dumps({"text": "x", "confidence": 0.5}))
    p = build_provider(cfg, claude=fake_claude)
    assert p.name == "fallback"
    assert isinstance(p.primary, LlamaCppProvider)


def test_llamacpp_config_required_when_provider_selected():
    """If provider=llamacpp but no llamacpp block, LLMConfig validation fails."""
    from pydantic import ValidationError as PydValidationError

    with pytest.raises(PydValidationError, match="llamacpp"):
        LLMConfig(
            provider="llamacpp",
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


# ---------- Stubs (mirror test_provider_fallback.py) -----------------------


class _FakeAnthropic:
    class _Messages:
        async def create(self, **kw):
            raise AssertionError("FakeAnthropic.messages.create should never be called")

    def __init__(self):
        self.messages = self._Messages()


class _StubClaude(ClaudeProvider):
    def __init__(self, cfg: LLMConfig, content: str, in_tok: int = 100, out_tok: int = 50):
        super().__init__(cfg, client=_FakeAnthropic())
        self._content = content
        self._in = in_tok
        self._out = out_tok
        self.calls = 0
        self.last_task: str | None = None

    async def complete(  # type: ignore[override]
        self, task, system, messages, response_format=None, max_tokens=1500
    ) -> LLMResponse:
        self.calls += 1
        self.last_task = task
        from wire.llm.budget import estimate_cost_usd
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

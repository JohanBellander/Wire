"""LLM provider abstraction. See SPEC.MD §3.

Three concrete providers:

  ClaudeProvider    — anthropic SDK with per-task model routing.
  LlamaCppProvider  — OpenAI-compatible /v1/chat/completions (llama.cpp, vLLM,
                       OpenRouter, etc.) with Bearer auth + JSON-schema enforced
                       structured output.
  FallbackProvider  — wraps a local primary with Claude as automatic fallback.

build_provider() picks the right shape from the LLMConfig toggle.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx
import structlog
from anthropic import AsyncAnthropic
from pydantic import BaseModel, ValidationError
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from wire.config import LLMConfig
from wire.llm.budget import estimate_cost_usd

log = structlog.get_logger()

TaskType = Literal["drafting", "triage", "voice_profile", "digest", "persona"]


@dataclass
class LLMResponse:
    """What every provider returns."""

    content: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    latency_ms: int
    cost_usd: float
    fallback_used: bool = False
    task: TaskType = "drafting"


class LLMProvider(Protocol):
    async def complete(
        self,
        task: TaskType,
        system: str | list[dict],
        messages: list[dict],
        response_format: type[BaseModel] | None = None,
        max_tokens: int = 1500,
    ) -> LLMResponse: ...


# --- exceptions ---------------------------------------------------------------


class LLMError(Exception):
    """Base for provider failures."""


class LLMTransientError(LLMError):
    """Retriable: timeouts, transient HTTP, network blips."""


class LLMSchemaError(LLMError):
    """Output failed JSON parse or schema validation."""


class LLMAuthError(LLMError):
    """Auth-related — never retry."""


# --- helpers ------------------------------------------------------------------


def parse_json_lenient(s: str) -> Any:
    """Parse JSON tolerantly. Handles three common LLM output quirks:

      1. Markdown code fences: ```json\\n{...}\\n```
      2. Leading/trailing prose: "Here is the JSON: { ... }"
      3. Trailing prose after a valid object closes.

    Falls back to extracting the first balanced {...} or [...] span if direct
    parse fails. Raises json.JSONDecodeError if no recoverable JSON is found.
    """
    text = s.strip()

    # Strip markdown code fences (```json ... ``` or ``` ... ```)
    if text.startswith("```"):
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1 :]
        text = text.rstrip()
        if text.endswith("```"):
            text = text[:-3].rstrip()

    # Try the simple case first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fall back: extract the first balanced top-level {...} or [...]
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue

    raise json.JSONDecodeError("no recoverable JSON in response", text or "", 0)


def _validate_output(output: str, response_format: type[BaseModel] | None) -> None:
    """Raises LLMSchemaError if output is empty / too short / unparseable /
    fails schema validation. Otherwise returns silently."""
    if not output or len(output.strip()) < 20:
        raise LLMSchemaError("response too short")
    if response_format is None:
        return
    try:
        parsed = parse_json_lenient(output)
    except json.JSONDecodeError as e:
        raise LLMSchemaError(f"response was not valid JSON: {e}") from e
    try:
        response_format.model_validate(parsed)
    except ValidationError as e:
        raise LLMSchemaError(f"response failed schema validation: {e}") from e


def _normalize_system(system: str | list[dict]) -> list[dict]:
    """The Anthropic Messages API accepts either a string or a list of content
    blocks for the system prompt. Lift strings to a single-block list so we can
    apply cache_control uniformly when caching is enabled."""
    if isinstance(system, str):
        return [{"type": "text", "text": system}]
    return system


# --- ClaudeProvider -----------------------------------------------------------


class ClaudeProvider:
    def __init__(
        self,
        config: LLMConfig,
        client: AsyncAnthropic | None = None,
    ) -> None:
        self._config = config
        self._client = client or AsyncAnthropic()
        self._models: dict[TaskType, str] = {
            "drafting": config.claude.drafting,
            "triage": config.claude.triage,
            "voice_profile": config.claude.voice_profile,
            "digest": config.claude.digest,
        }
        self._prompt_caching = config.prompt_caching

    @property
    def name(self) -> str:
        return "claude"

    def _system_blocks(self, system: str | list[dict]) -> list[dict]:
        blocks = _normalize_system(system)
        if self._prompt_caching and blocks:
            # Last block carries the cache breakpoint.
            *head, last = blocks
            tagged_last = {**last, "cache_control": {"type": "ephemeral"}}
            return [*head, tagged_last]
        return blocks

    async def complete(
        self,
        task: TaskType,
        system: str | list[dict],
        messages: list[dict],
        response_format: type[BaseModel] | None = None,
        max_tokens: int = 1500,
    ) -> LLMResponse:
        from anthropic import APIStatusError, APITimeoutError, AuthenticationError

        model = self._models[task]
        sys_blocks = self._system_blocks(system)

        async def _call() -> LLMResponse:
            t0 = time.perf_counter()
            resp = await self._client.messages.create(
                model=model,
                system=sys_blocks,
                messages=messages,
                max_tokens=max_tokens,
            )
            latency = int((time.perf_counter() - t0) * 1000)
            text = "".join(
                block.text for block in resp.content if getattr(block, "type", "") == "text"
            )
            usage = resp.usage
            in_t = getattr(usage, "input_tokens", 0) or 0
            out_t = getattr(usage, "output_tokens", 0) or 0
            cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
            cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
            _validate_output(text, response_format)
            return LLMResponse(
                content=text,
                provider="claude",
                model=model,
                input_tokens=in_t,
                output_tokens=out_t,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                latency_ms=latency,
                cost_usd=estimate_cost_usd(model, in_t, out_t, cache_read, cache_write),
                task=task,
            )

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=1, min=1, max=8),
                retry=retry_if_exception_type((APITimeoutError, httpx.TransportError)),
                reraise=True,
            ):
                with attempt:
                    return await _call()
        except AuthenticationError as e:
            raise LLMAuthError(f"Claude auth failed: {e}") from e
        except APIStatusError as e:
            # 5xx → transient (will only see this if retries exhausted), 4xx → fatal
            if 500 <= e.status_code < 600:
                raise LLMTransientError(f"Claude {e.status_code}: {e}") from e
            raise LLMError(f"Claude {e.status_code}: {e}") from e
        except httpx.TransportError as e:
            raise LLMTransientError(f"Claude transport error: {e}") from e
        # Unreachable: AsyncRetrying with reraise=True returns or raises.
        raise LLMError("retry loop exited unexpectedly")


# --- shared helpers used by LlamaCppProvider ----------------------------------


def _system_to_text(system: str | list[dict]) -> str:
    if isinstance(system, str):
        return system
    return "\n\n".join(b.get("text", "") for b in system if b.get("type") == "text")


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return str(content)


# --- LlamaCppProvider ---------------------------------------------------------


class LlamaCppProvider:
    """OpenAI-compatible local backend (llama.cpp server, vLLM, OpenRouter, ...).

    POSTs to `{base_url}/chat/completions` with Bearer auth. When a pydantic
    `response_format` is provided, sends the JSON Schema in the OpenAI
    `response_format: {"type": "json_schema", ...}` shape; falls back to
    `{"type": "json_object"}` if the server rejects the schema form.
    """

    def __init__(
        self,
        config: LLMConfig,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if config.llamacpp is None:
            raise ValueError(
                "LlamaCppProvider built without llm.llamacpp config — should be "
                "rejected upstream by LLMConfig validation."
            )
        self._cfg = config.llamacpp
        self._client = client
        self._owns_client = client is None
        self._api_key = os.environ.get(self._cfg.api_key_env, "") or ""

    @property
    def name(self) -> str:
        return "llamacpp"

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._cfg.timeout_seconds)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    @staticmethod
    def _flatten_messages(
        system: str | list[dict],
        messages: list[dict],
    ) -> list[dict[str, Any]]:
        """Convert Anthropic-shaped system/messages into OpenAI's flat
        list-of-{role, content}. Drops cache_control markers (OpenAI ignores)."""
        out: list[dict[str, Any]] = []
        sys_text = _system_to_text(system)
        if sys_text:
            out.append({"role": "system", "content": sys_text})
        for m in messages:
            out.append({"role": m["role"], "content": _content_to_text(m["content"])})
        return out

    def _build_response_format(
        self,
        response_format: type[BaseModel] | None,
    ) -> dict[str, Any] | None:
        if response_format is None:
            return None
        try:
            schema = response_format.model_json_schema()
        except Exception:  # noqa: BLE001 — defensive
            return {"type": "json_object"}
        return {
            "type": "json_schema",
            "json_schema": {
                "name": response_format.__name__,
                "schema": schema,
                "strict": True,
            },
        }

    async def complete(
        self,
        task: TaskType,
        system: str | list[dict],
        messages: list[dict],
        response_format: type[BaseModel] | None = None,
        max_tokens: int = 1500,
    ) -> LLMResponse:
        oai_messages = self._flatten_messages(system, messages)

        body: dict[str, Any] = {
            "model": self._cfg.model,
            "messages": oai_messages,
            "max_tokens": max_tokens,
            "temperature": self._cfg.temperature,
            "stream": False,
        }

        rf = self._build_response_format(response_format)
        if rf is not None:
            body["response_format"] = rf

        client = self._get_client()
        url = f"{self._cfg.base_url}/chat/completions"
        t0 = time.perf_counter()

        async def _post(req_body: dict[str, Any]) -> httpx.Response:
            return await client.post(
                url,
                json=req_body,
                headers=self._headers(),
                timeout=self._cfg.timeout_seconds,
            )

        try:
            resp = await _post(body)
        except httpx.TimeoutException as e:
            raise LLMTransientError(f"llamacpp timeout: {e}") from e
        except httpx.TransportError as e:
            raise LLMTransientError(f"llamacpp transport error: {e}") from e

        # Some llama.cpp builds reject json_schema but accept json_object.
        # Retry once with the looser shape so a stale server doesn't cascade
        # to Claude on every call.
        if resp.status_code == 400 and rf is not None and rf.get("type") == "json_schema":
            body["response_format"] = {"type": "json_object"}
            try:
                resp = await _post(body)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                raise LLMTransientError(f"llamacpp retry failed: {e}") from e

        latency = int((time.perf_counter() - t0) * 1000)

        if resp.status_code in (401, 403):
            raise LLMAuthError(
                f"llamacpp auth failed: HTTP {resp.status_code} "
                f"(check {self._cfg.api_key_env} env var)"
            )
        if resp.status_code >= 500:
            raise LLMTransientError(f"llamacpp HTTP {resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise LLMError(f"llamacpp HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            data = resp.json()
        except json.JSONDecodeError as e:
            raise LLMTransientError(f"llamacpp returned non-JSON: {e}") from e

        choices = data.get("choices") or []
        if not choices:
            raise LLMSchemaError("llamacpp response had no choices")
        msg = choices[0].get("message") or {}
        content = msg.get("content", "") or ""

        usage = data.get("usage") or {}
        in_t = int(usage.get("prompt_tokens", 0) or 0)
        out_t = int(usage.get("completion_tokens", 0) or 0)

        if not content or len(content.strip()) < 20:
            log.warning(
                "wire.llamacpp.short_response",
                task=task,
                model=self._cfg.model,
                content_len=len(content),
                content_preview=content[:80] or "(empty)",
                prompt_tokens=in_t,
                completion_tokens=out_t,
                finish_reason=choices[0].get("finish_reason"),
                has_response_format=response_format is not None,
            )

        _validate_output(content, response_format)
        return LLMResponse(
            content=content,
            provider="llamacpp",
            model=self._cfg.model,
            input_tokens=in_t,
            output_tokens=out_t,
            cache_read_tokens=0,
            cache_write_tokens=0,
            latency_ms=latency,
            cost_usd=0.0,
            task=task,
        )


# --- FallbackProvider ---------------------------------------------------------


class FallbackProvider:
    """Wraps a local primary (llama.cpp / OpenAI-compatible) with Claude as
    automatic fallback. Per SPEC §3, all tasks go to the local primary first;
    Claude is only invoked when the primary fails."""

    def __init__(
        self,
        primary: LlamaCppProvider,
        fallback: ClaudeProvider,
    ) -> None:
        self.primary = primary
        self.fallback = fallback

    @property
    def name(self) -> str:
        return "fallback"

    async def complete(
        self,
        task: TaskType,
        system: str | list[dict],
        messages: list[dict],
        response_format: type[BaseModel] | None = None,
        max_tokens: int = 1500,
    ) -> LLMResponse:
        try:
            resp = await self.primary.complete(task, system, messages, response_format, max_tokens)
            return resp
        except LLMAuthError:
            # Auth errors are configuration bugs — surfacing them through the
            # fallback would mask the real problem. Re-raise.
            raise
        except (LLMTransientError, LLMSchemaError, LLMError) as e:
            log.warning(
                "wire.llm.fallback",
                task=task,
                primary=self.primary.name,
                primary_error=str(e),
                error_type=type(e).__name__,
            )

        # Fall through: invoke Claude.
        resp = await self.fallback.complete(task, system, messages, response_format, max_tokens)
        resp.fallback_used = True
        return resp


# --- factory ------------------------------------------------------------------


async def probe_llamacpp(config: LLMConfig, *, timeout_seconds: float = 5.0) -> tuple[bool, str]:
    """Soft reachability probe for the configured llama.cpp / OpenAI-compat
    host. Hits `GET /models` (the standard OpenAI listing endpoint).

    Returns (reachable, detail). Never raises."""
    if config.llamacpp is None:
        return False, "no llamacpp config"
    base = config.llamacpp.base_url
    url = f"{base}/models"
    api_key = os.environ.get(config.llamacpp.api_key_env, "") or ""
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code in (401, 403):
            return False, f"HTTP {resp.status_code} (check {config.llamacpp.api_key_env})"
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}"
        data = resp.json()
        models = data.get("data", []) if isinstance(data, dict) else []
        return True, f"{len(models)} models available"
    except httpx.TimeoutException:
        return False, f"timeout after {timeout_seconds}s"
    except httpx.TransportError as e:
        return False, f"transport error: {e}"
    except Exception as e:  # noqa: BLE001 — defensive
        return False, f"{type(e).__name__}: {e}"


def build_provider(
    config: LLMConfig,
    *,
    claude: ClaudeProvider | None = None,
    llamacpp: LlamaCppProvider | None = None,
) -> LLMProvider:
    """Build the right provider tree. Optional injected providers are used
    by tests; production passes none."""
    claude_p = claude or ClaudeProvider(config)
    if config.provider == "claude":
        return claude_p
    if config.provider == "llamacpp":
        llamacpp_p = llamacpp or LlamaCppProvider(config)
        return FallbackProvider(primary=llamacpp_p, fallback=claude_p)
    raise ValueError(f"Unknown provider: {config.provider}")

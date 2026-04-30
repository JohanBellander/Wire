"""LLM provider abstraction. See SPEC.MD §3.

Three concrete providers:

  ClaudeProvider    — anthropic SDK with per-task model routing.
  OllamaProvider    — POST /api/chat to a self-hosted host, format: json.
  FallbackProvider  — wraps Ollama with Claude as automatic fallback.

build_provider() picks the right shape from the LLMConfig toggle.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import httpx
import structlog
from anthropic import AsyncAnthropic
from pydantic import BaseModel, ValidationError
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from wire.config import LLMConfig
from wire.llm.budget import estimate_cost_usd

log = structlog.get_logger()

TaskType = Literal["drafting", "triage", "voice_profile", "digest"]


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


def _validate_output(output: str, response_format: type[BaseModel] | None) -> None:
    """Raises LLMSchemaError if output is empty / too short / unparseable /
    fails schema validation. Otherwise returns silently."""
    if not output or len(output.strip()) < 20:
        raise LLMSchemaError("response too short")
    if response_format is None:
        return
    try:
        parsed = json.loads(output)
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
                cost_usd=estimate_cost_usd(
                    model, in_t, out_t, cache_read, cache_write
                ),
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


# --- OllamaProvider -----------------------------------------------------------


class OllamaProvider:
    def __init__(
        self,
        config: LLMConfig,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._cfg = config.ollama
        self._client = client
        self._owns_client = client is None

    @property
    def name(self) -> str:
        return "ollama"

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._cfg.timeout_seconds)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def complete(
        self,
        task: TaskType,
        system: str | list[dict],
        messages: list[dict],
        response_format: type[BaseModel] | None = None,
        max_tokens: int = 1500,
    ) -> LLMResponse:
        # Flatten Anthropic-shaped content blocks if we get them.
        sys_text = _system_to_text(system)
        ollama_messages: list[dict[str, Any]] = []
        if sys_text:
            ollama_messages.append({"role": "system", "content": sys_text})
        for m in messages:
            ollama_messages.append({"role": m["role"], "content": _content_to_text(m["content"])})

        body: dict[str, Any] = {
            "model": self._cfg.model,
            "messages": ollama_messages,
            "stream": False,
            "options": {"num_predict": max_tokens},
        }
        if response_format is not None:
            body["format"] = "json"

        client = self._get_client()
        t0 = time.perf_counter()
        try:
            resp = await client.post(
                f"{self._cfg.base_url}/api/chat",
                json=body,
                timeout=self._cfg.timeout_seconds,
            )
        except httpx.TimeoutException as e:
            raise LLMTransientError(f"Ollama timeout: {e}") from e
        except httpx.TransportError as e:
            raise LLMTransientError(f"Ollama transport error: {e}") from e
        latency = int((time.perf_counter() - t0) * 1000)

        if resp.status_code == 401 or resp.status_code == 403:
            raise LLMAuthError(f"Ollama auth failed: {resp.status_code}")
        if resp.status_code >= 400:
            raise LLMTransientError(f"Ollama HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            data = resp.json()
        except json.JSONDecodeError as e:
            raise LLMTransientError(f"Ollama returned non-JSON: {e}") from e

        content = data.get("message", {}).get("content", "")
        in_t = int(data.get("prompt_eval_count", 0))
        out_t = int(data.get("eval_count", 0))
        _validate_output(content, response_format)
        return LLMResponse(
            content=content,
            provider="ollama",
            model=self._cfg.model,
            input_tokens=in_t,
            output_tokens=out_t,
            cache_read_tokens=0,
            cache_write_tokens=0,
            latency_ms=latency,
            cost_usd=0.0,
            task=task,
        )


def _system_to_text(system: str | list[dict]) -> str:
    if isinstance(system, str):
        return system
    return "\n\n".join(b.get("text", "") for b in system if b.get("type") == "text")


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return str(content)


# --- FallbackProvider ---------------------------------------------------------


class FallbackProvider:
    """Wraps Ollama with Claude as automatic fallback. Per SPEC §3, all tasks
    go to Ollama first; Claude is only invoked when Ollama fails."""

    def __init__(self, primary: OllamaProvider, fallback: ClaudeProvider) -> None:
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
            resp = await self.primary.complete(
                task, system, messages, response_format, max_tokens
            )
            return resp
        except LLMAuthError:
            # Auth errors are configuration bugs — surfacing them through the
            # fallback would mask the real problem. Re-raise.
            raise
        except (LLMTransientError, LLMSchemaError, LLMError) as e:
            log.warning(
                "wire.llm.fallback",
                task=task,
                ollama_error=str(e),
                error_type=type(e).__name__,
            )

        # Fall through: invoke Claude.
        resp = await self.fallback.complete(
            task, system, messages, response_format, max_tokens
        )
        resp.fallback_used = True
        return resp


# --- factory ------------------------------------------------------------------


def build_provider(
    config: LLMConfig,
    *,
    claude: ClaudeProvider | None = None,
    ollama: OllamaProvider | None = None,
) -> LLMProvider:
    """Build the right provider tree. Optional injected providers are used
    by tests; production passes neither."""
    claude_p = claude or ClaudeProvider(config)
    if config.provider == "claude":
        return claude_p
    if config.provider == "ollama":
        ollama_p = ollama or OllamaProvider(config)
        return FallbackProvider(primary=ollama_p, fallback=claude_p)
    raise ValueError(f"Unknown provider: {config.provider}")

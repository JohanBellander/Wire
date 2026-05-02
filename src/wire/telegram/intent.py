"""Natural-language intent classifier for Telegram free-text messages.

The user types things like "what's the status" or "show last 10 events"
instead of typing slash commands. This module asks the configured LLM
provider (local model first, Claude only as fallback per `FallbackProvider`)
to classify the message into one of a fixed set of intents and extract any
arguments.

Best-effort by design — any failure (timeout, bad JSON, schema mismatch,
budget cap, provider not wired) returns `intent="unknown"` so the caller
replies with a static fallback rather than crashing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field

from wire.config import WireConfig
from wire.llm.alerts import is_drafting_blocked_by_budget
from wire.llm.budget import log_llm_call
from wire.llm.provider import LLMProvider, parse_json_lenient

log = structlog.get_logger()

INTENT_PROMPT_PATH = Path(__file__).resolve().parents[1] / "llm" / "prompts" / "intent.txt"

IntentName = Literal[
    "status",
    "budget",
    "resume",
    "saved",
    "digest",
    "repos",
    "extend",
    "last",
    "draft",
    "help",
    "draft_revise",
    "unknown",
]


class ClassifiedIntent(BaseModel):
    intent: IntentName
    args: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


def _load_prompt() -> str:
    return INTENT_PROMPT_PATH.read_text(encoding="utf-8")


_UNKNOWN = ClassifiedIntent(intent="unknown", args={}, confidence=0.0)


async def classify(
    text: str,
    cfg: WireConfig,
    provider: LLMProvider | None,
) -> ClassifiedIntent:
    """Classify a free-text Telegram message into an intent + args.

    Returns `intent="unknown"` (rather than raising) on any failure path so
    the caller can reply with a static fallback. The local model handles
    the call first; Claude is only used if the local model fails (via
    FallbackProvider). Cost is tiny — short input, short structured output.
    """
    if provider is None:
        return _UNKNOWN
    cleaned = text.strip()
    if not cleaned:
        return _UNKNOWN
    if is_drafting_blocked_by_budget(cfg):
        # If we're at the cap, fall back to "unknown" rather than burning more
        # tokens on classification. The user can still use /pause and /help.
        return _UNKNOWN

    system = _load_prompt()
    user = f"User message: {cleaned!r}\n\nClassify it. Respond as JSON."

    try:
        resp = await provider.complete(
            task=cfg.persona.model_task,
            system=system,
            messages=[{"role": "user", "content": user}],
            response_format=ClassifiedIntent,
            max_tokens=200,
        )
    except Exception as e:  # noqa: BLE001 — must never crash the message handler
        log.warning("wire.intent.llm_failed", error=str(e), error_type=type(e).__name__)
        return _UNKNOWN

    resp.task = "persona"  # bucket cost with the other Telegram-side LLM calls
    log_llm_call(resp)

    try:
        parsed = parse_json_lenient(resp.content)
        validated = ClassifiedIntent.model_validate(parsed)
    except Exception as e:  # noqa: BLE001
        log.warning("wire.intent.parse_failed", error=str(e), raw=resp.content[:200])
        return _UNKNOWN

    log.info(
        "wire.intent.classified",
        intent=validated.intent,
        confidence=validated.confidence,
        args=validated.args,
    )
    return validated

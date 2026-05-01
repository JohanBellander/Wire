"""LLM-driven persona pass for Telegram messages.

Two surfaces use this module:

  intro_for_draft(...)  — one-line intro prepended above a draft preview.
  frame_digest(...)     — opener + closer wrapped around the weekly digest.

Both are *additive* and best-effort. On any failure (LLM error, persona
disabled, quiet hours, budget cap, schema validation), they return None and
the caller falls back to a static line. We never block a draft or a digest
on a persona pass.

The underlying LLM call routes through `cfg.persona.model_task` (default:
`triage`, which is Haiku). Cost per call is fractions of a cent. Calls are
logged to `llm_calls` with task type `"persona"` so /budget and /status
can break out persona spend separately.
"""

from __future__ import annotations

from pathlib import Path

import structlog
from pydantic import BaseModel, Field

from wire.config import WireConfig
from wire.drafting.drafter import is_in_quiet_hours
from wire.llm.alerts import is_drafting_blocked_by_budget
from wire.llm.budget import log_llm_call
from wire.llm.provider import LLMProvider

log = structlog.get_logger()

PERSONA_PROMPT_PATH = Path(__file__).resolve().parents[1] / "llm" / "prompts" / "persona.txt"


def _load_persona() -> str:
    """Read persona.txt fresh each call — same pattern as drafting/voice_profile.
    No caching: persona is rarely called and the file is tiny."""
    return PERSONA_PROMPT_PATH.read_text(encoding="utf-8")


# --- response schemas --------------------------------------------------------


class IntroResponse(BaseModel):
    """One-line intro for a draft preview."""

    intro: str = Field(min_length=1, max_length=160)


class DigestFrame(BaseModel):
    """Opener + closer wrapped around the weekly digest stats."""

    opener: str = Field(min_length=1, max_length=300)
    closer: str = Field(min_length=1, max_length=160)


# --- public API --------------------------------------------------------------


async def intro_for_draft(
    cfg: WireConfig,
    provider: LLMProvider | None,
    *,
    thread_text: str,
    repo: str,
) -> str | None:
    """Generate a one-line persona intro for a draft preview.

    Returns None (so callers fall back to static) when:
      - persona disabled or LLM intro disabled in config
      - provider not wired
      - currently in quiet hours
      - monthly budget cap hit
      - LLM call fails or returns invalid output
    """
    if not cfg.persona.enabled or not cfg.persona.llm_intro_on_drafts:
        return None
    if provider is None:
        return None
    if is_in_quiet_hours(cfg.quiet_hours):
        return None
    if is_drafting_blocked_by_budget(cfg):
        return None

    system = _load_persona()
    user = (
        "A draft tweet just landed for review. Write ONE short intro line for "
        "Johan above the draft. Present-tense, small flourish, no emoji unless "
        "it lands hard. Do not summarize the draft. Do not quote it. Do not "
        "repeat the repo name. Keep under 80 characters.\n\n"
        f"Repo: {repo}\n"
        f"Draft body:\n{thread_text}\n\n"
        'Respond as JSON: {"intro": "..."}'
    )

    try:
        resp = await provider.complete(
            task=cfg.persona.model_task,
            system=system,
            messages=[{"role": "user", "content": user}],
            response_format=IntroResponse,
            max_tokens=200,
        )
    except Exception as e:  # noqa: BLE001 — persona must never break drafts
        log.warning("wire.persona.intro_failed", error=str(e), error_type=type(e).__name__)
        return None

    resp.task = "persona"
    log_llm_call(resp)

    try:
        from wire.llm.provider import parse_json_lenient

        parsed = parse_json_lenient(resp.content)
        validated = IntroResponse.model_validate(parsed)
    except Exception as e:  # noqa: BLE001
        log.warning("wire.persona.intro_parse_failed", error=str(e))
        return None

    return validated.intro.strip() or None


async def frame_digest(
    cfg: WireConfig,
    provider: LLMProvider | None,
    *,
    stats_block: str,
) -> tuple[str, str] | None:
    """Generate (opener, closer) lines for the weekly digest.

    Same fallback semantics as intro_for_draft. Quiet hours don't apply here
    — the digest fires on its own cron (Monday 09:00 Stockholm) which is
    already outside any reasonable quiet-hours window.
    """
    if not cfg.persona.enabled or not cfg.persona.llm_frame_on_digest:
        return None
    if provider is None:
        return None
    if is_drafting_blocked_by_budget(cfg):
        return None

    system = _load_persona()
    user = (
        "Wire's weekly digest is going out to Johan. Write a short, "
        "retrospective opener (1-3 lines, slightly poetic, mood-tinted on "
        "the numbers below) and a short closing sign-off (one line). "
        "Preserve every number verbatim — don't restate them, just frame "
        "them. No emoji unless the numbers earn it.\n\n"
        f"Stats block:\n{stats_block}\n\n"
        'Respond as JSON: {"opener": "...", "closer": "..."}'
    )

    try:
        resp = await provider.complete(
            task=cfg.persona.model_task,
            system=system,
            messages=[{"role": "user", "content": user}],
            response_format=DigestFrame,
            max_tokens=400,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("wire.persona.digest_failed", error=str(e), error_type=type(e).__name__)
        return None

    resp.task = "persona"
    log_llm_call(resp)

    try:
        from wire.llm.provider import parse_json_lenient

        parsed = parse_json_lenient(resp.content)
        validated = DigestFrame.model_validate(parsed)
    except Exception as e:  # noqa: BLE001
        log.warning("wire.persona.digest_parse_failed", error=str(e))
        return None

    opener = validated.opener.strip()
    closer = validated.closer.strip()
    if not opener or not closer:
        return None
    return opener, closer

"""Voice profile generator. Per SPEC §13.

Regenerated weekly from the user's posted content (in-DB tweets posted by the
bot, plus optionally a one-time seed of older tweets via seed_voice). The
profile is a short text block injected into the drafting prompt — it tracks
the user, can't drift away from them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

from wire.config import WireConfig
from wire.db import session as db_session
from wire.db.models import Post, VoiceProfile, utc_now
from wire.llm.budget import log_llm_call
from wire.llm.provider import LLMError, LLMProvider, LLMResponse, parse_json_lenient

log = structlog.get_logger()

PROMPT_PATH = Path(__file__).resolve().parents[1] / "llm" / "prompts" / "voice_profile.txt"


def _system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


class _VoiceResponse(BaseModel):
    profile_text: str = Field(min_length=20, max_length=2000)


def _gather_recent_posts(limit: int) -> list[str]:
    with db_session.session_scope() as sa:
        rows = sa.execute(
            select(Post).order_by(desc(Post.posted_at)).limit(limit)
        ).scalars().all()
    return [p.text for p in rows]


async def regenerate_voice_profile(
    cfg: WireConfig,
    provider: LLMProvider,
    *,
    posts_override: list[str] | None = None,
) -> str | None:
    posts = posts_override if posts_override is not None else _gather_recent_posts(cfg.learning.recent_posts_n)
    if not posts:
        log.info("wire.voice.no_posts")
        return None

    user_msg = "Recent posts (newest first):\n\n" + "\n\n---\n\n".join(posts[:100])
    try:
        resp = await provider.complete(
            task="voice_profile",
            system=_system_prompt(),
            messages=[{"role": "user", "content": user_msg}],
            response_format=_VoiceResponse,
            max_tokens=600,
        )
    except LLMError as e:
        log.warning("wire.voice.llm_failed", error=str(e))
        return None

    log_llm_call(resp)
    parsed = _VoiceResponse.model_validate(parse_json_lenient(resp.content))
    with db_session.session_scope() as sa:
        sa.add(VoiceProfile(profile_text=parsed.profile_text))
    log.info("wire.voice.regenerated", words=len(parsed.profile_text.split()))
    return parsed.profile_text

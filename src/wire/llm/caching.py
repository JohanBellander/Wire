"""Helpers for tagging system / context blocks with anthropic cache_control.

Strategy (SPEC §8):
  * System + voice profile → cache 1h
  * Recent posts + decisions → cache 5min
  * Session events → never cached

The provider already tags the *last* system block with cache_control when
prompt_caching is enabled. The drafter (step 7) calls into here to assemble
multi-block system prompts with explicit ttls.
"""

from __future__ import annotations


def text_block(text: str, *, cache_ttl: str | None = None) -> dict:
    """Return an Anthropic content block. cache_ttl ∈ {None, '5m', '1h'}.

    The Messages API's cache_control accepts {"type": "ephemeral"} (5 min) or
    {"type": "ephemeral", "ttl": "1h"} for the 1-hour beta.
    """
    block: dict = {"type": "text", "text": text}
    if cache_ttl is None:
        return block
    cc: dict = {"type": "ephemeral"}
    if cache_ttl == "1h":
        cc["ttl"] = "1h"
    block["cache_control"] = cc
    return block

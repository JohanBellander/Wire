"""Fetch + cache per-repo README text for injection into the drafting prompt.

Cached in `bot_state` under key prefix `readme:{repo}`. Refreshed weekly by
the scheduler; fetched on demand by the poller when first encountered for
a given repo (best-effort — failure never blocks ingestion).

Cleaning pipeline:
  1. Strip standalone image / badge lines (no useful textual context for
     the LLM, just visual noise).
  2. Collapse 3+ blank lines into 2.
  3. Truncate to MAX_README_CHARS, preferring a paragraph boundary.

The result lands as a 1h-cached system block in the drafting prompt, so the
extra tokens cost ~$0.001/draft on Sonnet with cache hits — see
`drafter.build_prompt_blocks`.
"""

from __future__ import annotations

import re

import structlog

from wire.config import ReposFile
from wire.db import session as db_session
from wire.db.models import BotState, utc_now
from wire.ingestion.github_client import GitHubClient

log = structlog.get_logger()

_README_KEY_PREFIX = "readme:"
MAX_README_CHARS = 2000


# --- cleaning ---------------------------------------------------------------


_BADGE_PATTERNS = [
    # standalone image: ![alt](url)
    re.compile(r"^!\[[^\]]*\]\([^)]*\)\s*$", re.MULTILINE),
    # linked badge: [![alt](url)](url)
    re.compile(r"^\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)\s*$", re.MULTILINE),
    # standalone <img> tag (HTML in markdown)
    re.compile(r"^\s*<img[^>]*>\s*$", re.MULTILINE | re.IGNORECASE),
    # <a href><img></a> wrapped badge
    re.compile(
        r"^\s*<a[^>]*>\s*<img[^>]*>\s*</a>\s*$",
        re.MULTILINE | re.IGNORECASE,
    ),
]
_MULTI_BLANK = re.compile(r"\n{3,}")


def _strip_badges_and_images(text: str) -> str:
    for pat in _BADGE_PATTERNS:
        text = pat.sub("", text)
    text = _MULTI_BLANK.sub("\n\n", text)
    return text.strip()


def _truncate(text: str, *, limit: int = MAX_README_CHARS) -> str:
    """Trim to `limit` chars, breaking on a paragraph boundary if possible."""
    if len(text) <= limit:
        return text
    snippet = text[:limit]
    # Prefer cutting at a double-newline within the last 400 chars
    last_para = snippet.rfind("\n\n")
    if last_para > limit - 400:
        return snippet[:last_para].rstrip() + "\n\n[…truncated]"
    return snippet.rstrip() + "\n\n[…truncated]"


# --- bot_state plumbing -----------------------------------------------------


def get_cached_readme(repo: str) -> str | None:
    """Fetch the cached README text for a repo, or None if not cached."""
    with db_session.session_scope() as s:
        row = s.get(BotState, _README_KEY_PREFIX + repo)
        return row.value if row else None


def _store_readme(repo: str, text: str) -> None:
    key = _README_KEY_PREFIX + repo
    with db_session.session_scope() as s:
        row = s.get(BotState, key)
        if row is None:
            s.add(BotState(key=key, value=text))
        else:
            row.value = text
            row.updated_at = utc_now()


# --- public API -------------------------------------------------------------


async def fetch_and_cache_readme(client: GitHubClient, repo: str) -> str | None:
    """Fetch the README for `repo`, clean + truncate, store in bot_state.
    Returns the stored text (or None if the repo has no README / fetch failed)."""
    try:
        raw = await client.get_readme(repo)
    except Exception as e:
        log.warning("wire.readme.fetch_failed", repo=repo, error=str(e))
        return None
    if not raw:
        log.info("wire.readme.not_found", repo=repo)
        return None

    cleaned = _strip_badges_and_images(raw)
    truncated = _truncate(cleaned)
    _store_readme(repo, truncated)
    log.info(
        "wire.readme.cached",
        repo=repo,
        chars=len(truncated),
        was_truncated=len(cleaned) > MAX_README_CHARS,
    )
    return truncated


async def ensure_readme_cached(client: GitHubClient, repo: str) -> None:
    """If the repo has no cached README yet, fetch it. No-op otherwise."""
    if get_cached_readme(repo) is not None:
        return
    await fetch_and_cache_readme(client, repo)


async def refresh_all_readmes(client: GitHubClient, repos_file: ReposFile) -> int:
    """Fetch a fresh README for every allowlisted repo. Used by the weekly
    cron. Returns the count successfully cached."""
    n = 0
    for entry in repos_file.repos:
        result = await fetch_and_cache_readme(client, entry.name)
        if result is not None:
            n += 1
    return n

"""GitHub ingestion poller.

Runs every `poll_interval_minutes`. For each allowlisted repo:
  1. Fetch events since max(occurred_at) for that repo (or last 24h on first run).
  2. Normalize raw payloads into NormalizedEvents.
  3. Run the filter chain.
  4. Insert survivors into `events` (UNIQUE on github_id handles dedup).

Triage scoring (per-event Haiku call) lives in step 6 and is invoked by the
poller after this insert step.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from sqlalchemy import func, select

from wire.config import ReposFile, WireConfig
from wire.db import session as db_session
from wire.db.models import BotState, Event, utc_now
from wire.ingestion.filters import NormalizedEvent, apply_all, build_default_chain
from wire.ingestion.github_client import GitHubClient, normalize_raw_event

log = structlog.get_logger()


@dataclass
class IngestStats:
    repo: str
    fetched: int
    kept: int
    dropped: int
    inserted: int
    drop_reasons: dict[str, int]


def _last_event_at_for(repo: str) -> datetime | None:
    with db_session.session_scope() as s:
        return s.execute(select(func.max(Event.occurred_at)).where(Event.repo == repo)).scalar()


def _existing_github_ids(repo: str) -> set[str]:
    with db_session.session_scope() as s:
        rows = s.execute(select(Event.github_id).where(Event.repo == repo)).scalars().all()
    return set(rows)


_FIRST_RUN_KEY_PREFIX = "ingest_completed:"
_LAST_FETCHED_KEY_PREFIX = "last_fetched_at:"
# Defensive floor: if we somehow have no watermark and no events for a repo,
# never fetch more than this much history. Prevents back-flood when the
# first-run flag was set but the fetch high-water mark wasn't.
_MISSING_WATERMARK_FLOOR_HOURS = 24


def _is_first_run_for(repo: str) -> bool:
    """First run = we've never completed an ingestion cycle for this repo. Tracked
    via bot_state so a repo with no recent activity (everything dropped by the
    24h cutoff) doesn't get stuck re-applying the cutoff forever."""
    with db_session.session_scope() as s:
        return s.get(BotState, _FIRST_RUN_KEY_PREFIX + repo) is None


def _mark_first_run_done(repo: str) -> None:
    key = _FIRST_RUN_KEY_PREFIX + repo
    with db_session.session_scope() as s:
        if s.get(BotState, key) is None:
            s.add(BotState(key=key, value=utc_now().isoformat()))


def _get_last_fetched_at(repo: str) -> datetime | None:
    """Per-repo high-water mark for the GitHub /events fetch. Updated after
    every successful poll regardless of whether anything survived the filter
    chain — decouples 'what we've seen' from 'what's in the DB.'"""
    with db_session.session_scope() as s:
        row = s.get(BotState, _LAST_FETCHED_KEY_PREFIX + repo)
        if row is None:
            return None
        try:
            return datetime.fromisoformat(row.value)
        except ValueError:
            return None


def _set_last_fetched_at(repo: str, ts: datetime) -> None:
    key = _LAST_FETCHED_KEY_PREFIX + repo
    iso = ts.isoformat()
    with db_session.session_scope() as s:
        row = s.get(BotState, key)
        if row is None:
            s.add(BotState(key=key, value=iso))
        else:
            row.value = iso
            row.updated_at = utc_now()


def _resolve_since(repo: str, *, first_run: bool) -> datetime | None:
    """Decide what timestamp to pass to list_events for this repo.

    First-run path: since=None — fetch what's available, the filter chain's
        24h cutoff drops the backlog.
    Subsequent polls: prefer the per-repo watermark, fall back to the events
        table's max(occurred_at), and if both are missing apply a 24h floor
        so a misconfigured / stale state can't flood the bot with old events.
    """
    if first_run:
        return None
    watermark = _get_last_fetched_at(repo)
    if watermark is not None:
        return watermark
    fallback = _last_event_at_for(repo)
    if fallback is not None:
        return fallback
    return utc_now() - timedelta(hours=_MISSING_WATERMARK_FLOOR_HOURS)


_NULL_SHA = "0000000000000000000000000000000000000000"


async def _enrich_events(client: GitHubClient, repo: str, raw_events: list[dict]) -> list[dict]:
    """Backfill the bits of payload that GitHub's /events endpoint strips.

    PushEvent          → fetch commits via /compare/{before}...{head}
    PullRequestEvent   → fetch full PR via /pulls/{n}

    A failure on any single enrichment is logged and the event passes through
    with its stripped payload — partial info beats abandoning the poll.
    """
    pr_cache: dict[int, dict] = {}

    for r in raw_events:
        et = r.get("type")
        payload = r.get("payload") or {}

        if et == "PushEvent":
            before = payload.get("before")
            head = payload.get("head")
            if before and head and before != _NULL_SHA:
                try:
                    commits = await client.compare_commits(repo, before, head)
                except Exception as e:
                    log.warning("wire.enrich.push_failed", repo=repo, error=str(e))
                    commits = None
                if commits is not None:
                    payload["commits"] = [
                        {
                            "sha": c.get("sha"),
                            "message": (c.get("commit") or {}).get("message", ""),
                            "author": ((c.get("commit") or {}).get("author") or {}).get("name"),
                        }
                        for c in commits
                    ]

        elif et == "PullRequestEvent":
            pr = payload.get("pull_request") or {}
            number = pr.get("number")
            if number is not None:
                full_pr = pr_cache.get(number)
                if full_pr is None:
                    try:
                        full_pr = await client.get_pull_request(repo, int(number))
                    except Exception as e:
                        log.warning(
                            "wire.enrich.pr_failed",
                            repo=repo,
                            number=number,
                            error=str(e),
                        )
                        full_pr = None
                    if full_pr is not None:
                        pr_cache[number] = full_pr
                if full_pr is not None:
                    payload["pull_request"] = full_pr

    return raw_events


async def ingest_repo(
    client: GitHubClient,
    repo: str,
    config: WireConfig,
    repos_file: ReposFile,
) -> IngestStats:
    log_ = log.bind(repo=repo)
    first_run = _is_first_run_for(repo)
    since = _resolve_since(repo, first_run=first_run)
    log_.info("wire.ingestion.start", first_run=first_run, since=str(since))

    default_branch = await client.get_default_branch(repo)
    raw = await client.list_events(repo, since=since)
    raw = await _enrich_events(client, repo, raw)

    normalized: list[NormalizedEvent] = []
    for r in raw:
        n = normalize_raw_event(r, repo=repo, default_branch=default_branch, org=client.org)
        if n is not None:
            normalized.append(n)

    chain = build_default_chain(
        allowlist=repos_file.names(),
        skip_commit_patterns=config.ingestion.skip_commit_patterns,
        first_run=first_run,
        first_run_max_age_hours=config.ingestion.first_run_max_age_hours,
    )
    res = apply_all(normalized, chain)

    drop_counts: dict[str, int] = {}
    for _, reason in res.dropped:
        bucket = reason.split(":")[0]
        drop_counts[bucket] = drop_counts.get(bucket, 0) + 1

    inserted = _persist(res.kept, existing=_existing_github_ids(repo))

    # Whether or not anything was inserted, this poll completed — flip the
    # first-run flag so the next poll skips the 24h cutoff.
    _mark_first_run_done(repo)

    # Advance the watermark to the most recent event we *fetched* (not just
    # those that survived the filter chain). This decouples what-we've-seen
    # from what's-in-the-DB, so a repo that has activity but nothing makes
    # it through filters doesn't end up re-fetching the entire backlog on a
    # later poll where the filter outcome changes.
    if normalized:
        max_ts = max(n.occurred_at for n in normalized)
        _set_last_fetched_at(repo, max_ts)

    # Lazy: cache the README if we don't have one yet. Best-effort — never
    # blocks the poll. Weekly cron handles refreshes.
    try:
        from wire.ingestion.readme_fetcher import ensure_readme_cached

        await ensure_readme_cached(client, repo)
    except Exception:
        log.exception("wire.readme.ensure_failed", repo=repo)

    log_.info(
        "wire.ingestion.done",
        fetched=len(raw),
        normalized=len(normalized),
        kept=len(res.kept),
        dropped=len(res.dropped),
        inserted=inserted,
        drop_reasons=drop_counts,
    )
    return IngestStats(
        repo=repo,
        fetched=len(raw),
        kept=len(res.kept),
        dropped=len(res.dropped),
        inserted=inserted,
        drop_reasons=drop_counts,
    )


def _persist(events: Iterable[NormalizedEvent], existing: set[str]) -> int:
    """Insert kept events; skip ones whose github_id is already in events.
    UNIQUE constraint backs us up but we filter first to avoid IntegrityError
    noise in the logs."""
    rows = [e for e in events if e.github_id not in existing]
    if not rows:
        return 0
    with db_session.session_scope() as s:
        for e in rows:
            s.add(
                Event(
                    github_id=e.github_id,
                    repo=e.repo,
                    event_type=e.event_type,
                    actor=e.actor,
                    payload=e.payload or {},
                    occurred_at=e.occurred_at,
                )
            )
    return len(rows)


async def ingest_all(config: WireConfig, repos_file: ReposFile) -> list[IngestStats]:
    """Ingest all allowlisted repos, sequentially (rate-limit friendly).

    Sequential is fine for ≤ a few dozen repos at a 20-minute cadence. Bumping
    to a small concurrency cap is a one-line change if needed.
    """
    client = GitHubClient.from_files(
        app_id=config.github.app_id,
        installation_id=config.github.installation_id,
        private_key_path=config.github.private_key_path,
        org=config.github.org,
    )
    try:
        stats = []
        for repo_entry in repos_file.repos:
            try:
                s = await ingest_repo(client, repo_entry.name, config, repos_file)
                stats.append(s)
            except Exception as e:
                log.exception("wire.ingestion.repo_failed", repo=repo_entry.name, error=str(e))
        return stats
    finally:
        await client.aclose()

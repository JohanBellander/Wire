"""Pre-LLM event filters. Free, code-only — runs on the raw event stream
before anything reaches the database. Per SPEC.MD §7.1.

Each filter is a pure function over a normalized event dict. The poller
chains them; the first reject_reason short-circuits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

# Bot actors we never want to post about.
BOT_ACTORS: frozenset[str] = frozenset({
    "dependabot",
    "dependabot[bot]",
    "github-actions[bot]",
    "renovate[bot]",
    "renovate-bot",
    "imgbot[bot]",
})


@dataclass
class FilterDecision:
    keep: bool
    reason: str = ""


@dataclass
class NormalizedEvent:
    """The minimal shape every filter expects. The github_client builds these
    from raw GitHub API payloads and passes them in. Keeping a slim interface
    keeps filters testable without any HTTP plumbing."""

    github_id: str
    repo: str
    event_type: str  # PushEvent | PullRequestEvent | ReleaseEvent | IssuesEvent | etc
    actor: str | None
    occurred_at: datetime  # UTC, tz-aware preferred but naive accepted
    default_branch: str | None = None
    branch: str | None = None  # for PushEvent
    pr_merged: bool = False  # PRs to non-default branches are kept if merged
    commit_messages: list[str] | None = None  # for PushEvent
    payload: dict | None = None


def _ts(d: datetime) -> datetime:
    """Coerce to a naive-UTC datetime so subtractions never blow up on tz mix."""
    if d.tzinfo is not None:
        return d.astimezone(timezone.utc).replace(tzinfo=None)
    return d


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def filter_bot_actor(e: NormalizedEvent) -> FilterDecision:
    if e.actor and e.actor.lower() in BOT_ACTORS:
        return FilterDecision(False, f"bot actor: {e.actor}")
    return FilterDecision(True)


def filter_non_default_branch(e: NormalizedEvent) -> FilterDecision:
    """Skip events on branches other than default branch — except PR merges."""
    if e.event_type != "PushEvent":
        return FilterDecision(True)
    if e.default_branch is None or e.branch is None:
        return FilterDecision(True)
    if e.branch == e.default_branch:
        return FilterDecision(True)
    if e.pr_merged:
        return FilterDecision(True)
    return FilterDecision(False, f"non-default branch: {e.branch}")


def make_commit_message_filter(patterns: Iterable[str]):
    """Return a closure that rejects PushEvents whose commits all match
    one of the given conventional-commit prefixes (chore/ci/docs/style)."""
    compiled = [re.compile(p) for p in patterns]

    def f(e: NormalizedEvent) -> FilterDecision:
        if e.event_type != "PushEvent":
            return FilterDecision(True)
        if not e.commit_messages:
            return FilterDecision(True)
        all_skipped = all(any(p.match(m or "") for p in compiled) for m in e.commit_messages)
        if all_skipped:
            return FilterDecision(False, f"all {len(e.commit_messages)} commits match skip patterns")
        return FilterDecision(True)

    return f


def make_first_run_age_filter(*, max_age_hours: int, now: datetime | None = None):
    """First-run cutoff: when the events table is empty for this repo, drop
    events older than max_age_hours so we don't backfill ancient activity."""
    cutoff = (_ts(now) if now is not None else _utc_now_naive()) - timedelta(hours=max_age_hours)

    def f(e: NormalizedEvent) -> FilterDecision:
        if _ts(e.occurred_at) < cutoff:
            return FilterDecision(False, f"older than {max_age_hours}h on first ingestion")
        return FilterDecision(True)

    return f


def filter_repo_allowlist(allowlist: set[str]):
    """Belt-and-suspenders: enforce repo allowlist at the filter layer too,
    even though github_client should already only pull from listed repos."""

    def f(e: NormalizedEvent) -> FilterDecision:
        if e.repo not in allowlist:
            return FilterDecision(False, f"repo {e.repo} not in allowlist")
        return FilterDecision(True)

    return f


@dataclass
class FilterResult:
    kept: list[NormalizedEvent]
    dropped: list[tuple[NormalizedEvent, str]]


def apply_all(
    events: Iterable[NormalizedEvent],
    filters: list,
) -> FilterResult:
    """Run every event through the filter chain. First reject reason wins."""
    kept: list[NormalizedEvent] = []
    dropped: list[tuple[NormalizedEvent, str]] = []
    for e in events:
        rejected_reason: str | None = None
        for f in filters:
            d = f(e)
            if not d.keep:
                rejected_reason = d.reason
                break
        if rejected_reason is None:
            kept.append(e)
        else:
            dropped.append((e, rejected_reason))
    return FilterResult(kept=kept, dropped=dropped)


def build_default_chain(
    *,
    allowlist: set[str],
    skip_commit_patterns: list[str],
    first_run: bool = False,
    first_run_max_age_hours: int = 24,
    now: datetime | None = None,
):
    """Build the standard filter chain in spec order:

       1. allowlist (belt + suspenders)
       2. bot actors
       3. non-default branch (push only)
       4. conventional-commit chore/ci/docs (push only, ALL commits)
       5. first-run 24h cutoff (only if first_run=True)
    """
    chain: list = [
        filter_repo_allowlist(allowlist),
        filter_bot_actor,
        filter_non_default_branch,
        make_commit_message_filter(skip_commit_patterns),
    ]
    if first_run:
        chain.append(make_first_run_age_filter(max_age_hours=first_run_max_age_hours, now=now))
    return chain

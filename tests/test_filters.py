"""Step 5 — pre-LLM filter tests with fixture events."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from wire.ingestion.filters import (
    NormalizedEvent,
    apply_all,
    build_default_chain,
    filter_bot_actor,
    filter_non_default_branch,
    filter_repo_allowlist,
    make_commit_message_filter,
    make_first_run_age_filter,
)
from wire.ingestion.github_client import normalize_raw_event


def _ev(**kw) -> NormalizedEvent:
    base = dict(
        github_id="1",
        repo="winetrackr",
        event_type="PushEvent",
        actor="someone",
        occurred_at=datetime(2026, 4, 29, 10, 0, 0),
        default_branch="main",
        branch="main",
        pr_merged=False,
        commit_messages=["feat: ship a thing"],
    )
    base.update(kw)
    return NormalizedEvent(**base)


# ---------- bot filter -------------------------------------------------------


@pytest.mark.parametrize(
    "actor", ["dependabot", "dependabot[bot]", "github-actions[bot]", "renovate[bot]"]
)
def test_bot_actor_dropped(actor):
    d = filter_bot_actor(_ev(actor=actor))
    assert d.keep is False
    assert "bot" in d.reason.lower()


def test_human_actor_kept():
    assert filter_bot_actor(_ev(actor="jbellander")).keep is True


# ---------- non-default branch filter ----------------------------------------


def test_push_to_default_branch_kept():
    e = _ev(branch="main", default_branch="main")
    assert filter_non_default_branch(e).keep is True


def test_push_to_feature_branch_dropped():
    e = _ev(branch="feat-foo", default_branch="main")
    d = filter_non_default_branch(e)
    assert d.keep is False
    assert "non-default" in d.reason


def test_pr_merge_passes_even_on_non_default():
    # PullRequestEvent with merged=True is allowed regardless of branch
    e = _ev(event_type="PullRequestEvent", branch=None, pr_merged=True)
    assert filter_non_default_branch(e).keep is True


def test_non_push_passes_through():
    # ReleaseEvent has no branch concept — filter is no-op
    e = _ev(event_type="ReleaseEvent", branch=None, default_branch=None)
    assert filter_non_default_branch(e).keep is True


# ---------- conventional commit filter ---------------------------------------


def test_all_commits_chore_dropped():
    f = make_commit_message_filter([r"^(chore|ci|docs|style)(\(.+\))?:"])
    e = _ev(commit_messages=["chore: bump deps", "ci: fix workflow", "docs(readme): typo"])
    d = f(e)
    assert d.keep is False
    assert "skip patterns" in d.reason


def test_mixed_commits_kept():
    f = make_commit_message_filter([r"^(chore|ci|docs|style)(\(.+\))?:"])
    e = _ev(commit_messages=["chore: bump", "feat: real feature"])
    assert f(e).keep is True


def test_no_commits_kept():
    """Edge case: PushEvent with empty commit list (e.g. force-push of merge)."""
    f = make_commit_message_filter([r"^chore:"])
    e = _ev(commit_messages=[])
    assert f(e).keep is True


def test_commit_filter_skips_non_push():
    f = make_commit_message_filter([r"^chore:"])
    # Even with all-chore-looking strings in commit_messages, non-push types pass
    e = _ev(event_type="ReleaseEvent", commit_messages=None)
    assert f(e).keep is True


# ---------- first-run age filter ---------------------------------------------


def test_first_run_drops_old_events():
    now = datetime(2026, 4, 29, 12, 0, 0)
    f = make_first_run_age_filter(max_age_hours=24, now=now)
    old = _ev(occurred_at=now - timedelta(hours=48))
    fresh = _ev(occurred_at=now - timedelta(hours=1))
    assert f(old).keep is False
    assert f(fresh).keep is True


def test_first_run_handles_tz_aware_input():
    now = datetime(2026, 4, 29, 12, 0, 0)
    f = make_first_run_age_filter(max_age_hours=24, now=now)
    e = _ev(occurred_at=datetime(2026, 4, 27, 0, 0, 0, tzinfo=UTC))
    assert f(e).keep is False


# ---------- allowlist --------------------------------------------------------


def test_allowlist_blocks_unknown_repo():
    f = filter_repo_allowlist({"winetrackr"})
    e = _ev(repo="visma-secret-stuff")
    d = f(e)
    assert d.keep is False
    assert "allowlist" in d.reason


def test_allowlist_allows_listed_repo():
    f = filter_repo_allowlist({"winetrackr"})
    assert f(_ev(repo="winetrackr")).keep is True


# ---------- chain composition ------------------------------------------------


def test_default_chain_drops_for_first_filter_failure():
    # Bot push that's also a feature branch — dropped by bot filter (first match wins)
    chain = build_default_chain(
        allowlist={"winetrackr"},
        skip_commit_patterns=[r"^chore:"],
        first_run=False,
    )
    e = _ev(actor="dependabot[bot]", branch="dependabot/npm/foo")
    res = apply_all([e], chain)
    assert len(res.kept) == 0
    assert "bot" in res.dropped[0][1].lower()


def test_default_chain_keeps_real_feature_push():
    chain = build_default_chain(
        allowlist={"winetrackr"},
        skip_commit_patterns=[r"^(chore|ci|docs|style)(\(.+\))?:"],
        first_run=False,
    )
    e = _ev(actor="jbellander", branch="main", commit_messages=["feat: shipping"])
    res = apply_all([e], chain)
    assert len(res.kept) == 1
    assert len(res.dropped) == 0


def test_default_chain_runs_first_run_age_filter_only_when_enabled():
    now = datetime(2026, 4, 29, 12, 0, 0)
    old = _ev(occurred_at=now - timedelta(hours=48))
    chain_no_age = build_default_chain(
        allowlist={"winetrackr"},
        skip_commit_patterns=[r"^chore:"],
        first_run=False,
        now=now,
    )
    chain_with_age = build_default_chain(
        allowlist={"winetrackr"},
        skip_commit_patterns=[r"^chore:"],
        first_run=True,
        first_run_max_age_hours=24,
        now=now,
    )
    assert apply_all([old], chain_no_age).kept == [old]
    assert apply_all([old], chain_with_age).kept == []


# ---------- normalize_raw_event ----------------------------------------------


def test_normalize_push_event():
    raw = {
        "id": "987",
        "type": "PushEvent",
        "actor": {"login": "jbellander"},
        "created_at": "2026-04-29T10:00:00Z",
        "payload": {
            "ref": "refs/heads/main",
            "commits": [
                {"sha": "abc", "message": "feat: thing"},
                {"sha": "def", "message": "fix: bug"},
            ],
        },
    }
    n = normalize_raw_event(raw, repo="winetrackr", default_branch="main", org="me")
    assert n is not None
    assert n.event_type == "PushEvent"
    assert n.branch == "main"
    assert n.commit_messages == ["feat: thing", "fix: bug"]
    assert n.actor == "jbellander"
    assert n.payload["html_url"].endswith("/commit/def")


def test_normalize_unknown_event_dropped():
    raw = {
        "id": "1",
        "type": "WatchEvent",
        "actor": {"login": "x"},
        "created_at": "2026-04-29T10:00:00Z",
        "payload": {},
    }
    assert normalize_raw_event(raw, repo="winetrackr", default_branch="main", org="me") is None


def test_normalize_pr_merged_flag():
    raw = {
        "id": "1",
        "type": "PullRequestEvent",
        "actor": {"login": "x"},
        "created_at": "2026-04-29T10:00:00Z",
        "payload": {
            "action": "closed",
            "pull_request": {"merged": True, "html_url": "https://github.com/me/r/pull/1"},
        },
    }
    n = normalize_raw_event(raw, repo="r", default_branch="main", org="me")
    assert n is not None
    assert n.pr_merged is True
    assert n.payload["html_url"] == "https://github.com/me/r/pull/1"

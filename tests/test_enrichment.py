"""Tests for the GitHub /events payload enrichment step.

The /events endpoint strips both PushEvent (no commits) and PullRequestEvent
(no title/body/merged) — Wire backfills via /compare and /pulls/{n}.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from wire.ingestion.github_client import GitHubClient
from wire.ingestion.poller import _enrich_events


@pytest.fixture
def fake_client():
    """Real GitHubClient with mocked methods so we don't hit the network."""
    c = GitHubClient(
        app_id=1, installation_id=1,
        private_key_pem="not-needed-for-this-test",
        org="me",
    )
    c.compare_commits = AsyncMock(return_value=[
        {"sha": "abc", "commit": {"message": "feat: ship a thing", "author": {"name": "Johan"}}},
        {"sha": "def", "commit": {"message": "fix: bug", "author": {"name": "Johan"}}},
    ])
    c.get_pull_request = AsyncMock(return_value={
        "id": 999, "number": 42, "title": "Add metrics dashboard",
        "body": "Long description here", "merged": True, "state": "closed",
        "html_url": "https://github.com/me/r/pull/42",
    })
    return c


@pytest.mark.asyncio
async def test_enrich_push_event_fills_commits(fake_client):
    raw = [{
        "id": "1", "type": "PushEvent", "actor": {"login": "j"},
        "created_at": "2026-04-30T10:00:00Z",
        "payload": {"before": "aaa", "head": "bbb", "ref": "refs/heads/main"},
    }]
    out = await _enrich_events(fake_client, "myrepo", raw)
    payload = out[0]["payload"]
    assert "commits" in payload
    assert len(payload["commits"]) == 2
    assert payload["commits"][0]["message"] == "feat: ship a thing"
    fake_client.compare_commits.assert_awaited_once_with("myrepo", "aaa", "bbb")


@pytest.mark.asyncio
async def test_enrich_push_event_skips_null_before_sha(fake_client):
    """When a branch is just created, before is the null sha and there's
    nothing meaningful to compare against."""
    raw = [{
        "id": "1", "type": "PushEvent", "actor": {"login": "j"},
        "created_at": "2026-04-30T10:00:00Z",
        "payload": {"before": "0" * 40, "head": "bbb", "ref": "refs/heads/main"},
    }]
    out = await _enrich_events(fake_client, "myrepo", raw)
    fake_client.compare_commits.assert_not_called()
    assert "commits" not in out[0]["payload"]


@pytest.mark.asyncio
async def test_enrich_pr_event_fills_full_pr(fake_client):
    raw = [{
        "id": "2", "type": "PullRequestEvent", "actor": {"login": "j"},
        "created_at": "2026-04-30T10:00:00Z",
        "payload": {
            "action": "closed",
            "number": 42,
            "pull_request": {"id": 999, "number": 42, "url": "..."},  # stripped
        },
    }]
    out = await _enrich_events(fake_client, "myrepo", raw)
    pr = out[0]["payload"]["pull_request"]
    assert pr["title"] == "Add metrics dashboard"
    assert pr["merged"] is True
    fake_client.get_pull_request.assert_awaited_once_with("myrepo", 42)


@pytest.mark.asyncio
async def test_enrich_pr_caches_within_one_call(fake_client):
    """Multiple events for the same PR (opened then merged) should only
    trigger ONE detail fetch."""
    raw = [
        {
            "id": "10", "type": "PullRequestEvent", "actor": {"login": "j"},
            "created_at": "2026-04-30T10:00:00Z",
            "payload": {"action": "opened", "number": 7,
                        "pull_request": {"number": 7}},
        },
        {
            "id": "11", "type": "PullRequestEvent", "actor": {"login": "j"},
            "created_at": "2026-04-30T10:05:00Z",
            "payload": {"action": "closed", "number": 7,
                        "pull_request": {"number": 7}},
        },
    ]
    await _enrich_events(fake_client, "myrepo", raw)
    assert fake_client.get_pull_request.await_count == 1


@pytest.mark.asyncio
async def test_enrich_failure_does_not_kill_the_chain(fake_client):
    """If one enrichment fails, the rest still run."""
    fake_client.compare_commits = AsyncMock(side_effect=RuntimeError("transient"))
    raw = [
        {
            "id": "20", "type": "PushEvent", "actor": {"login": "j"},
            "created_at": "2026-04-30T10:00:00Z",
            "payload": {"before": "aaa", "head": "bbb"},
        },
        {
            "id": "21", "type": "PullRequestEvent", "actor": {"login": "j"},
            "created_at": "2026-04-30T10:00:00Z",
            "payload": {"number": 5, "pull_request": {"number": 5}},
        },
    ]
    out = await _enrich_events(fake_client, "myrepo", raw)
    # PushEvent passed through with stripped payload (no commits added)
    assert "commits" not in out[0]["payload"]
    # PR event still got enriched
    assert out[1]["payload"]["pull_request"]["title"] == "Add metrics dashboard"


@pytest.mark.asyncio
async def test_enrich_unknown_event_types_pass_through(fake_client):
    raw = [{
        "id": "30", "type": "ReleaseEvent", "actor": {"login": "j"},
        "created_at": "2026-04-30T10:00:00Z",
        "payload": {"action": "published", "release": {"tag_name": "v1.0"}},
    }]
    out = await _enrich_events(fake_client, "myrepo", raw)
    assert out[0]["payload"]["release"]["tag_name"] == "v1.0"
    fake_client.compare_commits.assert_not_called()
    fake_client.get_pull_request.assert_not_called()

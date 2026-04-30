"""Tests for GitHub client pagination cap handling and 422 grace."""

from __future__ import annotations

import time

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from wire.ingestion.github_client import GitHubClient


@pytest.fixture
def fake_pem() -> str:
    """A throwaway RSA key so JWT signing works in tests."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")


@pytest.fixture
def gh_client(fake_pem) -> GitHubClient:
    return GitHubClient(
        app_id=12345,
        installation_id=67890,
        private_key_pem=fake_pem,
        org="testorg",
    )


def _stub_installation_token(client: GitHubClient) -> None:
    """Bypass the JWT exchange — set the cached token directly."""
    from wire.ingestion.github_client import _CachedToken

    client._installation_token = _CachedToken(token="fake-token", expires_at=time.time() + 3600)


@pytest.mark.asyncio
async def test_list_events_handles_422_gracefully(gh_client, respx_mock):
    """GitHub's /events API returns 422 past page 3. Should be treated as
    end-of-pagination, not raised as an error."""
    _stub_installation_token(gh_client)

    page1 = [
        {
            "id": str(i),
            "type": "PushEvent",
            "actor": {"login": "x"},
            "created_at": "2026-04-30T10:00:00Z",
            "payload": {},
        }
        for i in range(100)
    ]
    page2 = [
        {
            "id": str(i + 100),
            "type": "PushEvent",
            "actor": {"login": "x"},
            "created_at": "2026-04-30T09:00:00Z",
            "payload": {},
        }
        for i in range(100)
    ]
    page3 = [
        {
            "id": str(i + 200),
            "type": "PushEvent",
            "actor": {"login": "x"},
            "created_at": "2026-04-30T08:00:00Z",
            "payload": {},
        }
        for i in range(100)
    ]

    route = respx_mock.get("https://api.github.com/repos/testorg/myrepo/events")
    route.mock(
        side_effect=[
            httpx.Response(200, json=page1),
            httpx.Response(200, json=page2),
            httpx.Response(200, json=page3),
            httpx.Response(422, text="end of pagination"),  # would 422 on page 4
        ]
    )

    try:
        events = await gh_client.list_events("myrepo", max_pages=5)
    finally:
        await gh_client.aclose()

    # Should collect 300 events from pages 1-3 then stop on the 422
    assert len(events) == 300


@pytest.mark.asyncio
async def test_list_events_stops_at_max_pages_default_3(gh_client, respx_mock):
    """Default max_pages should be 3 — matches GitHub's actual cap."""
    _stub_installation_token(gh_client)

    full_page = [
        {
            "id": str(i),
            "type": "PushEvent",
            "actor": {"login": "x"},
            "created_at": "2026-04-30T10:00:00Z",
            "payload": {},
        }
        for i in range(100)
    ]

    route = respx_mock.get("https://api.github.com/repos/testorg/myrepo/events")
    # Always return a full page; default max_pages=3 should cap iteration.
    route.mock(return_value=httpx.Response(200, json=full_page))

    try:
        events = await gh_client.list_events("myrepo")  # default max_pages
    finally:
        await gh_client.aclose()

    # Default cap: 3 pages × 100 = 300
    assert len(events) == 300


@pytest.mark.asyncio
async def test_list_events_stops_on_404(gh_client, respx_mock):
    _stub_installation_token(gh_client)
    respx_mock.get("https://api.github.com/repos/testorg/missing/events").mock(
        return_value=httpx.Response(404, text="not found")
    )
    try:
        events = await gh_client.list_events("missing")
    finally:
        await gh_client.aclose()
    assert events == []


@pytest.mark.asyncio
async def test_5xx_is_retried(gh_client, respx_mock, monkeypatch):
    """500/502/503 from GitHub should be retried; transient blips shouldn't
    abandon the whole repo's poll."""
    _stub_installation_token(gh_client)
    # Make tenacity's exponential wait near-instant in tests.
    monkeypatch.setattr(
        "wire.ingestion.github_client.wait_exponential",
        lambda **kw: __import__("tenacity").wait_fixed(0),
    )

    page1 = [
        {
            "id": "1",
            "type": "PushEvent",
            "actor": {"login": "x"},
            "created_at": "2026-04-30T10:00:00Z",
            "payload": {},
        }
    ]

    route = respx_mock.get("https://api.github.com/repos/testorg/myrepo/events")
    route.mock(
        side_effect=[
            httpx.Response(500, text="internal error"),  # first attempt fails
            httpx.Response(502, text="bad gateway"),  # second attempt fails
            httpx.Response(200, json=page1),  # third attempt succeeds
        ]
    )

    try:
        events = await gh_client.list_events("myrepo")
    finally:
        await gh_client.aclose()
    assert len(events) == 1


@pytest.mark.asyncio
async def test_5xx_eventually_gives_up_after_retries(gh_client, respx_mock, monkeypatch):
    _stub_installation_token(gh_client)
    monkeypatch.setattr(
        "wire.ingestion.github_client.wait_exponential",
        lambda **kw: __import__("tenacity").wait_fixed(0),
    )

    respx_mock.get("https://api.github.com/repos/testorg/myrepo/events").mock(
        return_value=httpx.Response(503, text="service unavailable")
    )

    try:
        with pytest.raises(httpx.HTTPStatusError):
            await gh_client.list_events("myrepo")
    finally:
        await gh_client.aclose()


@pytest.mark.asyncio
async def test_compare_commits_returns_commit_array(gh_client, respx_mock):
    _stub_installation_token(gh_client)
    respx_mock.get("https://api.github.com/repos/testorg/myrepo/compare/aaa...bbb").mock(
        return_value=httpx.Response(
            200,
            json={
                "commits": [
                    {"sha": "abc1", "commit": {"message": "feat: add x", "author": {"name": "j"}}},
                    {"sha": "def2", "commit": {"message": "fix: y", "author": {"name": "j"}}},
                ]
            },
        )
    )
    try:
        commits = await gh_client.compare_commits("myrepo", "aaa", "bbb")
    finally:
        await gh_client.aclose()
    assert commits is not None and len(commits) == 2
    assert commits[0]["commit"]["message"] == "feat: add x"


@pytest.mark.asyncio
async def test_compare_commits_returns_none_on_404(gh_client, respx_mock):
    _stub_installation_token(gh_client)
    respx_mock.get("https://api.github.com/repos/testorg/myrepo/compare/aaa...bbb").mock(
        return_value=httpx.Response(404, text="not found")
    )
    try:
        commits = await gh_client.compare_commits("myrepo", "aaa", "bbb")
    finally:
        await gh_client.aclose()
    assert commits is None


@pytest.mark.asyncio
async def test_404_is_not_retried(gh_client, respx_mock):
    """404 means the repo is gone or wrong — don't retry."""
    _stub_installation_token(gh_client)
    route = respx_mock.get("https://api.github.com/repos/testorg/myrepo/events")
    route.mock(return_value=httpx.Response(404, text="not found"))

    try:
        events = await gh_client.list_events("myrepo")
    finally:
        await gh_client.aclose()
    # 404 returns empty list cleanly, not after retries
    assert events == []
    # Only one call made, not 3
    assert route.call_count == 1

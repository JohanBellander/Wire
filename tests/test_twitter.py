"""Step 9 — X posting client tests. respx-mocked; no real network."""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from wire.twitter.client import TwitterAuthError, TwitterClient
from wire.twitter.oauth import TwitterToken, load_token, save_token


def _write_token(tmp_path: Path, expires_in: int = 7200) -> Path:
    tok = TwitterToken(
        access_token="acc-1",
        refresh_token="ref-1",
        expires_at=time.time() + expires_in,
        scope="tweet.read tweet.write",
    )
    p = tmp_path / "twitter-token.json"
    save_token(p, tok)
    return p


@pytest.mark.asyncio
async def test_post_single_tweet(tmp_path, respx_mock):
    token_path = _write_token(tmp_path)
    respx_mock.post("https://api.x.com/2/tweets").mock(
        return_value=httpx.Response(201, json={"data": {"id": "tw-100", "text": "hi"}})
    )

    client = TwitterClient(
        client_id="cid",
        client_secret="csec",
        token_path=token_path,
        username="me",
    )
    try:
        result = await client.post("hi")
    finally:
        await client.aclose()
    assert result.tweet_id == "tw-100"
    assert "tw-100" in result.url


@pytest.mark.asyncio
async def test_post_thread_chains_replies(tmp_path, respx_mock):
    token_path = _write_token(tmp_path)
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        # Return synthetic ids for each call
        return httpx.Response(201, json={"data": {"id": f"tw-{len(calls)}", "text": body["text"]}})

    respx_mock.post("https://api.x.com/2/tweets").mock(side_effect=handler)

    client = TwitterClient(
        client_id="cid",
        client_secret="csec",
        token_path=token_path,
        username="me",
    )
    try:
        result = await client.post("first part\n---\nsecond part\n---\nthird part")
    finally:
        await client.aclose()

    assert len(calls) == 3
    # First post has no reply field
    assert "reply" not in calls[0]
    # Second + third reply to the previous tweet's id
    assert calls[1]["reply"]["in_reply_to_tweet_id"] == "tw-1"
    assert calls[2]["reply"]["in_reply_to_tweet_id"] == "tw-2"
    # Head id returned
    assert result.tweet_id == "tw-1"


@pytest.mark.asyncio
async def test_auth_error_does_not_retry(tmp_path, respx_mock):
    token_path = _write_token(tmp_path)
    respx_mock.post("https://api.x.com/2/tweets").mock(
        return_value=httpx.Response(401, text="invalid token")
    )
    client = TwitterClient(
        client_id="cid",
        client_secret="csec",
        token_path=token_path,
        username="me",
    )
    try:
        with pytest.raises(TwitterAuthError):
            await client.post("hi")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_rate_limit_waits_then_retries(tmp_path, respx_mock, monkeypatch):
    token_path = _write_token(tmp_path)
    # Patch sleep to be instant
    import wire.twitter.client as tw

    monkeypatch.setattr(tw.asyncio, "sleep", _instant_sleep)

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429,
                headers={"x-rate-limit-reset": str(int(time.time()) + 1)},
                text="rate limited",
            )
        return httpx.Response(201, json={"data": {"id": "tw-after", "text": "hi"}})

    respx_mock.post("https://api.x.com/2/tweets").mock(side_effect=handler)

    client = TwitterClient(
        client_id="cid",
        client_secret="csec",
        token_path=token_path,
        username="me",
    )
    try:
        result = await client.post("hi")
    finally:
        await client.aclose()
    assert result.tweet_id == "tw-after"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_fetch_metrics_batches_under_100(tmp_path, respx_mock):
    token_path = _write_token(tmp_path)
    respx_mock.get("https://api.x.com/2/tweets").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "1",
                        "public_metrics": {
                            "impression_count": 100,
                            "like_count": 5,
                            "retweet_count": 1,
                            "reply_count": 0,
                            "bookmark_count": 2,
                        },
                    },
                ]
            },
        )
    )
    client = TwitterClient(
        client_id="cid",
        client_secret="csec",
        token_path=token_path,
        username="me",
    )
    try:
        results = await client.fetch_metrics(["1"])
    finally:
        await client.aclose()
    assert len(results) == 1
    assert results[0].likes == 5
    assert results[0].impressions == 100


@pytest.mark.asyncio
async def test_token_refreshed_when_expiring(tmp_path, respx_mock):
    # Token already expired
    tok = TwitterToken(
        access_token="old",
        refresh_token="ref",
        expires_at=time.time() - 1,
        scope="",
    )
    token_path = tmp_path / "tok.json"
    save_token(token_path, tok)

    respx_mock.post("https://api.x.com/2/oauth2/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "fresh",
                "refresh_token": "ref-2",
                "expires_in": 7200,
                "scope": "tweet.read tweet.write",
            },
        )
    )
    captured: list[str] = []

    def post_handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get("authorization", ""))
        return httpx.Response(201, json={"data": {"id": "tw-9", "text": "hi"}})

    respx_mock.post("https://api.x.com/2/tweets").mock(side_effect=post_handler)

    client = TwitterClient(
        client_id="cid",
        client_secret="csec",
        token_path=token_path,
        username="me",
    )
    try:
        await client.post("hi")
    finally:
        await client.aclose()
    # POST headers should carry the *fresh* bearer token, not the old one.
    assert captured[0] == "Bearer fresh"
    # Token blob on disk now has the new values.
    saved = load_token(token_path)
    assert saved.access_token == "fresh"
    assert saved.refresh_token == "ref-2"


# ---------- helpers ----------------------------------------------------------


async def _instant_sleep(*_a, **_kw):
    return None

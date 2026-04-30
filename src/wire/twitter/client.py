"""X API v2 posting + metrics fetching client.

Uses raw httpx against api.twitter.com/2 with the OAuth 2.0 access token.
Refreshes automatically when the cached token is near expiry.

Posts:
  POST /2/tweets                    body={"text": "..."}            single tweet
  POST /2/tweets                    body={"text":"...", "reply": {"in_reply_to_tweet_id": "..."}}
                                                                    thread reply
Metrics:
  GET  /2/tweets?ids=<id>&tweet.fields=public_metrics

Rate-limit handling: on 429 we honor x-rate-limit-reset and wait once before
retrying. On 5xx we retry with exponential backoff via tenacity.
Auth (401/403) is fatal — never retried.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import httpx
import structlog
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from wire.twitter.oauth import TwitterToken, load_token, refresh_access_token, save_token

log = structlog.get_logger()

# Canonical base URL since 2026; api.twitter.com still works as an alias.
API = "https://api.x.com/2"


class TwitterAuthError(Exception):
    pass


class TwitterRateLimitError(Exception):
    def __init__(self, reset_at: float) -> None:
        super().__init__(f"rate-limited; resets at {reset_at}")
        self.reset_at = reset_at


@dataclass
class PostResult:
    tweet_id: str
    posted_text: str
    url: str


@dataclass
class TweetMetrics:
    tweet_id: str
    impressions: int | None
    likes: int | None
    retweets: int | None
    replies: int | None
    bookmarks: int | None


class TwitterClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        token_path: Path,
        username: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret  # stored for completeness; PKCE uses public client
        self.token_path = token_path
        self.username = username
        self._client = http_client
        self._owns_client = http_client is None
        self._token: TwitterToken | None = None

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def _get_http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    # --- token plumbing ------------------------------------------------------

    async def _ensure_token(self) -> str:
        if self._token is None:
            self._token = load_token(self.token_path)
        if self._token is None:
            raise TwitterAuthError(
                f"No twitter token at {self.token_path}. Run "
                f"`uv run python -m wire.scripts.twitter_auth` once."
            )
        if self._token.is_expiring_soon():
            self._token = await refresh_access_token(
                self.client_id,
                self._token.refresh_token,
                client_secret=self.client_secret or None,
                http_client=self._get_http(),
            )
            save_token(self.token_path, self._token)
        return self._token.access_token

    async def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {await self._ensure_token()}",
            "Content-Type": "application/json",
        }

    # --- core HTTP -----------------------------------------------------------

    async def _request(
        self, method: str, url: str, *, json_body: dict | None = None, params: dict | None = None
    ) -> httpx.Response:
        client = self._get_http()
        headers = await self._auth_headers()

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
            reraise=True,
        ):
            with attempt:
                resp = await client.request(
                    method, url, json=json_body, params=params, headers=headers
                )
                if resp.status_code in (401, 403):
                    raise TwitterAuthError(
                        f"Twitter auth failed at {url}: {resp.status_code} {resp.text[:200]}"
                    )
                if resp.status_code == 429:
                    reset = float(resp.headers.get("x-rate-limit-reset", time.time() + 60))
                    raise TwitterRateLimitError(reset)
                if 500 <= resp.status_code < 600:
                    resp.raise_for_status()
                return resp
        raise RuntimeError("unreachable")

    # --- posting -------------------------------------------------------------

    async def post(self, text: str) -> PostResult:
        """Post a single tweet OR a thread (joined by '\\n---\\n'). Threads
        are posted as a reply chain; returns the head tweet's id+url."""
        if "\n---\n" in text:
            tweets = [p.strip() for p in text.split("\n---\n") if p.strip()]
        else:
            tweets = [text]

        head_id: str | None = None
        head_text: str | None = None
        last_id: str | None = None
        for t in tweets:
            payload: dict = {"text": t}
            if last_id is not None:
                payload["reply"] = {"in_reply_to_tweet_id": last_id}
            try:
                resp = await self._request("POST", f"{API}/tweets", json_body=payload)
            except TwitterRateLimitError as e:
                wait = max(0.0, e.reset_at - time.time())
                log.warning("wire.twitter.rate_limited", wait_s=int(wait))
                await asyncio.sleep(min(wait, 600))
                resp = await self._request("POST", f"{API}/tweets", json_body=payload)
            resp.raise_for_status()
            data = resp.json().get("data", {})
            tid = str(data.get("id"))
            if not tid:
                raise RuntimeError(f"Twitter returned no id: {resp.text[:200]}")
            if head_id is None:
                head_id = tid
                head_text = t
            last_id = tid

        url = (
            f"https://x.com/{self.username}/status/{head_id}"
            if self.username
            else f"https://x.com/i/status/{head_id}"
        )
        return PostResult(tweet_id=head_id or "", posted_text=head_text or text, url=url)

    # --- metrics -------------------------------------------------------------

    async def fetch_metrics(self, tweet_ids: Iterable[str]) -> list[TweetMetrics]:
        ids = [str(i) for i in tweet_ids]
        if not ids:
            return []
        out: list[TweetMetrics] = []
        # X allows up to 100 ids per call.
        for i in range(0, len(ids), 100):
            batch = ids[i : i + 100]
            try:
                resp = await self._request(
                    "GET",
                    f"{API}/tweets",
                    params={"ids": ",".join(batch), "tweet.fields": "public_metrics"},
                )
            except TwitterRateLimitError as e:
                wait = max(0.0, e.reset_at - time.time())
                log.warning("wire.twitter.metrics_rate_limited", wait_s=int(wait))
                await asyncio.sleep(min(wait, 600))
                resp = await self._request(
                    "GET",
                    f"{API}/tweets",
                    params={"ids": ",".join(batch), "tweet.fields": "public_metrics"},
                )
            resp.raise_for_status()
            for row in resp.json().get("data", []):
                pm = row.get("public_metrics") or {}
                out.append(
                    TweetMetrics(
                        tweet_id=str(row["id"]),
                        impressions=pm.get("impression_count"),
                        likes=pm.get("like_count"),
                        retweets=pm.get("retweet_count"),
                        replies=pm.get("reply_count"),
                        bookmarks=pm.get("bookmark_count"),
                    )
                )
        return out

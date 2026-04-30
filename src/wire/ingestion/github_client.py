"""GitHub App authentication + events / PRs / releases fetcher.

Uses the GitHub App pattern: sign a JWT with the App private key, exchange it
for an installation access token, then call the REST API. Tokens last 1h —
we cache them and refresh lazily.

The events stream (`/repos/{owner}/{repo}/events`) is the source of truth.
PR and release events get an extra detail fetch from their dedicated
endpoints to enrich the payload (titles, bodies, merged state).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
import jwt
import structlog
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from wire.ingestion.filters import NormalizedEvent

log = structlog.get_logger()

GITHUB_API = "https://api.github.com"


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_github_ts(s: str) -> datetime:
    """GitHub returns ISO 8601 like '2026-04-29T10:00:00Z'. Convert to naive UTC."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(timezone.utc).replace(tzinfo=None)


@dataclass
class _CachedToken:
    token: str
    expires_at: float  # epoch seconds


class GitHubAuthError(Exception):
    pass


class GitHubClient:
    def __init__(
        self,
        *,
        app_id: int,
        installation_id: int,
        private_key_pem: str,
        org: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.app_id = app_id
        self.installation_id = installation_id
        self.private_key_pem = private_key_pem
        self.org = org
        self._client = client
        self._owns_client = client is None
        self._installation_token: _CachedToken | None = None

    @classmethod
    def from_files(cls, *, app_id: int, installation_id: int, private_key_path, org: str):
        from pathlib import Path
        path = Path(private_key_path)
        if not path.exists():
            raise GitHubAuthError(f"GitHub App private key not found: {path}")
        return cls(
            app_id=app_id,
            installation_id=installation_id,
            private_key_pem=path.read_text(encoding="utf-8"),
            org=org,
        )

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "wire-bot/0.1",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # --- auth -----------------------------------------------------------------

    def _app_jwt(self) -> str:
        """Sign an App JWT (good for ≤10 min). Used to mint installation tokens."""
        now = int(time.time())
        payload = {
            "iat": now - 60,           # tolerate small clock skew
            "exp": now + 9 * 60,       # 9 min — under the 10-min hard cap
            "iss": str(self.app_id),
        }
        return jwt.encode(payload, self.private_key_pem, algorithm="RS256")

    async def _installation_access_token(self) -> str:
        cached = self._installation_token
        if cached and cached.expires_at - 60 > time.time():
            return cached.token

        app_jwt = self._app_jwt()
        client = self._get_client()
        url = f"{GITHUB_API}/app/installations/{self.installation_id}/access_tokens"
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {app_jwt}"},
        )
        if resp.status_code in (401, 403):
            raise GitHubAuthError(
                f"GitHub installation token request failed ({resp.status_code}): "
                f"check app_id={self.app_id}, installation_id={self.installation_id}, "
                f"and that the private key matches the App. {resp.text[:200]}"
            )
        resp.raise_for_status()
        data = resp.json()
        token = data["token"]
        expires_at = _parse_github_ts(data["expires_at"]).replace(tzinfo=timezone.utc).timestamp()
        self._installation_token = _CachedToken(token=token, expires_at=expires_at)
        return token

    async def _auth_headers(self) -> dict[str, str]:
        token = await self._installation_access_token()
        return {"Authorization": f"Bearer {token}"}

    # --- core HTTP ------------------------------------------------------------

    async def _get(self, url: str, *, params: dict | None = None) -> httpx.Response:
        client = self._get_client()
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
            reraise=True,
        ):
            with attempt:
                headers = await self._auth_headers()
                resp = await client.get(url, params=params, headers=headers)
                if resp.status_code in (401, 403):
                    raise GitHubAuthError(f"GitHub auth failed at {url}: {resp.status_code} {resp.text[:200]}")
                if 500 <= resp.status_code < 600:
                    # raise so tenacity retries; but TransportError isn't a status
                    resp.raise_for_status()
                return resp
        raise RuntimeError("unreachable")

    # --- public fetch methods -------------------------------------------------

    async def list_events(self, repo: str, *, since: datetime | None = None, max_pages: int = 3) -> list[dict]:
        """List events from /repos/{owner}/{repo}/events. Pages until `since`
        is reached, max_pages exhausted, or GitHub returns 422 (the events API
        hard-caps total return at ~300 events / 3 pages of per_page=100, regardless
        of how far back you ask). Returns events newest-first.
        """
        cutoff = since
        out: list[dict] = []
        url = f"{GITHUB_API}/repos/{self.org}/{repo}/events"
        for page in range(1, max_pages + 1):
            resp = await self._get(url, params={"per_page": 100, "page": page})
            if resp.status_code == 404:
                log.warning("wire.github.repo_not_found", repo=f"{self.org}/{repo}")
                return out
            if resp.status_code == 422:
                # The events API caps total at ~300; past page 3 it returns 422.
                # Treat as natural end-of-pagination, not an error.
                log.info(
                    "wire.github.pagination_cap_reached",
                    repo=f"{self.org}/{repo}",
                    page=page,
                    collected=len(out),
                )
                return out
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            for raw in batch:
                ts = _parse_github_ts(raw["created_at"])
                if cutoff is not None and ts <= cutoff:
                    return out
                out.append(raw)
            if len(batch) < 100:
                break
        return out

    async def get_default_branch(self, repo: str) -> str | None:
        url = f"{GITHUB_API}/repos/{self.org}/{repo}"
        resp = await self._get(url)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json().get("default_branch")

    async def get_pull_request(self, repo: str, number: int) -> dict | None:
        url = f"{GITHUB_API}/repos/{self.org}/{repo}/pulls/{number}"
        resp = await self._get(url)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    async def list_releases(self, repo: str, *, since: datetime | None = None) -> list[dict]:
        url = f"{GITHUB_API}/repos/{self.org}/{repo}/releases"
        resp = await self._get(url, params={"per_page": 30})
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        out = resp.json()
        if since is not None:
            out = [r for r in out if _parse_github_ts(r["published_at"] or r["created_at"]) > since]
        return out


# --- normalization ------------------------------------------------------------


def normalize_raw_event(
    raw: dict,
    *,
    repo: str,
    default_branch: str | None,
    org: str,
) -> NormalizedEvent | None:
    """Convert a raw event-stream payload to a NormalizedEvent. Returns None
    for event types we don't care about (e.g. WatchEvent, ForkEvent)."""

    accepted_types = {
        "PushEvent",
        "PullRequestEvent",
        "ReleaseEvent",
        "IssuesEvent",
        "IssueCommentEvent",
        "CreateEvent",
        "DeleteEvent",
    }
    et = raw.get("type")
    if et not in accepted_types:
        return None

    actor = (raw.get("actor") or {}).get("login")
    occurred_at = _parse_github_ts(raw["created_at"])
    payload = raw.get("payload") or {}

    branch = None
    pr_merged = False
    commit_messages: list[str] | None = None

    if et == "PushEvent":
        ref = payload.get("ref", "")  # e.g. "refs/heads/main"
        branch = ref.split("/", 2)[-1] if ref.startswith("refs/heads/") else None
        commit_messages = [c.get("message", "") for c in payload.get("commits", [])]
    elif et == "PullRequestEvent":
        pr = payload.get("pull_request") or {}
        pr_merged = bool(pr.get("merged"))

    return NormalizedEvent(
        github_id=str(raw.get("id")),
        repo=repo,
        event_type=et,
        actor=actor,
        occurred_at=occurred_at,
        default_branch=default_branch,
        branch=branch,
        pr_merged=pr_merged,
        commit_messages=commit_messages,
        payload={
            "type": et,
            "actor": actor,
            "raw_payload": payload,
            "html_url": _event_url(raw, repo, org),
        },
    )


def _event_url(raw: dict, repo: str, org: str) -> str | None:
    et = raw.get("type")
    p = raw.get("payload") or {}
    if et == "PullRequestEvent":
        return (p.get("pull_request") or {}).get("html_url")
    if et == "ReleaseEvent":
        return (p.get("release") or {}).get("html_url")
    if et == "PushEvent":
        sha = (p.get("commits") or [{}])[-1].get("sha")
        if sha:
            return f"https://github.com/{org}/{repo}/commit/{sha}"
    return None

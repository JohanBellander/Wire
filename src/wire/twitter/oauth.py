"""X OAuth 2.0 PKCE token persistence + refresh.

The interactive bootstrap (browser-based authorization) lives in
`wire.scripts.twitter_auth`. This module just loads/saves the token blob
and refreshes the access token when it's about to expire.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
import structlog

log = structlog.get_logger()

# Canonical token endpoint since 2026; api.twitter.com still works as an alias.
TOKEN_URL = "https://api.x.com/2/oauth2/token"


@dataclass
class TwitterToken:
    access_token: str
    refresh_token: str
    expires_at: float  # epoch seconds
    scope: str = ""

    def is_expiring_soon(self, *, leeway_seconds: int = 300) -> bool:
        return time.time() + leeway_seconds >= self.expires_at

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> TwitterToken:
        now = time.time()
        return cls(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token", ""),
            expires_at=now + float(payload.get("expires_in", 7200)),
            scope=payload.get("scope", ""),
        )

    def to_disk_payload(self) -> dict[str, Any]:
        return asdict(self)


def load_token(path: Path) -> TwitterToken | None:
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    return TwitterToken(
        access_token=raw["access_token"],
        refresh_token=raw.get("refresh_token", ""),
        expires_at=float(raw.get("expires_at", 0)),
        scope=raw.get("scope", ""),
    )


def save_token(path: Path, token: TwitterToken) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(token.to_disk_payload(), indent=2), encoding="utf-8")
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass  # Windows or fs that doesn't support chmod — fine


async def refresh_access_token(
    client_id: str,
    refresh_token: str,
    *,
    client_secret: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> TwitterToken:
    """Use the refresh_token to mint a new access token. X returns a *new*
    refresh_token too (rotation), which we store back. Confidential clients
    (those configured with a client secret in the X portal) must pass HTTP
    Basic auth on the token endpoint."""
    auth = (client_id, client_secret) if client_secret else None
    own_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=30.0)
    try:
        resp = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            auth=auth,
        )
    finally:
        if own_client:
            await client.aclose()
    if resp.status_code in (400, 401, 403):
        raise RuntimeError(
            f"Twitter refresh_token failed ({resp.status_code}): {resp.text[:200]}. "
            f"Re-run wire.scripts.twitter_auth to obtain a new token."
        )
    resp.raise_for_status()
    return TwitterToken.from_payload(resp.json())

"""One-time interactive OAuth 2.0 PKCE bootstrap.

Run locally (NOT inside the container — this opens your browser):

  uv run python -m wire.scripts.twitter_auth

Reads TWITTER_CLIENT_ID + TWITTER_CLIENT_SECRET from env, runs the PKCE
flow on http://127.0.0.1:8765/callback, and writes the token blob to
/data/secrets/twitter-token.json (or wherever twitter.access_token_path
in config.yaml points).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import secrets
import sys
import urllib.parse
import webbrowser
from pathlib import Path

import httpx
from aiohttp import web

from wire.config import load_config
from wire.twitter.oauth import TOKEN_URL, TwitterToken, save_token

# Canonical authorize URL since 2026; the twitter.com host still redirects.
AUTH_URL = "https://x.com/i/oauth2/authorize"
DEFAULT_REDIRECT_PORT = 8765
SCOPES = ["tweet.read", "tweet.write", "users.read", "offline.access"]


def _make_pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return verifier, challenge


async def _exchange_code(
    *,
    client_id: str,
    client_secret: str | None,
    code: str,
    code_verifier: str,
    redirect_uri: str,
) -> TwitterToken:
    """Exchange the auth code for tokens. Confidential clients (apps that
    have a client secret in the X portal) must authenticate with HTTP Basic;
    public clients pass client_id in the body only."""
    auth = (client_id, client_secret) if client_secret else None
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
                "client_id": client_id,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            auth=auth,
        )
    if resp.status_code != 200:
        raise SystemExit(f"Token exchange failed: {resp.status_code} {resp.text[:300]}")
    return TwitterToken.from_payload(resp.json())


async def _run_flow(
    *,
    client_id: str,
    client_secret: str | None,
    token_path: Path,
    port: int,
) -> None:
    verifier, challenge = _make_pkce_pair()
    state = secrets.token_urlsafe(24)
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    auth_params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    auth_url = f"{AUTH_URL}?{auth_params}"

    code_future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def callback(request: web.Request) -> web.Response:
        got_state = request.query.get("state")
        code = request.query.get("code")
        if got_state != state or not code:
            return web.Response(status=400, text="Bad state or missing code")
        if not code_future.done():
            code_future.set_result(code)
        return web.Response(text="Got it. You can close this tab.")

    app = web.Application()
    app.router.add_get("/callback", callback)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()

    print("Open this URL in your browser if it doesn't open automatically:")
    print(f"  {auth_url}")
    print()
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    print("Waiting for OAuth callback (Ctrl+C to cancel)...")
    code = await code_future
    await runner.cleanup()

    print("Exchanging code for tokens...")
    token = await _exchange_code(
        client_id=client_id,
        client_secret=client_secret,
        code=code,
        code_verifier=verifier,
        redirect_uri=redirect_uri,
    )
    save_token(token_path, token)
    print(f"✅ Saved token to {token_path}")


def main() -> int:
    cfg_path = Path(os.environ.get("WIRE_CONFIG_PATH", "/data/config.yaml"))
    if not cfg_path.exists():
        print(f"Config not found at {cfg_path}; set WIRE_CONFIG_PATH or copy data/config.yaml.example.")
        return 1
    cfg = load_config(cfg_path)
    client_id = os.environ.get(cfg.twitter.client_id_env)
    if not client_id:
        print(f"Env var {cfg.twitter.client_id_env} is empty.")
        return 1
    # Optional for public clients; required for confidential clients (which is
    # what the X portal creates by default for "Web App, Automated App or Bot").
    client_secret = os.environ.get(cfg.twitter.client_secret_env) or None
    port = int(os.environ.get("WIRE_OAUTH_PORT", str(DEFAULT_REDIRECT_PORT)))
    asyncio.run(
        _run_flow(
            client_id=client_id,
            client_secret=client_secret,
            token_path=cfg.twitter.access_token_path,
            port=port,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

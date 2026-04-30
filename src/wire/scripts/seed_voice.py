"""Bootstrap the voice profile from your existing X timeline.

Run locally once:
  uv run python -m wire.scripts.seed_voice

Reads up to ~100 of your authenticated tweets via the X API, runs the voice
profile generator, and writes the result to the voice_profile table. Subsequent
weekly regenerations will use bot-posted tweets, so this seed is one-shot.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from wire.config import load_config
from wire.db import session as db_session
from wire.llm.provider import build_provider
from wire.twitter.client import TwitterClient
from wire.twitter.oauth import load_token
from wire.voice.profile_generator import regenerate_voice_profile


async def _fetch_recent_tweets(client: TwitterClient, *, max_n: int = 100) -> list[str]:
    """Fetch the authenticated user's recent tweets via /2/users/me + /2/users/:id/tweets."""
    headers = await client._auth_headers()
    http = client._get_http()
    me = await http.get("https://api.twitter.com/2/users/me", headers=headers)
    me.raise_for_status()
    user_id = me.json()["data"]["id"]
    out: list[str] = []
    next_token: str | None = None
    while len(out) < max_n:
        params = {"max_results": "50", "tweet.fields": "created_at,text"}
        if next_token:
            params["pagination_token"] = next_token
        resp = await http.get(
            f"https://api.twitter.com/2/users/{user_id}/tweets",
            params=params,
            headers=headers,
        )
        resp.raise_for_status()
        body = resp.json()
        for t in body.get("data", []):
            out.append(t.get("text", ""))
            if len(out) >= max_n:
                break
        meta = body.get("meta", {})
        next_token = meta.get("next_token")
        if not next_token:
            break
    return out


async def _run() -> int:
    cfg_path = Path(os.environ.get("WIRE_CONFIG_PATH", "/data/config.yaml"))
    cfg = load_config(cfg_path)

    db_path = Path(os.environ.get("WIRE_DB_PATH", "/data/wire.db"))
    db_session.init(db_path)

    if load_token(cfg.twitter.access_token_path) is None:
        print("No twitter token. Run wire.scripts.twitter_auth first.")
        return 1
    client_id = os.environ.get(cfg.twitter.client_id_env)
    if not client_id:
        print(f"Env var {cfg.twitter.client_id_env} is empty.")
        return 1

    twitter = TwitterClient(
        client_id=client_id,
        client_secret=os.environ.get(cfg.twitter.client_secret_env, ""),
        token_path=cfg.twitter.access_token_path,
    )
    try:
        tweets = await _fetch_recent_tweets(twitter, max_n=100)
        print(f"Fetched {len(tweets)} tweets from your timeline.")
        if not tweets:
            print("No tweets — nothing to seed.")
            return 0
        provider = build_provider(cfg.llm)
        profile = await regenerate_voice_profile(cfg, provider, posts_override=tweets)
    finally:
        await twitter.aclose()

    if profile is None:
        print("Voice profile generation returned no content.")
        return 1
    print("✅ Voice profile written:")
    print(profile)
    return 0


def main() -> None:
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()

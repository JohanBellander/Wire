---
name: wire-deploy
description: Deploy and operate the Wire bot in production. Use when the user wants to ship code, redeploy the container, run scripts against production, check live logs, or diagnose production-only issues.
when-to-use: |
  - "redeploy", "ship", "push to prod", "deploy" requests for Wire
  - inspecting production wire.db (events, drafts, sessions, llm_calls)
  - tailing or grepping production logs
  - troubleshooting issues that only appear in prod (drafts not arriving,
    container crash-looping, /status shows "never", etc.)
  - rotating secrets, updating /opt/wire-data/config.yaml or repos.yaml
---

# Wire production operations

## Layout

| | |
|---|---|
| Server | `johan@gary` (SSH key auth set up) |
| Coolify project ID prefix | `j13i32n8rrvzsxpydl404f6v` (stable across deploys) |
| Container name | `j13i32n8rrvzsxpydl404f6v-<random>` (suffix changes per deploy) |
| Bind mount | host `/opt/wire-data` → container `/data` |
| GitHub repo | <https://github.com/JohanBellander/Wire> |
| Healthcheck | `GET /health` on port 8080 (Coolify internal) |

## Find the running container

```bash
ssh johan@gary
WIRE=$(docker ps --filter name=j13i32n8rrvzsxpydl404f6v -q)
echo "Container: $WIRE"
```

The image tag's last 7 chars match the Git commit hash that was deployed:
```bash
docker ps --filter name=j13i32n8rrvzsxpydl404f6v --format '{{.Image}}'
# j13i32n8rrvzsxpydl404f6v:abcdef0123...   ← last 7 chars = commit
```

## Run a one-shot script in production

```bash
ssh johan@gary 'docker exec $(docker ps --filter name=j13i32n8rrvzsxpydl404f6v -q) python -m wire.scripts.<script>'
```

Available scripts:
- `wire.scripts.dry_run` — re-ingest last 24h into a TEMP DB, print kept-vs-filtered. Read-only against production data.
- `wire.scripts.inspect [hours]` — diagnostic summary (events, triage, sessions, drafts, decisions, posts, LLM cost). Default 24h.
- `wire.scripts.seed_voice` — bootstrap voice profile from X timeline. One-shot; subsequent regenerations run weekly via the scheduler.
- `wire.scripts.db_init` — `alembic upgrade head` (also runs on container boot, idempotent).

## Tail logs

```bash
ssh johan@gary 'docker logs --tail 50 -f $(docker ps --filter name=j13i32n8rrvzsxpydl404f6v -q)'
```

Or one-shot:
```bash
ssh johan@gary 'docker logs --tail 100 $(docker ps --filter name=j13i32n8rrvzsxpydl404f6v -q)' 2>&1 | grep -E '"event":|FATAL|wire.fatal'
```

## Redeploy

1. Code: Johan merges the PR to `main`; agents never push to `main` and land merges with the global
   `prod-landing` skill (auto-deploy is OFF in Coolify; manual click required)
2. Coolify UI → Wire app → **Deploy**
3. Watch logs (above) until `wire.ready` appears
4. Verify in Telegram: send `/status` to the Wire bot

After redeploy, the container name's suffix will have changed. Re-run the
`WIRE=$(docker ps ...)` lookup before any docker exec.

## Update config/secrets without code change

The `/opt/wire-data` directory is bind-mounted, so files placed there are
visible in the container at `/data` immediately:

```bash
# Locally, edit data/config.yaml or data/repos.yaml, then:
pwsh ./upload-to-server.ps1   # syncs to /opt/wire-data on gary
```

Or directly on the server with vim. After editing, **Restart** the container
in Coolify (not Deploy — Restart preserves the build).

## Secret rotation

| secret | how to rotate |
|---|---|
| `ANTHROPIC_API_KEY` | Console → API Keys → revoke + create. Update the env var in Coolify → Restart. |
| `TELEGRAM_BOT_TOKEN` | `@BotFather` → `/revoke` → `/token`. Update env var in Coolify → Restart. |
| `TWITTER_CLIENT_SECRET` | console.x.com → Keys and tokens → Regenerate OAuth 2.0 secret. Update env var in Coolify. Re-run `bootstrap-twitter.ps1` locally to get a fresh `data/secrets/twitter-token.json`. Upload via `upload-to-server.ps1`. Restart. |
| GitHub App private key | App settings → Generate new private key. Place locally at `data/secrets/github-app.pem`. `upload-to-server.ps1`. Restart. |

## Common diagnostics

**`/status` shows `last_ingestion_at: never`**
- New container; the first poll fires on boot but takes ~10s to complete. Wait, retry.
- If it persists, `docker logs` for `wire.poll.ingest_failed` exceptions.

**Drafts aren't arriving**
- `inspect 24` — is the agent triaging events at all? Are scores all <0.3?
- Triage scores all near zero usually means the prompt context is poor — verify `_enrich_events` is running (check git log for any recent regression on `poller.py`).
- Sessions might all be `below_threshold_skip` — check `inspect`'s session table.

**Container crash-loops**
- `docker logs` first 30 lines: `wire.config.missing` → bind mount or config file issue. `wire.fatal: InvalidToken` → bad Telegram token. `wire.fatal: ConfigError` → pydantic validation failed.
- Restart in Coolify after fixing.

**Surprise spend**
- `/budget` in Telegram. Auto-pauses drafting at 100%; `/extend [usd]` to bump.
- If unexpectedly high, `inspect 168 --top 30` to see what's been consuming the most.

## Backfill / re-process events

There's no built-in backfill. Old events with stripped payloads stay
mis-triaged. Two paths:

1. **Wait it out** — the last 7 days of events are what feeds the digest;
   newly-ingested enriched events overtake them naturally.
2. **Manual surgery** — `docker exec ... sqlite3 /data/wire.db` and re-trigger
   triage on specific event ids by setting `triage_score = NULL` then
   waiting for the next poll cycle. Risky; only do this if a specific event
   needs to surface.

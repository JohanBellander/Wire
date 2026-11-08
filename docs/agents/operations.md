# Wire operations reference

Moved from the old `CLAUDE.md`. Read before touching production, env vars or the llama.cpp fallback.
Production commands are also in the `wire-deploy` skill. Deploys happen only after Johan merges, via the global
`prod-landing` skill; never push to `main`.

## Production deployment

| | |
|---|---|
| Server | `johan@gary` (Coolify-managed Linux box) |
| Repo | <https://github.com/JohanBellander/Wire> (public) |
| Coolify project ID prefix | `j13i32n8rrvzsxpydl404f6v` |
| Bind mount | host `/opt/wire-data` → container `/data` |
| Healthcheck | `GET /health` on port 8080 |
| Env vars (in Coolify) | Secrets: `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TWITTER_CLIENT_ID`, `TWITTER_CLIENT_SECRET`. LLM selection: `WIRE_LLM_PROVIDER`, `WIRE_CLAUDE_DRAFTING_MODEL`, `WIRE_CLAUDE_TRIAGE_MODEL`, `WIRE_CLAUDE_VOICE_PROFILE_MODEL`, `WIRE_CLAUDE_DIGEST_MODEL`. llama.cpp (when `WIRE_LLM_PROVIDER=llamacpp`): `WIRE_LLAMACPP_BASE_URL`, `WIRE_LLAMACPP_MODEL`, optional `WIRE_LLAMACPP_TIMEOUT_SECONDS`, `WIRE_LLAMACPP_TEMPERATURE`, `LLM_API_KEY`. See `.env.example`. | <!-- pragma: allowlist secret -->

### Find the running container

```bash
ssh johan@gary
WIRE=$(docker ps --filter name=j13i32n8rrvzsxpydl404f6v -q)
```

The Coolify-generated container suffix changes on every redeploy; the prefix is stable.

### Redeploy flow

1. Johan merges the PR to `main` (agents never push to `main`)
2. Coolify UI → Wire app → **Deploy** (auto-deploy is intentionally off); agents do this through `prod-landing`
3. Watch logs in Coolify or via `ssh johan@gary "docker logs $(docker ps --filter name=j13i32n8rrvzsxpydl404f6v -q) --tail 30 -f"`
4. After `wire.ready` log line appears, send `/status` in Telegram to verify

### Update config without code change

For YAML (repos.yaml, session/quiet_hours/etc): edit files in `/opt/wire-data/` directly on the server (config is bind-mounted), then **Restart** the container in Coolify (not Deploy — Restart preserves the existing build).

For LLM model / provider / endpoint changes: edit the `WIRE_*` env vars in the Coolify UI, then **Restart**. No SSH or YAML edit required.

### llama.cpp observability

`/status` in Telegram has a 🧠 brain block: primary backend, fallback, which one was used last, and the 24h fallback rate (counts from `llm_calls.fallback`). When `WIRE_LLM_PROVIDER=claude`, the block collapses to "claude only".

The `wire.llamacpp.unreachable_warning` log line at startup means the configured llama.cpp host didn't respond to `GET /models` within 5s. Container starts anyway; `FallbackProvider` routes everything to Claude at runtime until the local backend recovers.

When debugging "why is Wire falling back to Claude every call?":

1. **Schema format mode** — `LlamaCppProvider` sends OpenAI's `response_format: {"type": "json_schema", strict: true}`. If the server rejects schema-mode it retries once with the looser `{"type": "json_object"}`. Stale servers that reject both cascade to Claude.
2. **Temperature** — defaults to 0.5 (`WIRE_LLAMACPP_TEMPERATURE`). <0.3 hurts voice variation; >0.7 hurts schema adherence.
3. **Auth** — 401/403 raises `LLMAuthError` and is *not* caught by `FallbackProvider`. Check `LLM_API_KEY`.

If the local backend is unhealthy and you need drafts now, set `WIRE_LLM_PROVIDER=claude` in Coolify and restart. Cost goes up but drafts arrive.

## Local runs that need config or network

```bash
uv run python -m wire.scripts.dry_run     # local ingestion sanity (temp DB; needs GitHub App config)
uv run alembic upgrade head               # migrate (also runs on container boot)
uv run alembic revision --autogenerate -m "descr"  # generate migration after model change
WIRE_DEV=1 uv run python -m wire.main     # /health on 8080 without /data/config.yaml; Ctrl+C to stop
```

Production diagnostic:
```bash
ssh johan@gary 'docker exec $(docker ps --filter name=j13i32n8rrvzsxpydl404f6v -q) python -m wire.scripts.inspect 24'
```

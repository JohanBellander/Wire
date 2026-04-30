# CLAUDE.md

Operational guide for Claude Code working in this repo. Read this first.

## What Wire is

A self-hosted bot that watches a configured GitHub org, drafts X/Twitter posts about post-worthy activity using Claude (with optional Ollama primary + Claude fallback), gates everything behind Telegram approval, and learns from approve/reject/edit decisions plus post performance via prompt context. Single user, single X account, single GitHub org. Single Docker container deployed via Coolify.

Authoritative documents:
- [`SPEC.MD`](./SPEC.MD) — full design (architecture, data model, components, configuration)
- [`SETUP.md`](./SETUP.md) — first-time external setup (GitHub App, Telegram, X API, Anthropic, optional Ollama)
- [`README.md`](./README.md) — quick start

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                  Wire (single container)                    │
│                                                             │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────┐     │
│  │  Ingestion   │──▶│   Drafting   │──▶│   Telegram   │     │
│  │   (cron)     │   │ (per session)│   │     bot      │     │
│  └──────────────┘   └──────────────┘   └──────────────┘     │
│         │                  │                  │             │
│         ▼                  ▼                  ▼             │
│  ┌──────────────────────────────────────────────────┐       │
│  │             SQLite (mounted volume)              │       │
│  └──────────────────────────────────────────────────┘       │
│         ▲                  ▲                  ▲             │
│         │                  │                  │             │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────┐     │
│  │   Metrics    │   │     LLM      │   │  X poster    │     │
│  │ fetch (cron) │   │   provider   │   │              │     │
│  └──────────────┘   └──────────────┘   └──────────────┘     │
│                            │                                │
│                  ┌─────────┴─────────┐                      │
│                  ▼                   ▼                      │
│            ┌─────────┐         ┌──────────┐                 │
│            │ Ollama  │ fail──▶ │  Claude  │                 │
│            └─────────┘         └──────────┘                 │
└─────────────────────────────────────────────────────────────┘
```

Five logical components, all in one Python process orchestrated by APScheduler:

- **Ingestion** — polls GitHub on a schedule, runs pre-LLM filters + per-event Haiku triage, writes events to SQLite. Enriches stripped `/events` payloads via `/compare/{base}...{head}` and `/pulls/{n}` detail endpoints.
- **Session detection + drafting** — clusters events into per-repo work sessions (idle / max-hours / immediate-trigger close). For closed sessions, calls Sonnet with the full prompt context (voice profile + recent posts + recent decisions + session events).
- **Telegram bot** — sends drafts to a fixed chat with inline keyboard (✅ / ✏️ / ❌ / 💤), receives decisions, posts approved content to X. Slash commands for status/budget/pause/resume/saved/digest/repos/extend.
- **Metrics fetcher** — pulls X engagement data daily; appends one metrics row per fetch (history preserved).
- **Weekly digest** — Monday 09:00 Stockholm summary message.

## Production deployment

| | |
|---|---|
| Server | `johan@gary` (Coolify-managed Linux box) |
| Repo | <https://github.com/JohanBellander/Wire> (public) |
| Coolify project ID prefix | `j13i32n8rrvzsxpydl404f6v` |
| Bind mount | host `/opt/wire-data` → container `/data` |
| Healthcheck | `GET /health` on port 8080 |
| Env vars (in Coolify) | `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TWITTER_CLIENT_ID`, `TWITTER_CLIENT_SECRET` |

### Find the running container

```bash
ssh johan@gary
WIRE=$(docker ps --filter name=j13i32n8rrvzsxpydl404f6v -q)
```

The Coolify-generated container suffix changes on every redeploy; the prefix is stable.

### Redeploy flow

1. `git push origin main`
2. Coolify UI → Wire app → **Deploy** (auto-deploy is intentionally off)
3. Watch logs in Coolify or via `ssh johan@gary "docker logs $(docker ps --filter name=j13i32n8rrvzsxpydl404f6v -q) --tail 30 -f"`
4. After `wire.ready` log line appears, send `/status` in Telegram to verify

### Update config without code change

Edit files in `/opt/wire-data/` directly on the server (config is bind-mounted), then **Restart** the container in Coolify (not Deploy — Restart preserves the existing build).

## Key file locations

| concept | file |
|---|---|
| Entrypoint, scheduler, signal handling | `src/wire/main.py` |
| Health endpoint | `src/wire/health.py` |
| Config validation (pydantic) | `src/wire/config.py` |
| DB models, migrations | `src/wire/db/models.py`, `src/wire/db/migrations/` |
| Prompts (text files) | `src/wire/llm/prompts/{drafting,triage,voice_profile,digest}.txt` |
| LLM provider abstraction | `src/wire/llm/provider.py` |
| Cost estimation, budget tracking | `src/wire/llm/budget.py` |
| Budget alert decisions | `src/wire/llm/alerts.py` |
| Cache markers for Anthropic | `src/wire/llm/caching.py` |
| GitHub App auth + events fetch | `src/wire/ingestion/github_client.py` |
| Pre-LLM event filters | `src/wire/ingestion/filters.py` |
| Per-event Haiku triage | `src/wire/ingestion/triage.py` |
| Polling orchestration + enrichment | `src/wire/ingestion/poller.py` |
| Session boundary detection | `src/wire/sessions/detector.py` |
| Drafting orchestration + prompt | `src/wire/drafting/drafter.py` |
| Telegram bot wiring | `src/wire/telegram/bot.py` |
| Approve / edit / reject handlers | `src/wire/telegram/handlers.py` |
| Slash commands | `src/wire/telegram/commands.py` |
| X posting client + thread chains | `src/wire/twitter/client.py` |
| X OAuth 2.0 PKCE + refresh | `src/wire/twitter/oauth.py` |
| Daily metrics fetcher | `src/wire/metrics/fetcher.py` |
| Voice profile generator | `src/wire/voice/profile_generator.py` |
| Weekly digest builder | `src/wire/digest/builder.py` |
| One-shot CLI scripts | `src/wire/scripts/{db_init,dry_run,inspect,seed_voice,twitter_auth}.py` |
| PowerShell ops helpers | `bootstrap-twitter.ps1`, `upload-to-server.ps1` |
| One-off SQLite payload diagnostic | `scripts/inspect_payload.py` |

## Critical conventions (load-bearing)

These are the patterns it took production traffic to find. Do not break them.

### All DB times stored as naive UTC

`utc_now()` in `src/wire/db/models.py` returns `datetime.now(timezone.utc).replace(tzinfo=None)`. Every timestamp column stores naive UTC. Convert to `Europe/Stockholm` only at boundaries (Telegram messages, weekly digest header). The `quiet_hours.timezone` config is consulted by `is_in_quiet_hours()` in `drafter.py`.

### `repos.yaml` allowlist enforced at ingestion

`filter_repo_allowlist` in `ingestion/filters.py` rejects any event whose repo isn't in `repos.yaml`, *even though* `github_client` only fetches listed repos. Belt + suspenders. Do not remove this filter — it's the safety net that keeps work / private repos out of public posts.

### Never `json.loads(resp.content)` directly

Always `parse_json_lenient(resp.content)` from `wire.llm.provider`. Claude wraps structured outputs in markdown fences (` ```json ... ``` `) often enough that the strict `json.loads` path will fail in production. The lenient parser strips fences and extracts the first balanced `{...}` if both fail.

### Every `provider.complete()` caller must `log_llm_call(resp)`

From `wire.llm.budget`. Without this, the call cost is invisible to budget tracking and the inspect script. Triage, drafting, voice profile, and digest all use it. **If you add a new LLM caller, this is non-optional.** Tested by `test_budget.py`.

### GitHub `/events` payloads are stripped — always enrich

GitHub's `/events` endpoint returns a stripped form of `pull_request` (only `id`, `number`, `url`, `base`, `head`) and `PushEvent` (no commits at all). `_enrich_events` in `ingestion/poller.py` runs between `list_events` and `normalize_raw_event` to backfill via `/compare/{base}...{head}` and `/pulls/{n}`. Tested by `test_enrichment.py`. **If you skip enrichment, every PR triages with empty title and every Push triages with no commit messages.**

### Secrets only via env var or `/data/secrets/`

Never in code, never in git. `.gitignore` covers `.env`, `data/config.yaml`, `data/repos.yaml`, `data/secrets/`. The five secret env vars are listed in `.env.example`. GitHub App private key + X OAuth token live as files under `/data/secrets/`.

### Claude model strings come from `config.yaml` verbatim

Never substitute `claude-sonnet-4-6` etc. Model routing in `LLMConfig.claude` (drafting / triage / voice_profile / digest). Per-task routing applies when `provider: claude` AND in the fallback path when `provider: ollama`.

## Common pitfalls (the day-one bugs, preserved here so they don't repeat)

- **APScheduler `IntervalTrigger` doesn't fire on boot.** Default first-fire is `now+interval`, not `now`. Pass `next_run_time=datetime.now()` to `add_job` so freshly-booted containers populate `last_ingestion_at` immediately. See `main.py`.
- **Triage previously discarded `LLMResponse`** so its calls weren't logged. Fixed; the lesson is to always preserve and log the response. Easy regression to introduce when adding a new task.
- **GitHub `/events` caps at ~300 events / 3 pages of `per_page=100`.** Beyond page 3 it returns 422. `list_events` defaults `max_pages=3` and treats 422 as natural end-of-pagination via `_is_retriable_github_error`.
- **5xx from GitHub must retry.** The retry filter is `retry_if_exception` with `_is_retriable_github_error` (transport + 5xx). `retry_if_exception_type((TransportError, TimeoutException))` alone misses HTTP 5xx — don't go back to that.
- **Coolify container names are UUID-prefixed.** Searching for `wire` in `docker ps` finds nothing. Use the project ID prefix `j13i32n8rrvzsxpydl404f6v`.
- **OAuth confidential clients need HTTP Basic auth.** Both `_exchange_code` (in `scripts/twitter_auth.py`) and `refresh_access_token` (in `twitter/oauth.py`) pass `auth=(client_id, client_secret)` when the secret is set. Body-only `client_id` works for public clients but fails for the confidential type (which is what the X portal creates by default for "Web App, Automated App or Bot").
- **Windows `WIRE_DEV=1`** allows boot without `/data/config.yaml` for local `/health`-only smoke testing. Don't ship this — production must fail-fast on missing config.
- **`docker volume ls | grep wire` returns nothing on a Coolify host** because Coolify uses managed volume names. Use the bind-mount path `/opt/wire-data` directly.

## Development recipes

```bash
uv sync                                   # install runtime + dev deps
uv run pytest -q                          # full suite (~15 s, 133 tests)
uv run pytest tests/test_filters.py -v    # one file
uv run ruff check src tests               # lint
uv run ruff format src tests              # format
uv run python -m wire.scripts.dry_run     # local ingestion sanity (temp DB)
uv run alembic upgrade head               # migrate (also runs on container boot)
uv run alembic revision --autogenerate -m "descr"  # generate migration after model change
```

Local dev container:
```bash
WIRE_DEV=1 uv run python -m wire.main     # /health on 8080; Ctrl+C to stop
```

Production diagnostic:
```bash
ssh johan@gary "docker exec \$(docker ps --filter name=j13i32n8rrvzsxpydl404f6v -q) python -m wire.scripts.inspect 24"
```

## Test layout

| file | covers |
|---|---|
| `tests/test_smoke.py` | version + `/health` endpoint shape |
| `tests/test_config.py` | pydantic validation of `config.yaml` + `repos.yaml` |
| `tests/test_db.py` | SQLAlchemy models + Alembic upgrade |
| `tests/test_provider_fallback.py` | Claude / Ollama / Fallback + `parse_json_lenient` |
| `tests/test_filters.py` | pre-LLM event filters (bot, branch, conventional commits, allowlist) |
| `tests/test_github_client.py` | pagination, 422 grace, 5xx retry, `compare_commits` |
| `tests/test_enrichment.py` | `_enrich_events` PR + push detail backfill |
| `tests/test_poller_first_run.py` | `bot_state`-backed first-run tracking |
| `tests/test_sessions.py` | idle / max-hours / immediate-trigger boundaries |
| `tests/test_prompt_building.py` | drafting prompt assembly + caching markers |
| `tests/test_telegram_handlers.py` | approve / edit / reject / save + diff format + expiry |
| `tests/test_twitter.py` | single tweet, threads, auth, rate limit, refresh |
| `tests/test_digest.py` | weekly digest gather + format |
| `tests/test_budget.py` | spend tracking, threshold, `/extend` |
| `tests/test_e2e.py` | events → triage → session → draft → Telegram-send chain |

## Adding a new feature checklist

1. Implement under `src/wire/`. If it touches LLM calls, follow the conventions above.
2. Add tests under `tests/test_<thing>.py`. Mock all external services (`respx` for httpx, `unittest.mock` for Telegram/X clients).
3. If your change introduces a new convention or surprise, **document it in this file** under "Critical conventions" or "Common pitfalls."
4. Local validation: `uv run ruff check src tests && uv run pytest -q`
5. Open a PR; CI runs the same checks (`.github/workflows/ci.yml`).
6. Merge to main; redeploy via Coolify UI.

## Adding a new prompt

1. Drop `src/wire/llm/prompts/<name>.txt`. Plain text, no frontmatter.
2. Reference it from the relevant module via `Path(__file__).resolve().parents[N] / "llm" / "prompts" / "<name>.txt"`.
3. The triage prompt's score guidance + structured-output schema are the model to follow — see `prompts/triage.txt` and `prompts/drafting.txt`.

## When in doubt

- "Where does X live?" → check the Key file locations table above.
- "Why was this written this way?" → check Common pitfalls; many oddities are scars from production.
- "Will this break something?" → run `uv run pytest -q`; the e2e test in `test_e2e.py` exercises most of the pipeline end-to-end.
- "Is this secret safe?" → if it's not in `.env` or `/data/secrets/`, it's wrong. Never commit secrets to the repo.

# Wire architecture reference

Moved from the old `CLAUDE.md`. Rules live in `AGENTS.md`; this is lookup material.

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
│                  ┌─────────┴──────────┐                     │
│                  ▼                    ▼                     │
│           ┌────────────┐         ┌──────────┐               │
│           │ llama.cpp  │ fail──▶ │  Claude  │               │
│           └────────────┘         └──────────┘               │
└─────────────────────────────────────────────────────────────┘
```

Five logical components, all in one Python process orchestrated by APScheduler:

- **Ingestion** — polls GitHub on a schedule, runs pre-LLM filters + per-event Haiku triage, writes events to SQLite. Enriches stripped `/events` payloads via `/compare/{base}...{head}` and `/pulls/{n}` detail endpoints.
- **Session detection + drafting** — clusters events into per-repo work sessions (idle / max-hours / immediate-trigger close). For closed sessions, calls Sonnet with the full prompt context (voice profile + recent posts + recent decisions + session events).
- **Telegram bot** — sends drafts to a fixed chat with inline keyboard (✅ / ✏️ / ❌ / 💤), receives decisions, posts approved content to X. Slash commands for status/budget/pause/resume/saved/digest/repos/extend.
- **Metrics fetcher** — pulls X engagement data daily; appends one metrics row per fetch (history preserved).
- **Weekly digest** — Monday 09:00 Stockholm summary message.

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

## Test layout

| file | covers |
|---|---|
| `tests/test_smoke.py` | version + `/health` endpoint shape |
| `tests/test_config.py` | pydantic validation of `config.yaml` + `repos.yaml` |
| `tests/test_db.py` | SQLAlchemy models + Alembic upgrade |
| `tests/test_llamacpp_provider.py` | Claude / llama.cpp / Fallback + `parse_json_lenient` |
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

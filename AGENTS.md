# Wire

A self-hosted bot that watches a configured GitHub org, drafts X/Twitter posts about post-worthy activity with the
Claude API (optionally an OpenAI-compatible llama.cpp server as primary with Claude as fallback), gates everything
behind Telegram approval, and learns from approve/reject/edit decisions plus post performance via prompt context.
Single user, single X account, single GitHub org, one Docker container.

Authoritative documents: `SPEC.MD` (full design), `SETUP.md` (GitHub App, Telegram, X API, Anthropic, llama.cpp),
`README.md` (quick start).

## Stack
- Python 3.12, `uv` (`uv.lock`), hatchling package `src/wire`. APScheduler runs everything in one process.
- SQLAlchemy + Alembic on SQLite (`/data/wire.db`), httpx + tenacity, gidgethub, pydantic(-settings),
  python-telegram-bot, tweepy, `anthropic` SDK, structlog.
- Dev: pytest (+ pytest-asyncio, respx, freezegun), ruff (lint + format), pre-commit (ruff, detect-secrets with `.secrets.baseline`).

## Layout
- `src/wire/main.py` — entrypoint, scheduler, signal handling. `health.py` — `GET /health` on 8080.
- `src/wire/ingestion/` (GitHub polling, filters, triage), `sessions/`, `drafting/`, `telegram/`, `twitter/`,
  `metrics/`, `voice/`, `digest/`, `events/` (shared event formatting), `llm/` (provider, budget, prompts as `.txt`), `db/` (models + migrations),
  `scripts/` (one-shot CLIs: `db_init`, `dry_run`, `inspect`, `seed_voice`, `twitter_auth`).
- `tests/` — one file per component; `test_e2e.py` runs events → triage → session → draft → Telegram.
- `docs/agents/architecture.md` — read for the component diagram, the key-file table and the test map.
- `docs/agents/operations.md` — read before anything that touches production, env vars or llama.cpp fallback.

## Commands
```bash
uv sync                                   # install runtime + dev deps (CI: uv sync --frozen)
uv run pytest -q                          # full suite, ~35 s, no services needed
uv run pytest tests/test_filters.py -v    # one file
uv run ruff check src tests               # lint (CI)
uv run ruff format --check src tests      # format check (CI); drop --check to format
```
Local runs that need config or network (`dry_run`, `WIRE_DEV=1` boot, Alembic revisions) are in
`docs/agents/operations.md`.

## Conventions (load-bearing; production found every one of these)
- **Naive UTC everywhere.** `utc_now()` in `db/models.py` returns naive UTC; convert to `Europe/Stockholm` only at
  boundaries (Telegram messages, digest header). Quiet hours: `is_in_quiet_hours()` in `drafter.py`.
- **`repos.yaml` allowlist at ingestion.** `filter_repo_allowlist` in `ingestion/filters.py` rejects events from
  unlisted repos even though `github_client` only fetches listed ones. Never remove it: it keeps work and private
  repos out of public posts.
- **Never `json.loads(resp.content)`.** Use `parse_json_lenient` from `wire.llm.provider`; models wrap structured
  output in markdown fences often enough to break the strict path.
- **Every `provider.complete()` caller calls `log_llm_call(resp)`** from `wire.llm.budget`, or the cost is invisible
  to budget tracking and `inspect` (`test_budget.py`). Non-optional for any new LLM caller.
- **Always enrich GitHub `/events` payloads.** They are stripped (PRs have no title, pushes no commits).
  `_enrich_events` in `ingestion/poller.py` backfills via `/compare/{base}...{head}` and `/pulls/{n}`
  (`test_enrichment.py`). Skip it and every PR triages with an empty title.
- **Secrets and LLM selection are env-only.** Never in code or git. `.env.example` is the contract;
  `WIRE_LLM_PROVIDER`, `WIRE_CLAUDE_*_MODEL`, `WIRE_LLAMACPP_*`. `config.yaml`'s `llm:` block keeps only
  `prompt_caching`, `monthly_budget_usd`, `budget_alert_threshold`. GitHub App key and X OAuth token are files
  under `/data/secrets/`.
- **Claude model strings come from env verbatim.** Never hard-code `claude-sonnet-4-6` and the like.
- **New prompts:** plain-text `src/wire/llm/prompts/<name>.txt`, loaded via `Path(__file__).resolve().parents[N]`;
  follow `triage.txt` / `drafting.txt` for score guidance and the structured-output schema.
- **New event types** are handled in `_summarize_event` (triage), `_format_event_line` (drafter) and
  `format_event_message` in `events/format.py` (inspect and Telegram).
- Tests mock every external service (`respx` for httpx, `unittest.mock` for Telegram/X). A new convention or
  surprise gets a line here.

## Deploy
Documented production: Coolify on **gary**, container name prefix `j13i32n8rrvzsxpydl404f6v`, bind mount
`/opt/wire-data` → `/data`, `GET /health` on 8080, `wire.ready` log line then `/status` in Telegram to verify.
As of 2026-11-08 Coolify lists no Wire application and `local-knowledge` `references/apps.md` has no entry, so ask
Johan whether Wire is deployed before acting on production. Coolify never deploys on its own: after Johan merges,
land the merge commit with the global `prod-landing` skill. Never push to `main`. Operations detail: the
`wire-deploy` skill and `docs/agents/operations.md`; homelab facts: global `local-knowledge`.

## Ways to hurt yourself
- APScheduler `IntervalTrigger` first fires at `now+interval`; pass `next_run_time=datetime.now()` (see `main.py`).
- Dropping an `LLMResponse` (triage once did) silently loses its cost; always keep and log it.
- GitHub `/events` caps at ~300 events (3 pages of 100); page 4 returns 422, which `list_events` treats as the end.
- GitHub 5xx must retry: keep `retry_if_exception(_is_retriable_github_error)`; type-only retry misses HTTP 5xx.
- X OAuth confidential clients need HTTP Basic auth (`auth=(client_id, client_secret)`) in both `_exchange_code`
  and `refresh_access_token`; body-only `client_id` fails.
- `WIRE_DEV=1` lets the app boot without `/data/config.yaml` for local `/health` smoke tests. Never ship it.
- On the Coolify host, `docker ps | grep wire` and `docker volume ls | grep wire` find nothing: use the UUID prefix
  and `/opt/wire-data`.
- `.dockerignore` excludes `*.md` and `tests`; the image only copies `pyproject.toml`, `uv.lock`, `README.md`,
  `alembic.ini` and `src/`.

## Review
Process: global skill `pr-crosscheck`. Tier triggers beyond the defaults:
- T2: `ingestion/filters.py` (allowlist), `telegram/handlers.py` (approve → post), `twitter/` (posting, OAuth),
  `llm/budget.py` and any new LLM caller, `db/migrations/`, `config.py`.
Invariants a reviewer must check: the conventions above, especially the allowlist, `log_llm_call`, enrichment and
lenient JSON parsing.
Failure looks like: a post about a private or work repo, a tweet sent without Telegram approval, a duplicate post,
or LLM spend that never reaches the budget.
Checks: `uv run pytest -q`, `uv run ruff check src tests`, `uv run ruff format --check src tests`.
Skip: `uv.lock`, `.secrets.baseline`, `data/*.example`. At most 3 minor findings per review.

## Agent setup
This repo is worked on by both Claude Code and Codex; keep them in sync.
- Rules: this file. `CLAUDE.md` only imports it — edit here.
- Skills: `.agents/skills/<name>/SKILL.md` is the source; `.claude/skills/` is a generated copy. After editing a skill run `node .agents/sync-skills.mjs` (`--check` to verify).
- Hooks: `.agents/hooks/format-edited.mjs`, registered in both `.claude/settings.json` and `.codex/hooks.json`.
- Permissions: `.claude/settings.json` `permissions.allow` and `.codex/rules/project.rules` list the same commands. Change both together.
- Handoff between agents: before stopping mid-task, write `.agent/handoff.md` (gitignored): goal, done, next, how to verify.

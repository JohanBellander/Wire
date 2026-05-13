# Wire — Build-in-Public Bot

A self-hosted agent that watches a GitHub organization, drafts X/Twitter posts about interesting activity, sends them to the user via Telegram for approval, posts approved content, and refines its drafting based on the user's decisions and post performance.

This document is the implementation spec. Hand it to Claude Code as the source of truth.

---

## 1. Goals & non-goals

**Goals**

- Monitor all repos (public + private) in a configured GitHub org.
- Use an LLM to identify post-worthy events and draft tweets in the user's voice.
- Send drafts to a Telegram bot. User approves, edits, or rejects every one. Nothing is posted without explicit approval.
- Post approved content to X via the API.
- Learn from approve/reject/edit decisions AND from post-performance metrics. Both signals feed into future drafting via prompt context — no fine-tuning, no reward functions.
- Toggle between Claude API and a self-hosted OpenAI-compatible endpoint (llama.cpp, vLLM, OpenRouter, etc.) for LLM calls. When a local endpoint is selected, Claude is the automatic fallback.
- Run as a single Docker container on a Coolify-managed server.
- Zero day-to-day maintenance after setup.

**Non-goals**

- Auto-posting without approval. Mandatory human approval is a hard requirement.
- Multi-user / multi-tenant. Single user, single X account, single GitHub org.
- A web UI. All interaction is via Telegram.
- Engagement-as-reward-function. Metrics are descriptive context for the LLM, never an optimization target.
- Media attachments. Text-only posts for v1.

---

## 2. High-level architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    Wire (single container)                   │
│                                                              │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────┐      │
│  │  Ingestion   │──▶│   Drafting   │──▶│   Telegram   │      │
│  │   (cron)     │   │ (per session)│   │     bot      │      │
│  └──────────────┘   └──────────────┘   └──────────────┘      │
│         │                  │                  │              │
│         ▼                  ▼                  ▼              │
│  ┌──────────────────────────────────────────────────┐        │
│  │             SQLite (mounted volume)              │        │
│  └──────────────────────────────────────────────────┘        │
│         ▲                  ▲                  ▲              │
│         │                  │                  │              │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────┐      │
│  │   Metrics    │   │     LLM      │   │  X poster    │      │
│  │ fetch (cron) │   │   provider   │   │              │      │
│  └──────────────┘   └──────────────┘   └──────────────┘      │
│                            │                                 │
│                  ┌─────────┴──────────┐                      │
│                  ▼                    ▼                      │
│           ┌────────────┐         ┌──────────┐                │
│           │ llama.cpp  │ fail──▶ │  Claude  │                │
│           └────────────┘         └──────────┘                │
└──────────────────────────────────────────────────────────────┘
       ▲                  ▲                       ▲
       │                  │                       │
   GitHub API       Telegram API              X API v2
```

Five logical components, all in one Python process orchestrated by APScheduler:

1. **Ingestion** — polls GitHub on a schedule, writes events to SQLite.
2. **Session detection + drafting** — clusters events into work sessions, drafts when sessions complete.
3. **Telegram bot** — sends drafts, receives approve/edit/reject, posts to X on approval.
4. **Metrics fetcher** — pulls X engagement data on a schedule.
5. **Weekly digest** — sends a summary of last week's posts and performance.

---

## 3. LLM provider abstraction

A single toggle (set via `WIRE_LLM_PROVIDER` env var): `claude` or `llamacpp`.

**When `WIRE_LLM_PROVIDER=claude`:**
- All calls go to Claude.
- Per-task model routing applies (Haiku for cheap tasks, Sonnet for drafting, etc.).
- No fallback (Claude is the only provider).

**When `WIRE_LLM_PROVIDER=llamacpp`:**
- All calls go to the local OpenAI-compatible endpoint first, regardless of task type.
- On failure (timeout, network error, invalid output, schema mismatch) → automatically retry against Claude using per-task routing.
- Auth failures (HTTP 401/403) surface as `LLMAuthError` and do NOT trigger fallback — they're configuration bugs.
- Fallback events are logged and surfaced in weekly digest.

Concrete classes live in `src/wire/llm/provider.py`:

- `ClaudeProvider` — uses the Anthropic SDK with per-task model strings (`WIRE_CLAUDE_DRAFTING_MODEL` etc.). Cache breakpoints on the last system block when `prompt_caching: true`.
- `LlamaCppProvider` — POSTs to `{WIRE_LLAMACPP_BASE_URL}/chat/completions` with Bearer auth from `LLM_API_KEY`. Sends OpenAI's `response_format: {"type": "json_schema", strict: true}` when a pydantic schema is supplied; retries once with `{"type": "json_object"}` on HTTP 400.
- `FallbackProvider` — wraps a local primary with `ClaudeProvider` as automatic fallback. Catches `LLMTransientError | LLMSchemaError | LLMError`; re-raises `LLMAuthError`.
- `build_provider(config)` — returns `ClaudeProvider` when `provider == "claude"`, otherwise `FallbackProvider(LlamaCppProvider, ClaudeProvider)`.

Every call to `provider.complete(...)` is logged to a `llm_calls` table with: task, provider used, fallback occurred (yes/no), input/output token counts, cost estimate, latency.

---

## 4. Configuration

Single YAML file at `/data/config.yaml`. Loaded at startup; changes require restart.

```yaml
# config.yaml

github:
  org: "your-org-name"
  app_id: 123456                  # GitHub App ID
  installation_id: 789012         # Installation ID for the org
  private_key_path: /data/secrets/github-app.pem
  poll_interval_minutes: 20

repos:
  config_path: /data/repos.yaml

llm:
  # Provider, Claude per-task models, and llama.cpp endpoint live in env vars
  # (WIRE_LLM_PROVIDER, WIRE_CLAUDE_*_MODEL, WIRE_LLAMACPP_*). See .env.example.
  prompt_caching: true
  monthly_budget_usd: 10
  budget_alert_threshold: 0.8     # alert at 80%

session:
  idle_minutes: 30                # session ends after 30 min of quiet
  max_hours: 4                    # force session boundary at 4h
  immediate_trigger_events:       # bypass session-clustering
    - release
    - milestone

quiet_hours:
  start: "22:00"
  end: "07:00"
  timezone: "Europe/Stockholm"

telegram:
  bot_token_env: TELEGRAM_BOT_TOKEN
  chat_id_env: TELEGRAM_CHAT_ID

twitter:
  client_id_env: TWITTER_CLIENT_ID
  client_secret_env: TWITTER_CLIENT_SECRET
  access_token_path: /data/secrets/twitter-token.json

metrics:
  fetch_cron: "0 9 * * *"         # daily 09:00 Stockholm
  posts_settle_days: 7            # only treat ≥7-day-old posts as performance signal

digest:
  cron: "0 9 * * 1"               # Monday 09:00 Stockholm

learning:
  recent_decisions_n: 20          # decisions injected as few-shot
  recent_posts_n: 30              # posts with metrics injected as voice/perf context

logging:
  level: INFO
  format: json
```

**Secrets** are loaded from environment variables, never committed:

```
ANTHROPIC_API_KEY
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
TWITTER_CLIENT_ID
TWITTER_CLIENT_SECRET
```

GitHub App private key and Twitter OAuth token are files mounted from a Coolify secret volume.

---

## 5. Repo allowlist

Hand-edited YAML at `/data/repos.yaml`:

```yaml
# repos.yaml

repos:
  - name: winetrackr
    visibility: public
    notes: "Public side project, post freely about features and debugging"

  - name: medianalyzer
    visibility: public
    notes: "Public, but boring infra commits — only post on releases or breakthroughs"

  - name: home-server-config
    visibility: private
    notes: "Personal infra, OK to post about lessons learned (not secrets)"

  # Repos NOT in this list are ignored entirely.
  # Visma repos are excluded by being absent.
```

The allowlist is enforced at the **ingestion layer** — events from non-listed repos never enter the database, so the LLM never sees them. Belt and suspenders for private/work content.

The `notes` field is injected into the drafting prompt as per-repo context.

---

## 6. Data model (SQLite)

```sql
-- Raw GitHub events
CREATE TABLE events (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  github_id       TEXT UNIQUE NOT NULL,
  repo            TEXT NOT NULL,
  event_type      TEXT NOT NULL,
  actor           TEXT,
  payload         JSON NOT NULL,
  occurred_at     TIMESTAMP NOT NULL,
  ingested_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  triage_score    REAL,
  triage_reason   TEXT,
  session_id      INTEGER REFERENCES sessions(id)
);
CREATE INDEX idx_events_repo_time ON events(repo, occurred_at);
CREATE INDEX idx_events_session ON events(session_id);

-- Work sessions
CREATE TABLE sessions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  repo            TEXT NOT NULL,
  started_at      TIMESTAMP NOT NULL,
  ended_at        TIMESTAMP,
  closed_reason   TEXT,                           -- 'idle' | 'max_hours' | 'immediate'
  drafted_at      TIMESTAMP
);

-- LLM-generated drafts
CREATE TABLE drafts (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id      INTEGER REFERENCES sessions(id),
  text            TEXT NOT NULL,
  reasoning       TEXT,
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  telegram_message_id INTEGER,
  status          TEXT NOT NULL DEFAULT 'pending' -- pending | approved | rejected | edited | expired
);
CREATE INDEX idx_drafts_status ON drafts(status);

-- User decisions
CREATE TABLE decisions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  draft_id        INTEGER NOT NULL REFERENCES drafts(id),
  decision        TEXT NOT NULL,                  -- approved | rejected | edited
  reject_reason   TEXT,
  edited_text     TEXT,
  edit_diff       TEXT,                           -- JSON diff
  decided_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Posted tweets
CREATE TABLE posts (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  draft_id        INTEGER REFERENCES drafts(id),
  twitter_id      TEXT UNIQUE NOT NULL,
  text            TEXT NOT NULL,
  posted_at       TIMESTAMP NOT NULL
);

-- Engagement metrics (snapshot per fetch — keep history)
CREATE TABLE metrics (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  post_id         INTEGER NOT NULL REFERENCES posts(id),
  fetched_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  impressions     INTEGER,
  likes           INTEGER,
  retweets        INTEGER,
  replies         INTEGER,
  bookmarks       INTEGER
);
CREATE INDEX idx_metrics_post ON metrics(post_id);

-- LLM accounting
CREATE TABLE llm_calls (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  task            TEXT NOT NULL,
  provider        TEXT NOT NULL,                  -- llamacpp | claude
  model           TEXT,
  fallback        BOOLEAN NOT NULL DEFAULT 0,
  input_tokens    INTEGER,
  output_tokens   INTEGER,
  cost_usd        REAL,
  latency_ms      INTEGER,
  called_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_llm_calls_month ON llm_calls(called_at);

-- Auto-generated voice profile
CREATE TABLE voice_profile (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  generated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  profile_text    TEXT NOT NULL
);
```

---

## 7. Component behavior

### 7.1 Ingestion (`src/wire/ingestion/`)

Runs every `poll_interval_minutes` (default 20).

1. Authenticate as GitHub App, get installation token for the org.
2. For each repo in `repos.yaml`, fetch events since `max(occurred_at)` from `events` for that repo.
   - Endpoints: `/repos/{owner}/{repo}/events`, plus `/repos/{owner}/{repo}/pulls` and `/repos/{owner}/{repo}/releases` for richer payloads.
3. **Pre-LLM filter** (free, code-only):
   - Skip: `dependabot`, `github-actions[bot]`, `renovate[bot]` actors
   - Skip: events on branches other than default branch (unless PR merge)
   - Skip: commits with messages matching `^(chore|ci|docs|style)(\(.+\))?:` (configurable)
   - Skip: events older than 24h on first ingestion (avoid backlog spam)
4. Insert surviving events into `events` (UNIQUE on `github_id` for dedup).
5. **Triage pass** (cheap LLM call): for each new event, score 0..1 for "post-worthiness" with a brief reason. Store on the event row.

### 7.2 Session detection (`src/wire/sessions/`)

Runs every `poll_interval_minutes` after ingestion.

1. Group ungrouped events by repo, ordered by `occurred_at`.
2. Assign each event to a session: same repo + within `idle_minutes` of previous event in that session.
3. Mark a session as **closed** when:
   - No new event in that session for `idle_minutes`, OR
   - Session duration exceeds `max_hours`, OR
   - Session contains an `immediate_trigger_events` type.
4. For closed sessions where `drafted_at IS NULL`, queue for drafting.

### 7.3 Drafting (`src/wire/drafting/`)

For each closed, undrafted session:

1. **Skip if all events have triage_score < 0.3.** Mark session as drafted with no draft. Log skip reason. (No expensive LLM call.)
2. **Skip if currently in quiet hours.** Defer until quiet hours end. Drafts that were generated earlier still get sent the moment quiet hours end (do not re-defer).
3. Build prompt context (see section 8).
4. Call `llm.complete(task="drafting", ...)` with structured output schema:
   ```python
   class DraftItem(BaseModel):
       text: str  # ≤ 280 chars; for threads use list of strings
       reasoning: str
       confidence: float  # 0..1

   class DraftResponse(BaseModel):
       skip_reason: str | None
       drafts: list[DraftItem]
   ```
5. If `skip_reason` is set, mark session as drafted, record skip reason, no Telegram message.
6. Otherwise, write each draft to `drafts` and send to Telegram (section 7.4).

### 7.4 Telegram bot (`src/wire/telegram/`)

Uses [python-telegram-bot](https://docs.python-telegram-bot.org/) with inline keyboards.

**Sending a draft:**

```
📝 Draft #47 · winetrackr
"Spent the morning chasing why Coolify deploys silently fail on
private repos. Turned out to be a permissions scope issue — the
GitHub App needed Contents:read AND Metadata:read."

Reasoning: Specific debugging story with a concrete fix.

[ ✅ Post ]  [ ✏️ Edit ]  [ ❌ Reject ]  [ 💤 Save ]
```

**Approve flow:**
- Tap ✅ → bot posts to X → updates `drafts.status='approved'`, writes `posts` row → confirmation message with X link.

**Edit flow:**
- Tap ✏️ → bot replies "Send me the edited version."
- Bot waits for next message in chat (state machine, 10-min timeout).
- On reply: posts edited text → records `decisions.decision='edited'` with `edited_text` and `edit_diff`.

**Reject flow:**
- Tap ❌ → bot shows reason buttons:
  ```
  Why? [ Boring ] [ Wrong tone ] [ Too internal ] [ Already covered ] [ Other ]
  ```
- Tap a reason or send free-text → records `decisions.decision='rejected'` with reason.

**Save flow:**
- Tap 💤 → status becomes `expired` after 24h if no further action; user can `/saved` to list saved drafts.

**Slash commands:**
- `/status` — bot health, last ingestion time, queue size, current month spend
- `/budget` — current spend vs cap
- `/pause [hours]` — pause drafting for N hours
- `/resume` — resume drafting
- `/saved` — list saved drafts
- `/digest` — force send weekly digest now
- `/repos` — list current allowlist (read-only)

### 7.5 Metrics fetcher (`src/wire/metrics/`)

Runs daily at `metrics.fetch_cron`.

For all posts younger than 30 days, fetch current metrics from the X API (`/2/tweets/:id` with `tweet.fields=public_metrics`). Append to `metrics` (one row per fetch — keep history, never overwrite).

Posts older than `posts_settle_days` (default 7) are eligible to be used as performance signal in the drafting prompt.

### 7.6 Weekly digest (`src/wire/digest/`)

Runs at `digest.cron` (default Monday 09:00 Stockholm).

Sends a single Telegram message:

```
📊 Last 7 days

Drafted: 8 · Posted: 5 · Rejected: 2 · Saved: 1
Approval rate: 71% (last week: 65%)

Top performers (≥7 days settled):
1. "Coolify silently failing on private repos…" (1.2k impr, 47 ❤️)
2. "Why I switched from Traefik…" (890 impr, 31 ❤️)

Below median:
1. "Just shipped a small fix" (180 impr, 4 ❤️)

LLM usage:
• Spend this month: $3.40 / $10.00 (34%)
• Provider: llama.cpp primary, Claude fallback rate: 12%
```

Informational only — no action required.

---

## 8. Drafting prompt

The prompt is the core of the system. Built dynamically per call.

```
SYSTEM:
You are Wire, a build-in-public assistant that drafts X/Twitter posts on
behalf of {user_name}, a developer. You have full access to their GitHub
activity and existing post history. Your job is to identify which work
sessions are worth posting about and draft tweets in their voice.

Hard rules:
- Never invent details not present in the events provided.
- Match the voice patterns shown in the examples — do not generic-ify.
- Drafts must be ≤ 280 characters per tweet. Threads are allowed (list of
  tweets) when the story warrants it, but most posts should be single tweets.
- If nothing in the session is genuinely post-worthy, return skip_reason
  and an empty drafts array. Most sessions should produce zero drafts.
- Do NOT post about: secrets, credentials, work content (Visma), unfinished
  experiments, or anything in a private repo unless its `notes` allow.

Voice profile (auto-generated, refreshed weekly):
{voice_profile_text}

USER:
─── Session events ───
Repo: {repo} ({visibility})
Repo notes: {repo_notes}
Session duration: {duration}
Events:
{event_list with triage_score and brief description}

─── Recent posts (last 30, settled ≥7d, with performance) ───
{post_list with text + impressions + likes + RTs}

Reference: your trailing 30-day median is {median} impressions, {median} likes.

─── Recent decisions (last 20) ───
{decision_list:
  ✅ APPROVED: "..." (posted as-is)
  ✏️ EDITED: bot draft → posted version
    DIFF: "..." → "..."
  ❌ REJECTED: "..." — reason: "boring"
  ❌ REJECTED: "..." — reason: "too internal"
}

─── Task ───
Draft 0–3 posts about this session, or return skip_reason if nothing here
warrants a post. Each draft must reference only events from the session above.
Return JSON matching the DraftResponse schema.
```

**Prompt caching strategy:**
- System prompt + voice profile → static, cache for 1 hour
- Recent posts + decisions → semi-static, cache for 5 minutes
- Session events → variable, never cached

Realistically 70%+ of input tokens cacheable on warm runs.

---

## 9. External setup (one-time)

Documented in `SETUP.md` shipped with the repo. The bot fails fast at startup with clear error messages if any step is incomplete.

### GitHub App
1. Create a GitHub App in your account/org settings.
2. Permissions: `Contents: Read`, `Metadata: Read`, `Pull requests: Read`, `Issues: Read`.
3. Subscribe to events: Push, Pull request, Release, Issues.
4. Install on your org, select all repos.
5. Download private key → mount at `/data/secrets/github-app.pem`.
6. Note App ID and Installation ID for `config.yaml`.

### Telegram bot
1. Talk to `@BotFather` → create bot → save token.
2. Send a message to your bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your chat ID.
3. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` env vars.

### X / Twitter
1. Apply for X API access (free tier supports the post and metrics volume needed here, but verify current limits).
2. Create an OAuth 2.0 app with `tweet.read`, `tweet.write`, and `users.read` scopes.
3. Run `python -m wire.scripts.twitter_auth` once locally → opens browser → completes OAuth flow → writes `/data/secrets/twitter-token.json` (refresh token persists).
4. Set `TWITTER_CLIENT_ID` and `TWITTER_CLIENT_SECRET` env vars.

### Anthropic
1. Get API key → set `ANTHROPIC_API_KEY` env var.

### llama.cpp / OpenAI-compatible endpoint (optional)
1. Run an OpenAI-compatible server (llama.cpp server, vLLM, Ollama's OpenAI-compat endpoint, OpenRouter, ...).
2. Set `WIRE_LLM_PROVIDER=llamacpp`, `WIRE_LLAMACPP_BASE_URL`, `WIRE_LLAMACPP_MODEL`, and `LLM_API_KEY` (if auth required) as env vars.

---

## 10. Project layout

```
wire/
├── Dockerfile
├── docker-compose.yml          # local dev
├── pyproject.toml              # Python 3.12, deps managed with uv
├── README.md
├── SETUP.md
├── src/
│   └── wire/
│       ├── __init__.py
│       ├── main.py             # entry point, scheduler, signal handling
│       ├── config.py           # YAML loader + pydantic validation
│       ├── db/
│       │   ├── models.py       # SQLAlchemy models
│       │   ├── migrations/     # Alembic
│       │   └── session.py
│       ├── llm/
│       │   ├── provider.py     # LLMProvider, ClaudeProvider, LlamaCppProvider, FallbackProvider
│       │   ├── caching.py      # prompt cache helpers
│       │   ├── budget.py       # monthly budget tracking + alerts
│       │   └── prompts/
│       │       ├── drafting.txt
│       │       ├── triage.txt
│       │       ├── voice_profile.txt
│       │       └── digest.txt
│       ├── ingestion/
│       │   ├── github_client.py
│       │   ├── filters.py
│       │   └── poller.py
│       ├── sessions/
│       │   └── detector.py
│       ├── drafting/
│       │   └── drafter.py
│       ├── telegram/
│       │   ├── bot.py
│       │   ├── handlers.py
│       │   └── commands.py
│       ├── twitter/
│       │   ├── client.py
│       │   └── oauth.py
│       ├── metrics/
│       │   └── fetcher.py
│       ├── digest/
│       │   └── builder.py
│       ├── voice/
│       │   └── profile_generator.py
│       └── scripts/
│           ├── twitter_auth.py
│           ├── seed_voice.py   # bootstrap voice profile from existing tweets
│           └── dry_run.py      # ingest + draft without sending/posting
├── data/                       # mounted volume in production
│   ├── wire.db
│   ├── config.yaml
│   ├── repos.yaml
│   └── secrets/
│       ├── github-app.pem
│       └── twitter-token.json
└── tests/
    ├── test_filters.py
    ├── test_sessions.py
    ├── test_provider_fallback.py
    ├── test_prompt_building.py
    └── ...
```

**Key dependencies:**
- `python-telegram-bot` ≥ 21
- `anthropic` (official SDK)
- `httpx` (llama.cpp + X API)
- `PyGithub` or `gidgethub` (GitHub App auth)
- `apscheduler` (cron-like scheduling)
- `sqlalchemy` + `alembic`
- `pydantic` v2 (config + LLM response schemas)
- `tenacity` (retries on flaky network calls)
- `structlog` (JSON logging for Coolify)

---

## 11. Dockerfile + deployment

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev

COPY src/ ./src/

ENV PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080

CMD ["uv", "run", "python", "-m", "wire.main"]
```

**Coolify deployment:**
1. Point Coolify at the private GitHub repo (use the existing GitHub App pattern from medianalyzer).
2. Mount a persistent volume at `/data`.
3. Set env vars: `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TWITTER_CLIENT_ID`, `TWITTER_CLIENT_SECRET`.
4. Place `config.yaml`, `repos.yaml`, and the secret files in `/data` via Coolify's file editor or one-time SSH copy.
5. Healthcheck: HTTP `GET /health` on port 8080 (the app exposes a tiny health endpoint with last-ingestion timestamp).
6. Disable auto-deploy initially per your preference; trigger deploys manually.

---

## 12. Cost controls (built in)

1. **Per-task model routing** — Haiku for triage/voice/digest, Sonnet for drafting, Opus never default. Applies when `WIRE_LLM_PROVIDER=claude` AND in the fallback path when `WIRE_LLM_PROVIDER=llamacpp`.
2. **Prompt caching** — system prompt + voice profile + posts + decisions cached at request level. Expected ~70% input cache hit rate after warm-up.
3. **Pre-LLM event filtering** — bots, version bumps, conventional-commit chore/ci/docs filtered before any LLM call.
4. **Triage threshold** — sessions with all events scoring < 0.3 skip drafting entirely (no expensive LLM call).
5. **Output token cap** — `max_tokens=1500` for drafting, lower for other tasks.
6. **Monthly budget cap** — tracked in `llm_calls.cost_usd`. At 80%: Telegram warning. At 100%: drafting paused (triage continues at minimal cost so the queue doesn't lose data); user can `/extend` to authorize overrun for the rest of the month.
7. **llama.cpp as cost shifter** — when `WIRE_LLM_PROVIDER=llamacpp`, the bulk of token volume runs locally for free; Claude only used on local-backend failures.

**Estimated monthly cost** (on Claude with caching, ~4 sessions/day):
- Sonnet drafting: ~$5/month
- Haiku triage/voice/digest: ~$1/month
- **Total: ~$6/month** with Claude as primary
- Lower if the local backend handles most volume successfully

---

## 13. Self-improvement mechanics (recap)

Two signals feed the drafting prompt — **never** as optimization targets, only as descriptive context:

1. **Decisions** — last 20 approve/reject/edit decisions injected as few-shot examples. Edits are particularly valuable: the diff shows how the user rewrites the bot.
2. **Performance** — last 30 posted tweets with engagement metrics (settled ≥7 days), normalized as relative-to-median.

Voice profile (a short auto-generated description of style patterns) is regenerated weekly from posted content — it tracks the user, can't drift away from them.

The user's approval gate is the safety mechanism. The bot cannot post anything without explicit Telegram approval. Drift is impossible because every output crosses the user's eyes.

---

## 14. Testing strategy

- **Unit tests** for: filters, session clustering, prompt building, provider fallback logic, budget tracking, edit-diff generation.
- **Integration tests** with a fake GitHub event stream + mocked LLM provider, end-to-end through to "Telegram message would be sent."
- **Provider fallback test**: stub the local llama.cpp endpoint to return invalid JSON / time out / 500 → verify Claude is called with same args.
- **Manual smoke test on first deploy**: run `python -m wire.scripts.dry_run` which ingests last 24h of events into a temp DB, drafts, and prints what *would* be sent — no Telegram, no posting.

---

## 15. Open items / explicit non-decisions

These are intentionally deferred to v2:

- Telegram commands for editing the repo allowlist (`/include`, `/exclude`).
- Image/media in posts (commit graphs, terminal output screenshots).
- Per-task LLM provider routing when `WIRE_LLM_PROVIDER=llamacpp` (e.g. "use the local backend for triage but always Claude for drafting"). v1 is all-or-nothing on the provider toggle.
- Threading multiple sessions into a longer-form post.
- A web dashboard.

---

## 16. First-run checklist (for the user)

1. Provision GitHub App, Telegram bot, X API app per `SETUP.md`.
2. Clone repo, fill in `config.yaml` and `repos.yaml`.
3. Run `python -m wire.scripts.twitter_auth` locally to get OAuth token.
4. Run `python -m wire.scripts.seed_voice` to bootstrap voice profile from your last 100 tweets (optional but recommended).
5. Build and push Docker image; deploy via Coolify.
6. Run `python -m wire.scripts.dry_run` against production DB to verify ingestion works.
7. Send `/status` to the Telegram bot — should respond.
8. Wait for first real session to close. Approve or reject the first draft. The system improves from there.

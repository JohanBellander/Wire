# Wire — first-time setup

This is the one-time external setup. Once it's done, the bot runs unattended in a single Docker container; day-to-day interaction is via Telegram.

The full design is in [`SPEC.MD`](./SPEC.MD).

---

## 0. Prerequisites

- A GitHub account/org you own
- A Telegram account
- An X (Twitter) developer account with API access
- An Anthropic API key
- A server (any Linux box, Coolify-managed or otherwise) running Docker

---

## 1. GitHub App

Wire authenticates as a GitHub App, not a personal access token. The App needs read access to your org's repos.

1. Go to your account or org Settings → **Developer settings → GitHub Apps → New GitHub App**.
2. Fill in:
   - **Name**: `wire-yourname` (must be unique on github.com)
   - **Homepage URL**: anything (`http://localhost`)
   - **Webhook**: uncheck **Active** — Wire polls, doesn't receive webhooks.
3. **Permissions** (Repository permissions):
   - Contents: **Read-only**
   - Metadata: **Read-only**
   - Pull requests: **Read-only**
   - Issues: **Read-only**
4. **Subscribe to events**: leave them all unchecked (no webhook).
5. **Where can this GitHub App be installed?** → "Only on this account".
6. Create the App, then on the App's page:
   - Note the **App ID** (numeric, top of page) → goes into `config.yaml` as `github.app_id`.
   - Click **Generate a private key** → downloads a `.pem` file. Save this; you'll mount it into the container.
7. Click **Install App** in the left sidebar → install on your account/org → choose **All repositories** (or pick the ones you'll allowlist).
8. After install, the URL bar will show `…/installations/<NUMBER>` — that **Installation ID** goes into `config.yaml` as `github.installation_id`.

---

## 2. Telegram bot

1. Open Telegram, message `@BotFather`.
2. Send `/newbot`, follow the prompts. Save the bot token it gives you.
3. Open a chat with your new bot and send any message (`hi`).
4. In a browser, visit:
   ```
   https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
   ```
   Find `"chat":{"id": <YOUR_CHAT_ID>}` in the response. That's the chat where Wire will send drafts.
5. You'll set:
   - `TELEGRAM_BOT_TOKEN=<token>` (env var)
   - `TELEGRAM_CHAT_ID=<chat id>` (env var)

The bot only responds in this chat — it ignores messages from other chats. That's deliberate.

---

## 3. X / Twitter

1. Apply at <https://developer.x.com>. The free tier covers Wire's volume (1 post/day, modest metrics polling), but verify current limits.
2. Create a **Project** → **App** → **OAuth 2.0** with:
   - Type: **Public client**
   - Callback URL: `http://127.0.0.1:8765/callback`
   - Website: anything
   - Scopes: `tweet.read`, `tweet.write`, `users.read`, `offline.access`
3. Copy the **Client ID** and **Client Secret**.
4. Run the OAuth bootstrap **on your laptop, not in the container**. It opens a browser:
   ```bash
   export TWITTER_CLIENT_ID=<your client id>
   export TWITTER_CLIENT_SECRET=<your client secret>
   export WIRE_CONFIG_PATH=./data/config.yaml
   uv run python -m wire.scripts.twitter_auth
   ```
   - Browser opens, authorize the app on twitter.com.
   - Token is written to whatever `twitter.access_token_path` points to in `config.yaml` (default `/data/secrets/twitter-token.json`).
5. Copy that token file to the server's `/data/secrets/twitter-token.json` mount.

The refresh token rotates on every refresh — Wire saves the new one back to disk automatically.

---

## 4. Anthropic API

1. <https://console.anthropic.com> → API Keys → create one.
2. Set `ANTHROPIC_API_KEY=<key>` as an env var on the server.

That's it. No further steps.

---

## 5. (Optional) Self-hosted Ollama

Only do this if you want to shift the bulk of LLM volume off Anthropic. Wire's `FallbackProvider` keeps Anthropic as the automatic safety net — when Ollama times out, returns invalid JSON, or refuses to produce structured output, the call gets retried against Claude. So flipping to Ollama is reversible at any time and doesn't risk drafting reliability.

### 5.1 — Run Ollama on a network-reachable host

```bash
# On the host (LAN box, VPN endpoint, or the same docker network as Wire):
OLLAMA_HOST=0.0.0.0 ollama serve

# Pull a known-good model. Helmsman + Wire have both production-tested this:
ollama pull qwen3.5:9b
```

### 5.2 — Configure Wire

In `/data/config.yaml`:

```yaml
llm:
  provider: ollama
  ollama:
    base_url: http://<your ollama host>:11434
    model: qwen3.5:9b
    timeout_seconds: 90
    temperature: 0.5     # tuned for qwen; raise for more variation, lower for tighter schemas
    think: true          # extended thinking — required for qwen reliability
    # extra_options:    # optional: pass any other Ollama option without code changes
    #   top_p: 0.95
    #   seed: 42
```

The `temperature` and `think` defaults match Helmsman's empirical tuning. Without them, qwen3.5:9b refuses to produce structured output ~40% of the time at default settings. With them, refusal rate drops to near zero.

Anthropic stays configured even when `provider: ollama` — it's the automatic fallback. Don't remove `ANTHROPIC_API_KEY` from your env vars.

### 5.3 — Verify

After redeploy:

1. **Boot logs**: look for `wire.ollama.reachable` (success) or `wire.ollama.unreachable_warning` (Ollama host down or wrong URL — Wire still starts, but every call falls back to Claude).
2. **`/status` in Telegram**: the new 🧠 brain block shows primary, fallback, last-used backend, and 24h fallback rate. After the first poll cycle:
   ```
   🧠 brain
   primary:  ollama (qwen3.5:9b)
   fallback: claude (claude-sonnet-4-6 / claude-haiku-4-5)
   last used: ollama
   fallback rate (24h): 0% (0 / 12)
   ```
   A high fallback rate (>20%) means Ollama is choking on something — see "Tuning" below.

### 5.4 — Tuning

If you switch to Ollama and the brain block shows >20% fallback rate after a few polls:

| symptom | likely cause | fix |
|---|---|---|
| Fallback rate ~100% on every call | Ollama not reachable, or wrong base_url | Check `wire.ollama.unreachable_warning` boot log |
| Fallback rate 30-60%, intermittent | Schema refusals from qwen | Lower `temperature` to 0.3 |
| Fallback rate slowly climbing over hours | Memory pressure on Ollama host | `ollama stop` + pull a smaller quant (e.g. `qwen3.5:9b-q4_K_M`) |
| All calls hit timeout | `timeout_seconds` too low for slow host | Raise to 120s or 180s |

If nothing helps, set `provider: claude` while debugging. Cost goes up, but drafts arrive.

---

## 6. Wire config files

On the server, the `/data` volume needs three things plus secrets:

```
/data/config.yaml                     # main config
/data/repos.yaml                      # the allowlist
/data/secrets/github-app.pem          # from step 1.6
/data/secrets/twitter-token.json      # from step 3.4 (or generated by twitter_auth)
```

1. Copy the templates:
   ```bash
   cp data/config.yaml.example /data/config.yaml
   cp data/repos.yaml.example /data/repos.yaml
   ```
2. Edit `/data/config.yaml`. Fields you must change:
   - `github.org`
   - `github.app_id`, `github.installation_id`
   - `quiet_hours.timezone` (if not Europe/Stockholm)
   - `llm.provider` (`claude` or `ollama`)
   - `llm.monthly_budget_usd` (cap; warn at 80%, pause at 100%)
3. Edit `/data/repos.yaml` to list only repos you want posts about. **Anything not in this list is ignored entirely** — Wire never sees events from un-allowlisted repos. This is the safety mechanism that keeps work repos out of public posts.

---

## 7. Run

### Coolify

1. Point Coolify at this repo.
2. Persistent volume → mount at `/data`.
3. Env vars:
   ```
   ANTHROPIC_API_KEY
   TELEGRAM_BOT_TOKEN
   TELEGRAM_CHAT_ID
   TWITTER_CLIENT_ID
   TWITTER_CLIENT_SECRET
   ```
4. Healthcheck: HTTP `GET /health` on port 8080.
5. Disable auto-deploy initially; trigger manually until you trust the setup.

### Plain Docker

```bash
docker run -d \
  --name wire \
  --restart unless-stopped \
  -p 8080:8080 \
  -v /opt/wire/data:/data \
  -e ANTHROPIC_API_KEY=... \
  -e TELEGRAM_BOT_TOKEN=... \
  -e TELEGRAM_CHAT_ID=... \
  -e TWITTER_CLIENT_ID=... \
  -e TWITTER_CLIENT_SECRET=... \
  wire:latest
```

### Local docker-compose

```bash
cp .env.example .env       # fill in the keys
cp data/config.yaml.example data/config.yaml
cp data/repos.yaml.example data/repos.yaml
# edit config.yaml + repos.yaml
docker compose up --build
```

---

## 8. Initialize the database

The first time you start the container, run the migration:

```bash
docker exec -it wire python -m wire.scripts.db_init
```

(Or just let the container come up; subsequent restarts are idempotent. The migration is fast on an empty DB.)

---

## 9. (Optional) Bootstrap the voice profile

Without this, Wire's first drafts will be generic. With it, drafts already match your voice on day one.

```bash
docker exec -it wire python -m wire.scripts.seed_voice
```

This reads up to ~100 of your recent X tweets via the OAuth token, generates a voice profile, and writes it to the DB. Subsequent weekly regenerations use only bot-posted tweets (so the profile tracks Wire's actual output, not your old style).

---

## 10. First-run smoke test

```bash
# verify ingestion + filters work against your real GitHub:
docker exec -it wire python -m wire.scripts.dry_run
```

This ingests the last 24h of events into a temporary DB, runs the filter chain, and prints what was kept vs filtered. **No Telegram messages are sent, no posts are made.** A single sanity-check.

In Telegram, send `/status` to your bot — it should respond with health and budget info.

---

## 11. Day-to-day

- Wait for a real session to close. You'll get a draft in Telegram.
- Tap ✅ Post · ✏️ Edit · ❌ Reject · 💤 Save.
- Every Monday at 09:00 (Stockholm) you get a digest.
- `/budget` to check spend; `/extend [usd]` to raise the cap.
- `/pause [hours]` if you want quiet for a while; `/resume` to lift.

---

## Troubleshooting

| symptom | check |
|--|--|
| Bot doesn't respond to `/status` | `TELEGRAM_BOT_TOKEN` set? Right `TELEGRAM_CHAT_ID`? Bot in that chat? |
| `GitHub auth failed` | `app_id`/`installation_id` correct? `.pem` mounted? Permissions match section 1.3? |
| `No twitter token` | run `python -m wire.scripts.twitter_auth` locally, copy file to `/data/secrets/` |
| `Invalid …/config.yaml` | run `docker exec -it wire python -c "from wire.config import load_config; load_config('/data/config.yaml')"` for the validation error in full |
| Drafts never arrive | `/status` to see queue + last ingestion. Then `dry_run` to confirm events are being ingested. Then check that triage scores aren't all <0.3 (sessions with all-low scores skip drafting). |
| Surprise spend | `/budget`. Drafting auto-pauses at 100%. Use `/extend` if you want to keep going. |

The bot fails fast on misconfiguration — startup logs will tell you exactly which field or file is wrong.

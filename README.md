# Wire

Self-hosted build-in-public bot. Watches a configured GitHub organization, drafts X/Twitter posts about post-worthy activity, sends them to Telegram for approval, and posts approved content to X. Learns from approve / reject / edit decisions and post performance via prompt context — no fine-tuning, no reward functions, no auto-posting.

The full design is in [`SPEC.MD`](./SPEC.MD).

## Status

Step 1 of 13 — project scaffold. The container starts, loads config, exposes `GET /health` on port 8080, and exits cleanly on `SIGTERM`. No business logic yet.

Subsequent steps add config validation, the database layer, the LLM provider abstraction, ingestion, session detection, drafting, the Telegram bot, X posting, metrics, the weekly digest, and budget controls. See `SPEC.MD` for the full plan.

## Local quick start

```bash
cp .env.example .env                                 # fill in real keys
cp data/config.yaml.example data/config.yaml        # placeholder values fine for now
cp data/repos.yaml.example data/repos.yaml

docker compose up --build
curl http://localhost:8080/health
```

To shut down: `docker compose down`. The container exits cleanly on `SIGTERM`.

## Development without Docker

```bash
pip install --user uv             # one time
uv sync                           # creates .venv, installs runtime + dev deps
uv run pytest -q                  # smoke tests
WIRE_DEV=1 uv run python -m wire.main   # starts /health on 8080; Ctrl+C to stop
```

## Layout

```
src/wire/         # the package
├── main.py       # entrypoint: scheduler + signal handling
├── config.py     # YAML + pydantic validation (step 2)
├── health.py     # aiohttp /health endpoint
├── db/           # SQLAlchemy + Alembic (step 3)
├── llm/          # provider abstraction + prompts (step 4, 7)
├── ingestion/    # GitHub App auth, polling, filters (step 5)
├── sessions/     # session detection (step 6)
├── drafting/     # prompt builder + LLM call (step 7)
├── telegram/     # bot + handlers + slash commands (step 8)
├── twitter/      # OAuth + posting client (step 9)
├── metrics/      # X engagement fetch (step 10)
├── digest/       # weekly digest builder (step 10)
├── voice/        # voice profile generator (step 10)
└── scripts/      # one-shot CLI tools (twitter_auth, seed_voice, dry_run)

data/             # mounted volume in production (gitignored except *.example)
tests/            # pytest
```

## Setup

`SETUP.md` (one-time external setup for GitHub App, Telegram bot, X API, Anthropic, optional llama.cpp) lands in step 13. For now, refer to SPEC.MD §9.

## License

Apache-2.0 — see [`LICENSE`](./LICENSE).

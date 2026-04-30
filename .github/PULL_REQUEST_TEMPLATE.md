## What

<!-- One paragraph: what's changing and why -->

## Checklist

- [ ] Tests pass locally (`uv run pytest -q`)
- [ ] Ruff clean (`uv run ruff check src tests && uv run ruff format --check src tests`)
- [ ] `CLAUDE.md` updated if any of the conventions, file locations, or pitfalls changed
- [ ] No secrets committed (`.env`, `data/secrets/`, API keys, tokens — all should be gitignored)
- [ ] If a new prompt was added: dropped into `src/wire/llm/prompts/` and referenced from a module
- [ ] If a new LLM caller was added: it calls `log_llm_call(resp)` from `wire.llm.budget`
- [ ] If a new event type is being processed: it's handled in `_summarize_event` (triage), `_format_event_line` (drafter), and `_format_event_message` (inspect)
- [ ] If touching the GitHub `/events` payload path: enrichment via `_enrich_events` is preserved

## How tested

<!-- Manual reproduction steps, dry_run output, screenshots, etc. -->

## Production impact

<!-- Will this need a redeploy? Migration? Config change? Secret rotation? -->

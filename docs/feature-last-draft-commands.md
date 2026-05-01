# Feature: `/last` and `/draft` Telegram commands

## Request

> Can we add a slash command so that I can check which was the last PR/event the
> bot picked up (even if it decided it was not worth an X post). I would also
> like to be able to force an X post for that event.

## Motivation

Today there is no Telegram-side visibility into events that were ingested but
not drafted. When triage scores an event below the 0.3 threshold (or the
drafting LLM returns `skip_reason`), the event is silently dropped. The user
has to SSH to `gary` and run `python -m wire.scripts.inspect` to see what
happened.

Two concrete pain points:

1. **Diagnostic blindness.** A commit that "should" have been posted is
   triaged at 0.25 — there is no signal in Telegram that anything happened,
   so the user thinks Wire is broken.
2. **No manual override.** Even when the user knows a specific event is
   post-worthy, the only way to force a draft is to artificially edit the
   triage score in SQLite or to hand-craft a tweet. The Wire-flavored draft
   (with voice profile, README context, recent posts as reference) is gone.

## Two new commands

### `/last [n]`

Lists the last `n` ingested events (default 5, capped at 50), most recent
first, with their triage outcome and what happened next.

**Output format:**

```
🕓 last 5 events
[42] wire/PushEvent "feat: ship triage repo_notes" triage=0.62 → drafted #17 (pending)
[41] wire/PushEvent "chore: bump deps" triage=0.18 → below-threshold skip
[40] medianalyzer/PullRequestEvent "Add Withings OAuth" triage=0.71 → drafted #16 (approved)
[39] wire/PushEvent "fix: typo in prompt" triage=0.22 → below-threshold skip
[38] winetrackr/PushEvent "wip" triage=0.10 → below-threshold skip
```

**Outcome strings (priority order):**

| precedence | outcome string                | meaning                                                                |
| ---------- | ----------------------------- | ---------------------------------------------------------------------- |
| 1          | `drafted #N (status)`         | event's session was drafted, draft #N exists, with current status      |
| 2          | `LLM said skip: <reason>`     | session drafted but LLM returned `skip_reason`                         |
| 3          | `below-threshold skip`        | event's session closed but all events scored < 0.3                     |
| 4          | `pending session close`      | session is open and accumulating; drafting hasn't run yet              |
| 5          | `no session`                  | event hasn't been clustered yet (rare; only inside a single poll cycle)|

The bracketed `[42]` is the `events.id` — the same ID used by `/draft`.

### `/draft <event_id>`

Force-draft a specific event, regardless of triage score or quiet hours.

**Behavior:**

- Loads the event by id; errors if not found.
- Builds a synthetic single-event "session wrapper" — same prompt blocks
  (voice profile, README, recent posts, recent decisions) as a normal session,
  but with one event in the events list.
- Appends a marker to the user-message: `Note: user has explicitly requested a
  draft for this event via /draft. Do not return skip_reason.`
- Calls the drafting LLM with the standard `DraftResponse` schema.
- Persists the draft(s) and sends the first one through the normal Telegram
  approval keyboard (✅ / ✏️ / ❌ / 💤).
- Reasoning field gets the prefix `[forced via /draft]`.

**What it does *not* override:**

- The Telegram approval gate. Force-drafting writes a draft, it does not
  post to X.
- The monthly budget pause. If `/budget` shows the cap has been hit,
  `/draft` returns an error.

## Design decisions (user-confirmed)

| question                                | choice                                |
| --------------------------------------- | ------------------------------------- |
| Which event types are listed by `/last`? | All event types (not just PRs)       |
| Identifier for `/draft`                 | `events.id` (not `drafts.id`)         |
| Re-running `/draft` on the same event   | Always fire; no dedup against prior drafts |

## Implementation outline

### `src/wire/drafting/drafter.py`

Add `force_draft_for_event`:

```python
async def force_draft_for_event(
    event_id: int,
    config: WireConfig,
    repos_file: ReposFile,
    provider: LLMProvider,
) -> tuple[int | None, str | None]:
    """Force-draft a single event regardless of triage score / quiet hours.

    Returns (draft_id, skip_reason). draft_id is None if the LLM returned
    skip_reason; skip_reason is None on success.
    """
```

- Load event with its session (or build a synthetic one if it has no session yet).
- Wrap as a `Session`-shaped object for `build_prompt_blocks`.
- Append the override note to `blocks.user_message`.
- Call `provider.complete(...)` with the same parameters as
  `draft_pending_sessions`.
- `_log_llm_call(resp)` — non-negotiable (CLAUDE.md convention).
- Persist `Draft` linked to the original session id (or None).
- Prefix reasoning with `[forced via /draft]`.

### `src/wire/telegram/commands.py`

Add `last_cmd` and `draft_cmd`:

```python
async def last_cmd(update, context) -> None:
    # parse [n], default 5, cap 50
    # query events ORDER BY occurred_at DESC LIMIT n
    # for each: figure outcome string from session/draft state
    # render and reply

async def draft_cmd(update, context) -> None:
    # parse <event_id>; reject if missing/non-int
    # call force_draft_for_event
    # if draft_id: send_draft(app, draft_id)
    # else: reply with skip_reason
```

### `src/wire/telegram/bot.py`

Register the two new `CommandHandler`s in `build_application`.

### Help text

Update `help_cmd` to include:

```
/last [n]            last N events with triage + outcome
/draft <event_id>    force a draft for a specific event
```

## Tests

- `tests/test_drafting.py` — `force_draft_for_event` happy path, skip_reason
  path, missing-event path.
- `tests/test_telegram_handlers.py` — `/last` rendering with mixed event
  outcomes; `/draft` invokes `force_draft_for_event` and `send_draft`.

## Out of scope

- Post-from-`/draft`-directly (skipping the approval keyboard). The whole
  point of Wire is the human gate; `/draft` overrides judgment, not consent.
- Pagination for `/last`. 50 events is enough; older diagnostics live in
  `python -m wire.scripts.inspect`.
- A `/why <event_id>` command that re-runs triage and returns the
  reasoning. Could be a follow-up if the triage-reason field turns out to
  be too terse.

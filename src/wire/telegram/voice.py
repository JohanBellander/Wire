"""Static phrase pools for Wire's Telegram persona.

Every user-facing Telegram string in handlers, commands, alerts, and the
digest goes through `say(state, **kwargs)`. The pools live here so the voice
can be tuned without hunting through five files of f-strings.

Voice rules: see `src/wire/llm/prompts/persona.txt`. tldr: lowercase by
default, lightly cyberpunk, addresses "Johan" by first name when warmth
lands, no glitched text, no leetspeak.

Numeric data tables (the body of /status, /budget, /saved, /repos, /last,
the digest stats block) are intentionally NOT routed through this module —
they need to stay parseable. Only headers, footers, ack lines, and error
strings flow through `say()`.
"""

from __future__ import annotations

import random
from typing import Any

# Each key maps to a list of templates. `say(key, **kwargs)` picks one and
# substitutes `{placeholder}` fields from kwargs. Keep the templates short
# and consistent in placeholder names so callers can't get them wrong.
PHRASES: dict[str, list[str]] = {
    # ---- draft preview --------------------------------------------------
    "draft_header": [
        "📝 draft #{draft_id} · {repo}",
    ],
    # ---- approve / post -------------------------------------------------
    "post_success_with_url": [
        "✅ posted, johan. {url}",
        "✅ live on the wire. {url}",
        "✅ shipped. signal's clean. {url}",
    ],
    "post_success_no_url": [
        "✅ posted.",
        "✅ out on the wire.",
    ],
    "post_failed": [
        "the wire didn't take it: {error}",
        "post failed: {error}",
    ],
    "post_dry_run": [
        "twitter not wired. would've posted (no real send).",
    ],
    # ---- edit -----------------------------------------------------------
    "edit_prompt": [
        "describe the change — 'shorter', 'less hype', 'drop the emoji'. 10min window.",
        "what should change? 'shorter', 'no emoji', 'less hype' — i'll rewrite. 10min.",
        "tell me what to fix. plain english. 10min on the clock.",
    ],
    "edit_revised": [
        "✏️ rewritten. take another look.",
        "✏️ here's the rewrite — your call.",
        "✏️ revision below. ✅ to ship, ✏️ to keep tweaking.",
    ],
    "edit_revision_failed": [
        "revision didn't land: {error}. try again or ❌ to kill it.",
        "couldn't rewrite: {error}. send another instruction or reject.",
    ],
    "edit_success_with_url": [
        "✏️ edited and live. {url}",
        "✏️ rewritten. signal's out. {url}",
    ],
    "edit_success_no_url": [
        "✏️ edited and posted.",
        "✏️ rewritten and shipped.",
    ],
    "edit_dry_run": [
        "edit recorded. twitter not wired — would've posted.",
    ],
    "edit_post_failed": [
        "rewrite didn't make it out: {error}",
        "posting the edit failed: {error}",
    ],
    # ---- reject ---------------------------------------------------------
    "reject_prompt": [
        "why kill draft #{draft_id}?",
        "reason for #{draft_id}?",
    ],
    "reject_other_prompt": [
        "send the reason as a reply.",
    ],
    "rejected": [
        "killed: {reason}.",
        "rejected: {reason}.",
        "scrapped — {reason}.",
    ],
    "rejected_custom": [
        "killed with custom reason.",
        "rejected, noted.",
    ],
    # ---- save -----------------------------------------------------------
    "saved": [
        "💤 parked. /saved when you want it.",
        "💤 in cold storage. /saved to dig it up.",
    ],
    # ---- pause / resume -------------------------------------------------
    "paused_until": [
        "⏸ off the wire until {until} UTC.",
        "⏸ drafting paused until {until} UTC. /resume to lift.",
    ],
    "paused_indefinite": [
        "⏸ going dark. /resume to bring me back up.",
        "⏸ drafting paused. /resume when you want me back.",
    ],
    "resumed": [
        "▶️ back on the wire. drafting live.",
        "▶️ live again. /status to confirm.",
    ],
    # ---- /status / brain headers ---------------------------------------
    "status_header": [
        "🤖 wire status",
    ],
    "brain_header": [
        "🧠 brain",
    ],
    # ---- /budget --------------------------------------------------------
    "budget_header": [
        "💰 budget {month}",
    ],
    "budget_extended": [
        "💵 cap raised by ${amount:.2f}, johan. new cap ${cap:.2f}; "
        "spent ${spend:.2f} ({pct:.1f}%).",
    ],
    # ---- /saved / /repos / /last empty + headers ------------------------
    "saved_header": [
        "💤 saved drafts:",
    ],
    "saved_empty": [
        "cold storage is empty.",
        "no saved drafts. inbox is clean.",
    ],
    "repos_header": [
        "📦 allowlisted repos:",
    ],
    "repos_empty": [
        "no repos in the allowlist.",
    ],
    "last_header": [
        "🕓 last {count} events",
    ],
    "last_empty": [
        "🕓 no events ingested yet",
    ],
    # ---- error states ---------------------------------------------------
    "draft_not_found": [
        "ghost id. nothing here.",
        "no draft by that id.",
    ],
    "digest_not_wired": [
        "digest builder isn't wired yet.",
    ],
    "provider_not_wired": [
        "❌ llm provider not wired into the bot — restart needed.",
    ],
    "event_not_found": [
        "❌ event #{event_id} not found",
    ],
    "budget_blocked": [
        "❌ monthly cap hit ({detail}); /extend first.",
    ],
    "force_skip_reason": [
        "⚠️ llm said skip: {reason}",
    ],
    "force_send_failed": [
        "⚠️ draft #{draft_id} created but send failed; check /saved",
    ],
    "force_failed": [
        "❌ force-draft failed: {error_type}: {error}",
    ],
    "force_success": [
        "✅ forced draft #{draft_id} for event #{event_id}",
    ],
    # ---- /pause / /extend usage hints -----------------------------------
    "pause_usage": [
        "usage: /pause [hours]",
    ],
    "extend_usage": [
        "usage: /extend [usd]  (default +$5)",
    ],
    "extend_non_positive": [
        "amount must be positive.",
    ],
    "last_usage": [
        "usage: /last [n]  (default 5, max 50)",
    ],
    "draft_usage": [
        "usage: /draft <event_id>",
    ],
    "draft_usage_int": [
        "usage: /draft <event_id>  (event_id must be an integer)",
    ],
    # ---- budget alerts (alerts.py) --------------------------------------
    "budget_warn": [
        "⚠️ budget at {pct:.0f}% (${spend:.2f} / ${cap:.2f}). going quiet at 100%. "
        "/extend for more runway.",
    ],
    "budget_capped": [
        "🛑 budget hit. ${spend:.2f} / ${cap:.2f}. parked till next month or /extend "
        "to raise the cap.",
    ],
    # ---- digest static fallbacks (when LLM frame fails) ----------------
    "digest_opener_fallback": [
        "📊 last 7 days, johan. here's the read:",
    ],
    "digest_closer_fallback": [
        "that's the week on the wire.",
    ],
    # ---- intent classifier fallback ------------------------------------
    "intent_unknown": [
        "didn't catch that, johan. /help for the menu.",
        "signal's noisy — try /help.",
        "not sure what you meant. /help shows what i listen for.",
    ],
    # ---- /help cheat-sheet (single template, no variation) -------------
    "help_text": [
        "wire — build-in-public bot\n"
        "talk to me in plain english — i'll figure it out.\n\n"
        "things you can say:\n"
        "  status                       bot health, queue, pause state\n"
        "  budget / how's the budget    spend vs cap\n"
        "  resume / wake up             resume drafting after a pause\n"
        "  saved / show saved drafts    list parked drafts\n"
        "  digest / send the digest     force-send weekly digest\n"
        "  repos / list repos           list allowlisted repos\n"
        "  extend [by N usd]            raise monthly cap by N (default 5)\n"
        "  last [n]                     last N events with triage + outcome\n"
        "  draft <event_id>             force a draft for a specific event\n\n"
        "always-available slash commands:\n"
        "  /pause [hours]               pause drafting (kill switch)\n"
        "  /help                        this menu\n\n"
        "draft messages have buttons: ✅ post · ✏️ edit · ❌ reject · 💤 save\n"
        "after ✏️ edit, describe the change — 'shorter', 'less hype', "
        "'drop the emoji'. i'll rewrite it."
    ],
}


class _MissingKey(KeyError):
    pass


def say(state: str, *, rng: random.Random | None = None, **kwargs: Any) -> str:
    """Pick a template for `state`, substitute `{placeholders}` from kwargs.

    Raises KeyError on unknown state (loud failure — beats silently shipping
    an empty string). Raises ValueError on a placeholder mismatch so the
    test suite catches mismatched call sites.
    """
    pool = PHRASES.get(state)
    if not pool:
        raise _MissingKey(f"unknown phrase state: {state!r}")
    pick = (rng or random).choice(pool)
    try:
        return pick.format(**kwargs)
    except KeyError as e:
        raise ValueError(
            f"phrase {state!r} expected placeholder {e.args[0]!r}, got kwargs={sorted(kwargs)}"
        ) from e


def seeded(state: str, *, seed: Any, **kwargs: Any) -> str:
    """Deterministic variant — same seed always picks the same template.
    Used where a draft / digest might be re-rendered and we want the line
    to be stable across retries."""
    rng = random.Random(seed)
    return say(state, rng=rng, **kwargs)

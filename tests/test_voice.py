"""Phrase-pool sanity tests.

These tests don't validate the voice (that's a human call) — they validate
that no caller can land on an empty pool, that placeholders match every
template in a pool, and that `say()` raises loudly on misuse.
"""

from __future__ import annotations

import re
import string

import pytest

from wire.telegram.voice import PHRASES, say, seeded


def _placeholders(template: str) -> set[str]:
    """Extract `{name}` placeholders, including the `{name:.0f}` spec form.
    Returns just the names — not the format spec."""
    fmt = string.Formatter()
    names: set[str] = set()
    for _, field_name, _, _ in fmt.parse(template):
        if field_name is None or field_name == "":
            continue
        # Strip any attribute / index access; keep just the leading identifier.
        head = re.split(r"[.\[]", field_name, maxsplit=1)[0]
        names.add(head)
    return names


def test_every_pool_has_at_least_one_template():
    for state, pool in PHRASES.items():
        assert pool, f"phrase pool {state!r} is empty"
        for tmpl in pool:
            assert isinstance(tmpl, str) and tmpl.strip(), (
                f"phrase pool {state!r} contains an empty template"
            )


def test_all_templates_in_a_pool_share_placeholders():
    """Every template in a pool must declare the same set of placeholders.
    If they diverge, `say()` will surface a confusing KeyError on the
    branch the caller didn't supply."""
    for state, pool in PHRASES.items():
        first = _placeholders(pool[0])
        for tmpl in pool[1:]:
            assert _placeholders(tmpl) == first, (
                f"pool {state!r}: templates declare different placeholders. "
                f"first: {sorted(first)} vs {tmpl!r}: "
                f"{sorted(_placeholders(tmpl))}"
            )


def test_say_returns_one_of_the_pool_templates_unformatted():
    # No-placeholder pool — all picks should round-trip to one of the
    # raw strings.
    out = say("draft_not_found")
    assert out in PHRASES["draft_not_found"]


def test_say_substitutes_placeholders():
    out = say("post_success_with_url", url="https://x.com/a/b/c")
    assert "https://x.com/a/b/c" in out


def test_say_raises_on_unknown_state():
    with pytest.raises(KeyError):
        say("nonexistent_state_name")


def test_say_raises_on_missing_placeholder():
    # post_success_with_url requires {url}; omitting it must blow up.
    with pytest.raises(ValueError, match="url"):
        say("post_success_with_url")


def test_seeded_is_deterministic():
    a = seeded("post_success_with_url", seed="draft-42", url="u")
    b = seeded("post_success_with_url", seed="draft-42", url="u")
    assert a == b


def test_seeded_picks_can_differ_across_seeds_eventually():
    """Sanity check that the seed actually selects different templates from
    pools that have more than one. We try a handful of seeds — if none
    of them differ, the seeded picker isn't using the seed."""
    pool_state = next(s for s, p in PHRASES.items() if len(p) >= 2 and not _placeholders(p[0]))
    seen = {seeded(pool_state, seed=i) for i in range(20)}
    assert len(seen) >= 2, f"seeded({pool_state!r}) appears not to use the seed"


# ---- Placeholder coverage for known callers --------------------------------
#
# These pin down the kwargs each call site supplies, so that renaming a
# placeholder in voice.py without updating the caller is caught here.

CALLER_KWARGS: dict[str, dict[str, object]] = {
    "draft_header": {"draft_id": 1, "repo": "demo"},
    "post_success_with_url": {"url": "u"},
    "post_success_no_url": {},
    "post_failed": {"error": "boom"},
    "post_dry_run": {},
    "edit_prompt": {},
    "edit_revised": {},
    "edit_revision_failed": {"error": "boom"},
    "edit_success_with_url": {"url": "u"},
    "edit_success_no_url": {},
    "edit_dry_run": {},
    "edit_post_failed": {"error": "boom"},
    "reject_prompt": {"draft_id": 1},
    "reject_other_prompt": {},
    "rejected": {"reason": "boring"},
    "rejected_custom": {},
    "saved": {},
    "paused_until": {"until": "2026-05-02T12:00:00"},
    "paused_indefinite": {},
    "resumed": {},
    "status_header": {},
    "brain_header": {},
    "budget_header": {"month": "2026-05"},
    "budget_extended": {"amount": 5.0, "cap": 15.0, "spend": 7.5, "pct": 50.0},
    "saved_header": {},
    "saved_empty": {},
    "repos_header": {},
    "repos_empty": {},
    "last_header": {"count": 5},
    "last_empty": {},
    "draft_not_found": {},
    "digest_not_wired": {},
    "provider_not_wired": {},
    "event_not_found": {"event_id": 7},
    "budget_blocked": {"detail": "month spend $11.00 / cap $10.00"},
    "force_skip_reason": {"reason": "boring"},
    "force_send_failed": {"draft_id": 1},
    "force_failed": {"error_type": "RuntimeError", "error": "boom"},
    "force_success": {"draft_id": 1, "event_id": 7},
    "pause_usage": {},
    "extend_usage": {},
    "extend_non_positive": {},
    "last_usage": {},
    "draft_usage": {},
    "draft_usage_int": {},
    "budget_warn": {"pct": 85.0, "spend": 8.5, "cap": 10.0},
    "budget_capped": {"spend": 10.0, "cap": 10.0},
    "digest_opener_fallback": {},
    "digest_closer_fallback": {},
    "help_text": {},
}


def test_every_pool_has_known_caller_kwargs():
    """Each phrase key shipped in PHRASES must have an entry in CALLER_KWARGS,
    and those kwargs must be sufficient to render every template in the pool.
    Catches drift between voice.py and the call sites."""
    for state in PHRASES:
        assert state in CALLER_KWARGS, (
            f"voice.py defines pool {state!r} but no caller kwargs are pinned "
            "in tests/test_voice.py — add them so renames are detected."
        )
        kwargs = CALLER_KWARGS[state]
        # `say` picks one randomly, but every template in the pool shares the
        # same placeholders (validated above), so any pick that succeeds means
        # all picks succeed for this kwargs set.
        out = say(state, **kwargs)
        assert isinstance(out, str) and out

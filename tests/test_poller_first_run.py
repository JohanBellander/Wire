"""Test first-run tracking and the per-repo fetch watermark.

Two related concerns:
- A quiet repo (everything dropped by 24h cutoff) shouldn't be stuck on
  first_run=True forever.
- A repo that flipped to first_run=False without anything getting inserted
  shouldn't re-fetch its entire backlog on the next poll (the bug that
  flooded the user with 30 old aventyrligare PRs).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from wire.db import session as db_session
from wire.db.models import Base, BotState
from wire.ingestion.poller import (
    _FIRST_RUN_KEY_PREFIX,
    _LAST_FETCHED_KEY_PREFIX,
    _MISSING_WATERMARK_FLOOR_HOURS,
    _get_last_fetched_at,
    _is_first_run_for,
    _mark_first_run_done,
    _resolve_since,
    _set_last_fetched_at,
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "wire.db"
    monkeypatch.setenv("WIRE_DB_PATH", str(db_path))
    db_session.reset_for_tests()
    engine = db_session.init(db_path)
    Base.metadata.create_all(engine)
    yield db_session
    db_session.reset_for_tests()


def test_first_run_true_initially(db):
    assert _is_first_run_for("winetrackr") is True


def test_first_run_false_after_marking(db):
    _mark_first_run_done("winetrackr")
    assert _is_first_run_for("winetrackr") is False


def test_per_repo_isolation(db):
    _mark_first_run_done("winetrackr")
    assert _is_first_run_for("winetrackr") is False
    assert _is_first_run_for("medianalyzer") is True


def test_marking_idempotent(db):
    _mark_first_run_done("winetrackr")
    _mark_first_run_done("winetrackr")
    _mark_first_run_done("winetrackr")
    # Only one row in bot_state
    with db.session_scope() as s:
        from sqlalchemy import select

        rows = (
            s.execute(select(BotState).where(BotState.key == _FIRST_RUN_KEY_PREFIX + "winetrackr"))
            .scalars()
            .all()
        )
        assert len(rows) == 1


# ---------- watermark ----------


def test_watermark_returns_none_when_unset(db):
    assert _get_last_fetched_at("winetrackr") is None


def test_watermark_round_trips(db):
    ts = datetime(2026, 4, 30, 12, 0, 0)
    _set_last_fetched_at("winetrackr", ts)
    assert _get_last_fetched_at("winetrackr") == ts


def test_watermark_overwrites(db):
    earlier = datetime(2026, 4, 30, 10, 0, 0)
    later = datetime(2026, 4, 30, 14, 0, 0)
    _set_last_fetched_at("winetrackr", earlier)
    _set_last_fetched_at("winetrackr", later)
    assert _get_last_fetched_at("winetrackr") == later


def test_watermark_isolated_per_repo(db):
    a = datetime(2026, 4, 30, 10, 0, 0)
    b = datetime(2026, 4, 30, 14, 0, 0)
    _set_last_fetched_at("winetrackr", a)
    _set_last_fetched_at("aventyrligare", b)
    assert _get_last_fetched_at("winetrackr") == a
    assert _get_last_fetched_at("aventyrligare") == b


# ---------- flood guard via _resolve_since ----------


def test_first_run_returns_none_since(db):
    """First run delegates the cutoff to the filter chain, not the API call."""
    assert _resolve_since("winetrackr", first_run=True) is None


def test_resolve_since_uses_watermark_first(db):
    ts = datetime(2026, 4, 30, 12, 0, 0)
    _set_last_fetched_at("winetrackr", ts)
    assert _resolve_since("winetrackr", first_run=False) == ts


def test_resolve_since_falls_back_to_events_table(db):
    """If no watermark, prefer max(events.occurred_at) for the repo."""
    from wire.db.models import Event, utc_now

    with db.session_scope() as s:
        s.add(
            Event(
                github_id="evt-x",
                repo="winetrackr",
                event_type="PushEvent",
                payload={},
                occurred_at=datetime(2026, 4, 30, 9, 30, 0),
            )
        )
        # touch utc_now to avoid lint errors about unused import
        _ = utc_now()
    got = _resolve_since("winetrackr", first_run=False)
    assert got == datetime(2026, 4, 30, 9, 30, 0)


def test_resolve_since_floor_when_no_state_no_events(db):
    """The bug we fixed: aventyrligare had first_run=True flipped but
    inserted nothing, so events table was empty AND no watermark. The old
    code passed since=None and re-fetched the entire backlog. Now we floor
    it at -24h."""
    from wire.db.models import utc_now

    got = _resolve_since("aventyrligare", first_run=False)
    assert got is not None
    delta = utc_now() - got
    # Should be ~24h ± a tiny clock skew
    assert (
        timedelta(hours=_MISSING_WATERMARK_FLOOR_HOURS - 1)
        < delta
        < timedelta(hours=_MISSING_WATERMARK_FLOOR_HOURS + 1)
    )


def test_watermark_key_isolation(db):
    """Verify the bot_state key prefix doesn't collide with first-run keys."""
    _mark_first_run_done("winetrackr")
    _set_last_fetched_at("winetrackr", datetime(2026, 4, 30, 0, 0, 0))
    with db.session_scope() as s:
        from sqlalchemy import select

        all_keys = sorted(s.execute(select(BotState.key)).scalars().all())
    assert _FIRST_RUN_KEY_PREFIX + "winetrackr" in all_keys
    assert _LAST_FETCHED_KEY_PREFIX + "winetrackr" in all_keys
    assert len(set(all_keys)) == len(all_keys)  # no duplicates

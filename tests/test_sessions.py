"""Step 6 — session boundary tests.

Covers:
  - idle close (gap > idle_minutes opens a new session, prior closes as 'idle')
  - max_hours close (running session closes once duration > max_hours)
  - immediate-trigger close (release/milestone forces own single-event session)
  - close_idle_sessions (background sweep)
  - per-repo isolation (concurrent activity on two repos = two parallel sessions)
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from wire.db import session as db_session
from wire.db.models import Base, Event, Session
from wire.sessions.detector import (
    DetectorConfig,
    assign_sessions_for_repo,
    close_idle_sessions,
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


def _add_events(
    db,
    repo: str,
    ts_offsets_minutes: list[int],
    event_type: str = "PushEvent",
    base: datetime = datetime(2026, 4, 29, 10, 0, 0),
) -> list[int]:
    ids: list[int] = []
    with db.session_scope() as sa:
        for i, off in enumerate(ts_offsets_minutes):
            e = Event(
                github_id=f"{repo}-{event_type}-{base.timestamp()}-{i}",
                repo=repo,
                event_type=event_type,
                actor="dev",
                payload={},
                occurred_at=base + timedelta(minutes=off),
            )
            sa.add(e)
            sa.flush()
            ids.append(e.id)
    return ids


def _cfg(idle: int = 30, max_h: int = 4, triggers=("release", "milestone")) -> DetectorConfig:
    return DetectorConfig(
        idle_minutes=idle,
        max_hours=max_h,
        immediate_trigger_events=frozenset(triggers),
    )


def test_idle_close_creates_two_sessions(db):
    # Two events 90 minutes apart with idle=30 → two separate sessions
    _add_events(db, "winetrackr", [0, 90])
    n = assign_sessions_for_repo("winetrackr", _cfg(idle=30))
    assert n == 2
    with db.session_scope() as sa:
        sessions = sa.query(Session).order_by(Session.started_at).all()
        assert len(sessions) == 2
        assert sessions[0].closed_reason == "idle"
        # Second one is still open in this pass (no gap-following event yet).
        assert sessions[1].closed_reason is None


def test_close_idle_sessions_sweeps_open(db):
    _add_events(db, "winetrackr", [0, 5])  # one session
    assign_sessions_for_repo("winetrackr", _cfg(idle=30))
    # Now jump time forward; open session goes idle
    later = datetime(2026, 4, 29, 12, 0, 0)
    closed = close_idle_sessions(_cfg(idle=30), now=later)
    assert closed == 1
    with db.session_scope() as sa:
        s = sa.query(Session).one()
        assert s.closed_reason == "idle"


def test_max_hours_close(db):
    # 25-minute gaps so each event extends the session, with max_hours=2 →
    # the session forces a close after 120min total duration.
    _add_events(db, "winetrackr", [0, 25, 50, 75, 100, 125, 150])
    assign_sessions_for_repo("winetrackr", _cfg(idle=30, max_h=2))
    with db.session_scope() as sa:
        # First session covers 0..125 (the 125-min event triggers the max_hours close).
        sessions = sa.query(Session).order_by(Session.started_at).all()
        assert any(s.closed_reason == "max_hours" for s in sessions), (
            f"expected at least one max_hours close in {[s.closed_reason for s in sessions]}"
        )


def test_immediate_trigger_release_opens_singleton_session(db):
    base = datetime(2026, 4, 29, 10, 0, 0)
    # Push events at 0, 5; then a Release at 10
    _add_events(db, "winetrackr", [0, 5], event_type="PushEvent", base=base)
    _add_events(db, "winetrackr", [10], event_type="ReleaseEvent", base=base)
    assign_sessions_for_repo("winetrackr", _cfg(idle=30))
    with db.session_scope() as sa:
        sessions = sa.query(Session).order_by(Session.started_at).all()
        assert len(sessions) == 2
        push_session = sessions[0]
        release_session = sessions[1]
        assert push_session.closed_reason == "idle"  # closed when release came in
        assert release_session.closed_reason == "immediate"
        # Release session contains exactly one event
        assert len([e for e in release_session.events if e.event_type == "ReleaseEvent"]) == 1


def test_milestone_event_also_immediate_trigger(db):
    # Spec lists 'milestone' but GitHub doesn't emit a 'MilestoneEvent' often;
    # detector maps it via _IMMEDIATE_KEY_FOR. Skip if no mapping exists.
    base = datetime(2026, 4, 29, 10, 0, 0)
    _add_events(db, "winetrackr", [0], event_type="MilestoneEvent", base=base)
    assign_sessions_for_repo("winetrackr", _cfg(idle=30))
    with db.session_scope() as sa:
        s = sa.query(Session).one()
        assert s.closed_reason == "immediate"


def test_per_repo_sessions_are_independent(db):
    base = datetime(2026, 4, 29, 10, 0, 0)
    # winetrackr at t=0..5; medianalyzer at t=2..3 (overlapping)
    _add_events(db, "winetrackr", [0, 5], base=base)
    _add_events(db, "medianalyzer", [2, 3], base=base)
    assign_sessions_for_repo("winetrackr", _cfg(idle=30))
    assign_sessions_for_repo("medianalyzer", _cfg(idle=30))
    with db.session_scope() as sa:
        sessions = sa.query(Session).order_by(Session.repo, Session.started_at).all()
        assert len(sessions) == 2
        assert {s.repo for s in sessions} == {"winetrackr", "medianalyzer"}


def test_consecutive_events_within_idle_join_one_session(db):
    _add_events(db, "winetrackr", [0, 5, 10, 15])
    assign_sessions_for_repo("winetrackr", _cfg(idle=30))
    with db.session_scope() as sa:
        sessions = sa.query(Session).all()
        assert len(sessions) == 1
        assert len(sessions[0].events) == 4


def test_close_idle_sessions_idempotent(db):
    _add_events(db, "winetrackr", [0])
    assign_sessions_for_repo("winetrackr", _cfg(idle=30))
    later = datetime(2026, 4, 29, 12, 0, 0)
    n1 = close_idle_sessions(_cfg(idle=30), now=later)
    n2 = close_idle_sessions(_cfg(idle=30), now=later)
    assert n1 == 1
    assert n2 == 0

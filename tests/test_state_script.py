"""Smoke test for wire.scripts.state — verifies it runs without error
against a populated DB and produces the expected sections."""

from __future__ import annotations

from datetime import datetime

import pytest

from wire.db import session as db_session
from wire.db.models import Base, BotState, Event
from wire.scripts.state import main as state_main


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "wire.db"
    monkeypatch.setenv("WIRE_DB_PATH", str(db_path))
    db_session.reset_for_tests()
    engine = db_session.init(db_path)
    Base.metadata.create_all(engine)
    yield db_session
    db_session.reset_for_tests()


def test_state_runs_against_empty_db(db, capsys):
    rc = state_main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "bot_state" in out
    assert "(empty)" in out  # no rows yet
    assert "no repos seen yet" in out


def test_state_groups_and_renders_real_data(db, capsys):
    with db.session_scope() as s:
        s.add_all(
            [
                BotState(key="ingest_completed:helmsman", value="2026-04-30T08:00:00"),
                BotState(key="ingest_completed:winetrackr", value="2026-04-30T08:00:00"),
                BotState(key="last_fetched_at:helmsman", value="2026-04-30T11:19:28"),
                BotState(key="paused_until", value=""),
                BotState(key="budget_alert_pct", value="0.0"),
                Event(
                    github_id="evt-1",
                    repo="helmsman",
                    event_type="PushEvent",
                    payload={},
                    occurred_at=datetime(2026, 4, 30, 11, 19, 28),
                ),
                Event(
                    github_id="evt-2",
                    repo="helmsman",
                    event_type="PullRequestEvent",
                    payload={},
                    occurred_at=datetime(2026, 4, 30, 10, 15, 0),
                ),
            ]
        )

    rc = state_main()
    assert rc == 0
    out = capsys.readouterr().out

    # Section headers
    assert "[runtime flags]" in out
    assert "[first-run flags]" in out
    assert "[fetch watermarks]" in out
    assert "per-repo poll state" in out

    # Specific values are visible
    assert "paused_until" in out
    assert "budget_alert_pct" in out
    assert "ingest_completed:helmsman" in out
    assert "last_fetched_at:helmsman" in out
    assert "2026-04-30T11:19:28" in out

    # Per-repo table includes both helmsman (with events) and winetrackr (no events
    # but has first-run flag set)
    assert "helmsman" in out
    assert "winetrackr" in out
    # helmsman should show 2 events in the per-repo table
    helmsman_lines = [
        line for line in out.splitlines() if "helmsman" in line and "events" not in line
    ]
    assert any("2" in line for line in helmsman_lines)

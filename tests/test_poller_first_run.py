"""Test first-run tracking for ingestion: a quiet repo with no recent events
should NOT be stuck on first_run=True forever."""

from __future__ import annotations

import pytest

from wire.db import session as db_session
from wire.db.models import Base, BotState
from wire.ingestion.poller import (
    _FIRST_RUN_KEY_PREFIX,
    _is_first_run_for,
    _mark_first_run_done,
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
        rows = s.execute(
            select(BotState).where(BotState.key == _FIRST_RUN_KEY_PREFIX + "winetrackr")
        ).scalars().all()
        assert len(rows) == 1

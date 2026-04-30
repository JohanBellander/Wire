"""Step 3 — DB models + Alembic smoke."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

from wire.db import session as db_session
from wire.db.models import (
    Base,
    Decision,
    Draft,
    Event,
    LLMCall,
    Metric,
    Post,
    Session,
    VoiceProfile,
    utc_now,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Fresh SQLite per test using create_all (fast). Migrations are exercised
    in test_alembic_upgrade_creates_all_tables."""
    db_path = tmp_path / "wire-test.db"
    monkeypatch.setenv("WIRE_DB_PATH", str(db_path))
    db_session.reset_for_tests()
    engine = db_session.init(db_path)
    Base.metadata.create_all(engine)
    yield db_session
    db_session.reset_for_tests()


def test_alembic_upgrade_creates_all_tables(tmp_path, monkeypatch):
    """Run the real migration and confirm every table is present."""
    import sqlite3

    db_path = tmp_path / "alembic.db"
    monkeypatch.setenv("WIRE_DB_PATH", str(db_path))
    db_session.reset_for_tests()  # alembic env builds its own engine via make_engine

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    command.upgrade(cfg, "head")

    conn = sqlite3.connect(db_path)
    try:
        names = sorted(
            t
            for (t,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        )
    finally:
        conn.close()

    expected = {
        "alembic_version",
        "bot_state",
        "budget_overrides",
        "decisions",
        "drafts",
        "events",
        "llm_calls",
        "metrics",
        "posts",
        "sessions",
        "voice_profile",
    }
    assert expected.issubset(set(names))


def test_event_unique_github_id(db):
    with db.session_scope() as s:
        s.add(
            Event(
                github_id="evt-1",
                repo="r",
                event_type="push",
                payload={},
                occurred_at=utc_now(),
            )
        )
    with pytest.raises(IntegrityError):
        with db.session_scope() as s:
            s.add(
                Event(
                    github_id="evt-1",
                    repo="r",
                    event_type="push",
                    payload={},
                    occurred_at=utc_now(),
                )
            )


def test_event_session_relationship(db):
    with db.session_scope() as s:
        sess = Session(repo="winetrackr", started_at=utc_now())
        s.add(sess)
        s.flush()
        sid = sess.id
        s.add_all(
            [
                Event(
                    github_id="a",
                    repo="winetrackr",
                    event_type="push",
                    payload={},
                    occurred_at=utc_now(),
                    session_id=sid,
                ),
                Event(
                    github_id="b",
                    repo="winetrackr",
                    event_type="pr",
                    payload={},
                    occurred_at=utc_now() + timedelta(minutes=5),
                    session_id=sid,
                ),
            ]
        )

    with db.session_scope() as s:
        sess = s.get(Session, sid)
        assert len(sess.events) == 2
        assert {e.github_id for e in sess.events} == {"a", "b"}


def test_draft_status_default_pending(db):
    with db.session_scope() as s:
        d = Draft(text="hello", reasoning="r")
        s.add(d)
        s.flush()
        did = d.id
    with db.session_scope() as s:
        d = s.get(Draft, did)
        assert d.status == "pending"


def test_post_decision_metric_chain(db):
    with db.session_scope() as s:
        d = Draft(text="t", status="approved")
        s.add(d)
        s.flush()
        post = Post(draft_id=d.id, twitter_id="tw-1", text="t", posted_at=utc_now())
        s.add(post)
        s.flush()
        s.add_all(
            [
                Decision(draft_id=d.id, decision="approved", decided_at=utc_now()),
                Metric(post_id=post.id, impressions=1, likes=2),
                Metric(
                    post_id=post.id,
                    fetched_at=utc_now() + timedelta(days=1),
                    impressions=10,
                    likes=5,
                ),
            ]
        )

    with db.session_scope() as s:
        post = s.execute(
            __import__("sqlalchemy").select(Post).where(Post.twitter_id == "tw-1")
        ).scalar_one()
        assert len(post.metrics) == 2
        # ordered by fetched_at ascending
        assert post.metrics[0].likes == 2
        assert post.metrics[-1].likes == 5


def test_llm_call_logging(db):
    with db.session_scope() as s:
        s.add(
            LLMCall(
                task="drafting",
                provider="claude",
                model="claude-sonnet-4-6",
                fallback=False,
                input_tokens=1200,
                output_tokens=180,
                cost_usd=0.0072,
                latency_ms=2300,
            )
        )
    with db.session_scope() as s:
        from sqlalchemy import select

        rows = s.execute(select(LLMCall)).scalars().all()
        assert len(rows) == 1
        assert rows[0].fallback is False


def test_voice_profile_writable(db):
    with db.session_scope() as s:
        s.add(VoiceProfile(profile_text="terse, lowercase, debug-story-shaped"))
    with db.session_scope() as s:
        from sqlalchemy import select

        rows = s.execute(select(VoiceProfile)).scalars().all()
        assert rows[0].profile_text.startswith("terse")


def test_payload_json_roundtrip(db):
    payload = {"head_sha": "abc", "files": ["a.py"], "stats": {"add": 10, "del": 3}}
    with db.session_scope() as s:
        s.add(
            Event(
                github_id="evt-json",
                repo="r",
                event_type="push",
                payload=payload,
                occurred_at=utc_now(),
            )
        )
    with db.session_scope() as s:
        from sqlalchemy import select

        e = s.execute(select(Event).where(Event.github_id == "evt-json")).scalar_one()
        # JSON column round-trips dicts/lists
        assert e.payload == payload
        # And just to confirm it's actually stored as JSON, not pickled:
        assert isinstance(e.payload, dict)
        json.dumps(e.payload)

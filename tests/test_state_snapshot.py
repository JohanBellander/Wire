"""Tests for the chat-agent's bot-state snapshot.

The snapshot is what makes "show me last events" / "any rejections lately"
answerable without round-tripping to the DB through tools. These tests
pin the section structure and the casing rules.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from wire.config import (
    ClaudeModelsConfig,
    DigestConfig,
    GithubConfig,
    LearningConfig,
    LLMConfig,
    MetricsConfig,
    OllamaConfig,
    QuietHoursConfig,
    RepoEntry,
    ReposFile,
    ReposLocation,
    SessionConfig,
    TelegramConfig,
    TwitterConfig,
    WireConfig,
)
from wire.db import session as db_session
from wire.db.models import Base, BotState, Decision, Draft, Event, Session
from wire.telegram.state_snapshot import build_state_snapshot


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "wire.db"
    monkeypatch.setenv("WIRE_DB_PATH", str(db_path))
    db_session.reset_for_tests()
    engine = db_session.init(db_path)
    Base.metadata.create_all(engine)
    yield db_session
    db_session.reset_for_tests()


def _config() -> WireConfig:
    return WireConfig(
        github=GithubConfig(
            org="me",
            app_id=1,
            installation_id=1,
            private_key_path="/data/secrets/github-app.pem",
            poll_interval_minutes=20,
        ),
        repos=ReposLocation(config_path="/data/repos.yaml"),
        llm=LLMConfig(
            provider="ollama",
            ollama=OllamaConfig(base_url="http://x", model="m", timeout_seconds=10),
            claude=ClaudeModelsConfig(
                drafting="claude-sonnet-4-6",
                triage="claude-haiku-4-5",
                voice_profile="claude-haiku-4-5",
                digest="claude-haiku-4-5",
            ),
            prompt_caching=True,
            monthly_budget_usd=10,
            budget_alert_threshold=0.8,
        ),
        session=SessionConfig(idle_minutes=30, max_hours=4, immediate_trigger_events=[]),
        quiet_hours=QuietHoursConfig(start="22:00", end="07:00", timezone="Europe/Stockholm"),
        telegram=TelegramConfig(bot_token_env="X", chat_id_env="Y"),
        twitter=TwitterConfig(
            client_id_env="C",
            client_secret_env="S",
            access_token_path="/data/secrets/twitter-token.json",
        ),
        metrics=MetricsConfig(fetch_cron="0 9 * * *", posts_settle_days=7),
        digest=DigestConfig(cron="0 9 * * 1"),
        learning=LearningConfig(recent_decisions_n=20, recent_posts_n=30),
    )


def test_snapshot_contains_top_level_state(db):
    snap = build_state_snapshot(_config(), None)
    assert "─── bot state ───" in snap
    assert "paused: no" in snap
    assert "month spend" in snap
    assert "pending drafts in queue" in snap


def test_snapshot_paused_state_surfaces(db):
    with db.session_scope() as sa:
        sa.add(BotState(key="paused_until", value="2026-05-02T18:00:00"))
    snap = build_state_snapshot(_config(), None)
    assert "paused: yes" in snap
    assert "2026-05-02T18:00:00" in snap


def test_snapshot_paused_indefinite(db):
    """Empty value = indefinite pause (matches commands.is_drafting_paused)."""
    with db.session_scope() as sa:
        sa.add(BotState(key="paused_until", value=""))
    snap = build_state_snapshot(_config(), None)
    assert "paused: yes (indefinite)" in snap


def test_snapshot_renders_recent_events_with_display_names(db):
    repos = ReposFile(repos=[RepoEntry(name="winetrackr", visibility="public")])
    base = datetime(2026, 5, 2, 12, 0, 0)
    with db.session_scope() as sa:
        sa.add(
            Event(
                github_id="ev1",
                repo="winetrackr",
                event_type="PushEvent",
                payload={"raw_payload": {"commits": [{"message": "feat: thing"}]}},
                occurred_at=base,
            )
        )
    snap = build_state_snapshot(_config(), repos)
    assert "recent events" in snap
    # Repo name is capitalized via display_name_for fallback.
    assert "Winetrackr/PushEvent" in snap
    assert "winetrackr/PushEvent" not in snap


def test_snapshot_omits_event_section_when_empty(db):
    snap = build_state_snapshot(_config(), None)
    assert "recent events" not in snap


def test_snapshot_pending_drafts_section(db):
    with db.session_scope() as sa:
        d = Draft(text="this draft hasn't been touched yet", status="pending")
        sa.add(d)
    snap = build_state_snapshot(_config(), None)
    assert "pending drafts (last" in snap
    assert "this draft hasn't been touched yet" in snap


def test_snapshot_recent_decisions_uses_original_text(db):
    """When a draft has been revised through NL edit (original_text set),
    the EDITED line shows the LLM's first try on the left side."""
    with db.session_scope() as sa:
        d = Draft(text="final revised text", original_text="LLM first try", status="edited")
        sa.add(d)
        sa.flush()
        sa.add(Decision(draft_id=d.id, decision="edited", edited_text="final revised text"))

    snap = build_state_snapshot(_config(), None)
    assert "EDITED" in snap
    # Original on the left, final on the right.
    assert '"LLM first try"' in snap
    assert '"final revised text"' in snap


def test_snapshot_lists_allowlisted_repos(db):
    repos = ReposFile(
        repos=[
            RepoEntry(
                name="winetrackr",
                visibility="public",
                display_name="Winetrackr",
                notes="Public side project",
            ),
            RepoEntry(name="medianalyzer", visibility="public", display_name="MediAnalyzer"),
        ]
    )
    snap = build_state_snapshot(_config(), repos)
    assert "allowlisted repos" in snap
    assert "Winetrackr (public) — Public side project" in snap
    assert "MediAnalyzer (public)" in snap


def test_snapshot_handles_recent_events_with_outcomes(db):
    """Each event line should carry an outcome string from the same logic
    /last uses (drafted #N / below-threshold skip / etc.)."""
    base = datetime(2026, 5, 2, 12, 0, 0)
    with db.session_scope() as sa:
        s = Session(
            repo="winetrackr",
            started_at=base,
            ended_at=base + timedelta(minutes=5),
            closed_reason="idle",
            drafted_at=base + timedelta(minutes=10),
        )
        sa.add(s)
        sa.flush()
        d = Draft(session_id=s.id, text="draft text", status="pending")
        sa.add(d)
        sa.flush()
        sa.add(
            Event(
                github_id="e1",
                repo="winetrackr",
                event_type="PushEvent",
                payload={"raw_payload": {"commits": [{"message": "feat: x"}]}},
                occurred_at=base,
                session_id=s.id,
                triage_score=0.7,
            )
        )

    snap = build_state_snapshot(_config(), None)
    assert "drafted #" in snap
    assert "(pending)" in snap

"""Step 7 — drafting prompt assembly tests.

Verifies all expected sections are present, voice profile is injected, and
recent decisions/posts are formatted correctly. Also covers the quiet-hours
predicate and the all-events-below-threshold short-circuit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from wire.config import (
    ClaudeModelsConfig,
    DigestConfig,
    GithubConfig,
    IngestionConfig,
    LearningConfig,
    LLMConfig,
    LoggingConfig,
    MetricsConfig,
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
from wire.db.models import (
    Base,
    Decision,
    Draft,
    Event,
    Metric,
    Post,
    Session,
    VoiceProfile,
    utc_now,
)
from wire.drafting.drafter import (
    DraftResponse,
    _all_events_below_threshold,
    build_prompt_blocks,
    is_in_quiet_hours,
)


def _config(quiet_start="22:00", quiet_end="07:00") -> WireConfig:
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
            provider="claude",
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
        session=SessionConfig(idle_minutes=30, max_hours=4, immediate_trigger_events=["release"]),
        quiet_hours=QuietHoursConfig(start=quiet_start, end=quiet_end, timezone="Europe/Stockholm"),
        telegram=TelegramConfig(bot_token_env="X", chat_id_env="Y"),
        twitter=TwitterConfig(
            client_id_env="C",
            client_secret_env="S",
            access_token_path="/data/secrets/twitter-token.json",
        ),
        metrics=MetricsConfig(fetch_cron="0 9 * * *", posts_settle_days=7),
        digest=DigestConfig(cron="0 9 * * 1"),
        learning=LearningConfig(recent_decisions_n=20, recent_posts_n=30),
        logging=LoggingConfig(),
        ingestion=IngestionConfig(),
    )


def _repos() -> ReposFile:
    return ReposFile(
        repos=[
            RepoEntry(name="winetrackr", visibility="public", notes="Public side project"),
            RepoEntry(name="medianalyzer", visibility="public", notes="Public, boring infra"),
        ]
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


# --- quiet hours ----------------------------------------------------------


def test_quiet_hours_wraps_midnight():
    qh = QuietHoursConfig(start="22:00", end="07:00", timezone="Europe/Stockholm")
    # 23:00 Stockholm → in quiet
    t = datetime(2026, 4, 29, 21, 0, tzinfo=UTC)  # 23:00 CEST
    assert is_in_quiet_hours(qh, now=t)
    # 03:00 Stockholm → in quiet
    t = datetime(2026, 4, 29, 1, 0, tzinfo=UTC)  # 03:00 CEST
    assert is_in_quiet_hours(qh, now=t)
    # 12:00 Stockholm → not quiet
    t = datetime(2026, 4, 29, 10, 0, tzinfo=UTC)  # 12:00 CEST
    assert not is_in_quiet_hours(qh, now=t)


def test_quiet_hours_non_wrap():
    qh = QuietHoursConfig(start="13:00", end="14:00", timezone="UTC")
    assert is_in_quiet_hours(qh, now=datetime(2026, 4, 29, 13, 30, tzinfo=UTC))
    assert not is_in_quiet_hours(qh, now=datetime(2026, 4, 29, 14, 0, tzinfo=UTC))
    assert not is_in_quiet_hours(qh, now=datetime(2026, 4, 29, 12, 59, tzinfo=UTC))


# --- triage threshold short-circuit ------------------------------------------


def test_all_events_below_threshold(db):
    with db.session_scope() as sa:
        s = Session(repo="r", started_at=utc_now())
        sa.add(s)
        sa.flush()
        sa.add_all(
            [
                Event(
                    github_id="a",
                    repo="r",
                    event_type="PushEvent",
                    payload={},
                    occurred_at=utc_now(),
                    session_id=s.id,
                    triage_score=0.1,
                ),
                Event(
                    github_id="b",
                    repo="r",
                    event_type="PushEvent",
                    payload={},
                    occurred_at=utc_now(),
                    session_id=s.id,
                    triage_score=0.25,
                ),
            ]
        )
        sid = s.id
    assert _all_events_below_threshold(sid, 0.3) is True


def test_one_event_above_threshold_keeps_session(db):
    with db.session_scope() as sa:
        s = Session(repo="r", started_at=utc_now())
        sa.add(s)
        sa.flush()
        sa.add_all(
            [
                Event(
                    github_id="a",
                    repo="r",
                    event_type="PushEvent",
                    payload={},
                    occurred_at=utc_now(),
                    session_id=s.id,
                    triage_score=0.1,
                ),
                Event(
                    github_id="b",
                    repo="r",
                    event_type="PushEvent",
                    payload={},
                    occurred_at=utc_now(),
                    session_id=s.id,
                    triage_score=0.7,
                ),
            ]
        )
        sid = s.id
    assert _all_events_below_threshold(sid, 0.3) is False


# --- prompt assembly ---------------------------------------------------------


def test_prompt_blocks_contain_all_sections(db):
    cfg = _config()
    repos = _repos()
    with db.session_scope() as sa:
        sa.add(VoiceProfile(profile_text="terse, lowercase, debug-story-shaped"))
        # add a settled post + metric
        d = Draft(text="some old draft", status="approved")
        sa.add(d)
        sa.flush()
        p = Post(
            draft_id=d.id,
            twitter_id="tw1",
            text="settled text",
            posted_at=utc_now() - timedelta(days=14),
        )
        sa.add(p)
        sa.flush()
        sa.add(Metric(post_id=p.id, impressions=900, likes=42))
        # add a recent decision
        sa.add(Decision(draft_id=d.id, decision="rejected", reject_reason="boring"))

        sess = Session(
            repo="winetrackr",
            started_at=utc_now() - timedelta(minutes=30),
            ended_at=utc_now(),
            closed_reason="idle",
        )
        sa.add(sess)
        sa.flush()
        sa.add(
            Event(
                github_id="ev1",
                repo="winetrackr",
                event_type="PushEvent",
                payload={"raw_payload": {"commits": [{"message": "feat: ship a thing"}]}},
                occurred_at=utc_now(),
                session_id=sess.id,
                triage_score=0.7,
                triage_reason="real feature",
            )
        )
        sid = sess.id

    with db.session_scope() as sa:
        sess_obj = sa.get(Session, sid)
        _ = list(sess_obj.events)
        sa.expunge_all()

    blocks = build_prompt_blocks(sess_obj, cfg, repos)

    sys_text = "\n".join(b["text"] for b in blocks.system_blocks)
    # System prompt body
    assert "Voice profile" in sys_text
    assert "terse, lowercase" in sys_text
    # Performance + decisions block
    assert "Recent posts" in sys_text
    assert "settled text" in sys_text
    assert "impr=900" in sys_text
    assert "Recent decisions" in sys_text
    assert "REJECTED" in sys_text
    assert "boring" in sys_text
    # The "developer friend" mental-model framing must survive into the
    # loaded system prompt — guards against accidental deletion.
    assert "developer friend" in sys_text or "developer-friend" in sys_text

    # User message body
    user = blocks.user_message
    # Repo name is capitalized for display (winetrackr → Winetrackr); see
    # `wire.util.repo_names.display_name_for`. Per-repo overrides land via
    # `RepoEntry.display_name` in repos.yaml.
    assert "Repo: Winetrackr (public)" in user
    assert "Repo notes: Public side project" in user
    assert "Closed reason: idle" in user
    assert "feat: ship a thing" in user
    assert 'reason="real feature"' in user
    assert "Task" in user


def test_prompt_caching_markers_present_when_enabled(db):
    cfg = _config()
    repos = _repos()
    with db.session_scope() as sa:
        sess = Session(
            repo="winetrackr", started_at=utc_now(), ended_at=utc_now(), closed_reason="idle"
        )
        sa.add(sess)
        sa.flush()
        sa.add(
            Event(
                github_id="ev1",
                repo="winetrackr",
                event_type="PushEvent",
                payload={"raw_payload": {}},
                occurred_at=utc_now(),
                session_id=sess.id,
                triage_score=0.5,
            )
        )
        sid = sess.id
    with db.session_scope() as sa:
        s = sa.get(Session, sid)
        _ = list(s.events)
        sa.expunge_all()

    blocks = build_prompt_blocks(s, cfg, repos)
    # Two system blocks, both with cache_control
    assert len(blocks.system_blocks) == 2
    for b in blocks.system_blocks:
        assert "cache_control" in b
    # First (system+voice) has 1h ttl; second (posts+decisions) is default (5m → no ttl key)
    assert blocks.system_blocks[0]["cache_control"].get("ttl") == "1h"
    assert "ttl" not in blocks.system_blocks[1]["cache_control"]


def test_prompt_caching_absent_when_disabled(db):
    cfg = _config()
    cfg.llm.prompt_caching = False
    repos = _repos()
    with db.session_scope() as sa:
        sess = Session(
            repo="winetrackr", started_at=utc_now(), ended_at=utc_now(), closed_reason="idle"
        )
        sa.add(sess)
        sa.flush()
        sa.add(
            Event(
                github_id="ev1",
                repo="winetrackr",
                event_type="PushEvent",
                payload={"raw_payload": {}},
                occurred_at=utc_now(),
                session_id=sess.id,
                triage_score=0.5,
            )
        )
        sid = sess.id
    with db.session_scope() as sa:
        s = sa.get(Session, sid)
        _ = list(s.events)
        sa.expunge_all()

    blocks = build_prompt_blocks(s, cfg, repos)
    for b in blocks.system_blocks:
        assert "cache_control" not in b


def test_voice_profile_fallback_when_none(db):
    cfg = _config()
    repos = _repos()
    with db.session_scope() as sa:
        sess = Session(
            repo="winetrackr", started_at=utc_now(), ended_at=utc_now(), closed_reason="idle"
        )
        sa.add(sess)
        sa.flush()
        sa.add(
            Event(
                github_id="x",
                repo="winetrackr",
                event_type="PushEvent",
                payload={"raw_payload": {}},
                occurred_at=utc_now(),
                session_id=sess.id,
                triage_score=0.5,
            )
        )
        sid = sess.id
    with db.session_scope() as sa:
        s = sa.get(Session, sid)
        _ = list(s.events)
        sa.expunge_all()
    blocks = build_prompt_blocks(s, cfg, repos)
    sys_text = "\n".join(b["text"] for b in blocks.system_blocks)
    assert "no voice profile yet" in sys_text


def test_prompt_includes_readme_when_cached(db):
    """A cached README for the session's repo lands as its own 1h-cached block."""
    from wire.db.models import BotState

    cfg = _config()
    repos = _repos()
    with db.session_scope() as sa:
        sa.add(
            BotState(
                key="readme:winetrackr",
                value=(
                    "# winetrackr\n\nA wine cellar tracker for tracking which "
                    "vintages I have and when they should be drunk."
                ),
            )
        )
        sess = Session(
            repo="winetrackr",
            started_at=utc_now(),
            ended_at=utc_now(),
            closed_reason="idle",
        )
        sa.add(sess)
        sa.flush()
        sa.add(
            Event(
                github_id="ev1",
                repo="winetrackr",
                event_type="PushEvent",
                payload={"raw_payload": {}},
                occurred_at=utc_now(),
                session_id=sess.id,
                triage_score=0.7,
            )
        )
        sid = sess.id
    with db.session_scope() as sa:
        s = sa.get(Session, sid)
        _ = list(s.events)
        sa.expunge_all()

    blocks = build_prompt_blocks(s, cfg, repos)
    # Three system blocks now: system+voice, about-this-repo, learning
    assert len(blocks.system_blocks) == 3
    sys_text = "\n".join(b["text"] for b in blocks.system_blocks)
    assert "About Winetrackr" in sys_text  # display-name capitalized
    assert "wine cellar tracker" in sys_text
    # README block has 1h cache (same as system+voice)
    repo_block = blocks.system_blocks[1]
    assert repo_block["cache_control"].get("ttl") == "1h"


def test_prompt_omits_readme_block_when_not_cached(db):
    """No cache entry → no README block; back to the original 2-block shape."""
    cfg = _config()
    repos = _repos()
    with db.session_scope() as sa:
        sess = Session(
            repo="winetrackr",
            started_at=utc_now(),
            ended_at=utc_now(),
            closed_reason="idle",
        )
        sa.add(sess)
        sa.flush()
        sa.add(
            Event(
                github_id="ev1",
                repo="winetrackr",
                event_type="PushEvent",
                payload={"raw_payload": {}},
                occurred_at=utc_now(),
                session_id=sess.id,
                triage_score=0.7,
            )
        )
        sid = sess.id
    with db.session_scope() as sa:
        s = sa.get(Session, sid)
        _ = list(s.events)
        sa.expunge_all()

    blocks = build_prompt_blocks(s, cfg, repos)
    assert len(blocks.system_blocks) == 2
    sys_text = "\n".join(b["text"] for b in blocks.system_blocks)
    assert "About Winetrackr" not in sys_text
    assert "About winetrackr" not in sys_text


# --- DraftResponse schema ----------------------------------------------------


def test_draft_response_skip_only():
    payload = {"skip_reason": "all events too internal", "drafts": []}
    r = DraftResponse.model_validate(payload)
    assert r.skip_reason == "all events too internal"
    assert r.drafts == []


def test_draft_response_with_drafts():
    payload = {
        "skip_reason": None,
        "drafts": [
            {"text": "hello world", "reasoning": "specific feature shipped", "confidence": 0.8},
        ],
    }
    r = DraftResponse.model_validate(payload)
    assert len(r.drafts) == 1
    assert r.drafts[0].confidence == 0.8


def test_draft_response_invalid_confidence():
    from pydantic import ValidationError

    payload = {"drafts": [{"text": "x", "reasoning": "y", "confidence": 1.5}]}
    with pytest.raises(ValidationError):
        DraftResponse.model_validate(payload)

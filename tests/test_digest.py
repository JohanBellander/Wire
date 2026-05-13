"""Step 10 — weekly digest builder smoke test."""

from __future__ import annotations

from datetime import timedelta

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
    LLMCall,
    Metric,
    Post,
    utc_now,
)
from wire.digest.builder import format_digest, gather_numbers


def _config() -> WireConfig:
    return WireConfig(
        github=GithubConfig(
            org="me",
            app_id=1,
            installation_id=1,
            private_key_path="/d/k.pem",
            poll_interval_minutes=20,
        ),
        repos=ReposLocation(config_path="/d/r.yaml"),
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
        session=SessionConfig(idle_minutes=30, max_hours=4, immediate_trigger_events=[]),
        quiet_hours=QuietHoursConfig(start="22:00", end="07:00", timezone="UTC"),
        telegram=TelegramConfig(bot_token_env="X", chat_id_env="Y"),
        twitter=TwitterConfig(
            client_id_env="C", client_secret_env="S", access_token_path="/d/t.json"
        ),
        metrics=MetricsConfig(fetch_cron="0 9 * * *", posts_settle_days=7),
        digest=DigestConfig(cron="0 9 * * 1"),
        learning=LearningConfig(recent_decisions_n=20, recent_posts_n=30),
        logging=LoggingConfig(),
        ingestion=IngestionConfig(),
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


def test_digest_with_no_data(db):
    cfg = _config()
    n = gather_numbers(cfg)
    assert n.drafted == 0
    assert n.posted == 0
    text = format_digest(n)
    assert "Last 7 days" in text
    assert "Drafted: 0" in text


def test_digest_counts_recent_activity(db):
    cfg = _config()
    now = utc_now()
    with db.session_scope() as sa:
        # 3 drafts created in last week, 1 older
        for i in range(3):
            d = Draft(text=f"recent {i}")
            d.created_at = now - timedelta(days=2)
            sa.add(d)
        old = Draft(text="old")
        old.created_at = now - timedelta(days=20)
        sa.add(old)

        # 2 approved decisions, 1 rejected, all within last week
        sa.flush()
        for d in sa.query(Draft).filter(Draft.created_at > now - timedelta(days=7)).all():
            sa.add(Decision(draft_id=d.id, decision="approved"))
        # one rejected too — separate fresh draft
        d2 = Draft(text="rej one")
        d2.created_at = now - timedelta(days=1)
        sa.add(d2)
        sa.flush()
        sa.add(Decision(draft_id=d2.id, decision="rejected", reject_reason="boring"))

        # 2 posted last week
        for i in range(2):
            d3 = Draft(text=f"posted {i}")
            sa.add(d3)
            sa.flush()
            p = Post(
                draft_id=d3.id,
                twitter_id=f"tw-{i}",
                text=f"posted {i}",
                posted_at=now - timedelta(days=1),
            )
            sa.add(p)

    n = gather_numbers(cfg)
    # 3 "recent" + 1 "rej one" + 2 "posted" = 6 drafts in the last 7 days
    assert n.drafted == 6
    assert n.posted == 2
    assert n.rejected == 1
    text = format_digest(n)
    assert "Posted: 2" in text


def test_digest_top_and_below_median_with_settled_posts(db):
    cfg = _config()
    now = utc_now()
    with db.session_scope() as sa:
        for i, impr in enumerate([1200, 890, 180, 50, 450]):
            d = Draft(text=f"x{i}")
            sa.add(d)
            sa.flush()
            p = Post(
                draft_id=d.id,
                twitter_id=f"t{i}",
                text=f"settled post {i}",
                posted_at=now - timedelta(days=14),
            )
            sa.add(p)
            sa.flush()
            sa.add(
                Metric(
                    post_id=p.id,
                    impressions=impr,
                    likes=impr // 30,
                    retweets=0,
                    replies=0,
                    bookmarks=0,
                )
            )

    n = gather_numbers(cfg)
    assert len(n.top) == 3
    # Top is sorted by impressions desc
    assert (n.top[0][1].impressions or 0) >= (n.top[1][1].impressions or 0)


def test_digest_fallback_rate(db):
    cfg = _config()
    with db.session_scope() as sa:
        for fb in (False, False, True, False, True):
            sa.add(
                LLMCall(
                    task="drafting",
                    provider="llamacpp" if not fb else "claude",
                    model="claude-sonnet-4-6" if not fb else None,
                    fallback=fb,
                    input_tokens=100,
                    output_tokens=50,
                    cost_usd=0.001,
                    latency_ms=1000,
                )
            )
    n = gather_numbers(cfg)
    # 2 / 5 = 40% fallback
    assert 39 <= n.fallback_rate_pct <= 41

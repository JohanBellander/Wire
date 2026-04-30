"""Step 11 — budget tracking + alert tests."""

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
    OllamaConfig,
    QuietHoursConfig,
    ReposLocation,
    SessionConfig,
    TelegramConfig,
    TwitterConfig,
    WireConfig,
)
from wire.db import session as db_session
from wire.db.models import Base, LLMCall, utc_now
from wire.llm.alerts import (
    evaluate_alert,
    is_drafting_blocked_by_budget,
)
from wire.llm.budget import compute_status, current_month_key, record_extension


def _config(cap=10.0, threshold=0.8) -> WireConfig:
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
            ollama=OllamaConfig(base_url="http://x", model="m", timeout_seconds=10),
            claude=ClaudeModelsConfig(
                drafting="claude-sonnet-4-6",
                triage="claude-haiku-4-5",
                voice_profile="claude-haiku-4-5",
                digest="claude-haiku-4-5",
            ),
            prompt_caching=True,
            monthly_budget_usd=cap,
            budget_alert_threshold=threshold,
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


def _add_spend(db, dollars: float, when: datetime | None = None):
    when = when or utc_now()
    with db.session_scope() as sa:
        sa.add(
            LLMCall(
                task="drafting",
                provider="claude",
                model="claude-sonnet-4-6",
                fallback=False,
                input_tokens=1,
                output_tokens=1,
                cost_usd=dollars,
                latency_ms=10,
                called_at=when,
            )
        )


def test_compute_status_no_spend(db):
    cfg = _config()
    with db.session_scope() as sa:
        s = compute_status(sa, cfg.llm.monthly_budget_usd, cfg.llm.budget_alert_threshold)
    assert s.spend_usd == 0.0
    assert s.cap_usd == 10.0
    assert s.paused is False
    assert s.warning is False


def test_warning_triggered_at_80pct(db):
    cfg = _config()
    _add_spend(db, 8.0)
    with db.session_scope() as sa:
        s = compute_status(sa, 10.0, 0.8)
    assert s.warning is True
    assert s.paused is False
    assert is_drafting_blocked_by_budget(cfg) is False


def test_pause_at_100pct(db):
    cfg = _config()
    _add_spend(db, 10.0)
    with db.session_scope() as sa:
        s = compute_status(sa, 10.0, 0.8)
    assert s.paused is True
    assert is_drafting_blocked_by_budget(cfg) is True


def test_extend_raises_cap(db):
    cfg = _config()
    _add_spend(db, 10.0)
    assert is_drafting_blocked_by_budget(cfg) is True
    with db.session_scope() as sa:
        record_extension(sa, 5.0, reason="test")
    with db.session_scope() as sa:
        s = compute_status(sa, 10.0, 0.8)
    assert s.cap_usd == 15.0
    assert is_drafting_blocked_by_budget(cfg) is False


def test_evaluate_alert_warns_then_does_not_repeat(db):
    cfg = _config()
    _add_spend(db, 8.5)  # 85%
    s1, txt1 = evaluate_alert(cfg)
    s2, txt2 = evaluate_alert(cfg)  # same level — should be silent
    assert s1.warning is True
    assert txt1 is not None and "85" in txt1.replace(" ", "")
    assert txt2 is None


def test_evaluate_alert_pause_after_warn(db):
    cfg = _config()
    _add_spend(db, 8.5)
    _, _ = evaluate_alert(cfg)  # warn
    _add_spend(db, 2.0)  # pushes over 100%
    _, txt = evaluate_alert(cfg)
    assert txt is not None and "paused" in txt


def test_evaluate_alert_resets_when_extended(db):
    cfg = _config()
    _add_spend(db, 10.0)
    _, _ = evaluate_alert(cfg)  # records 1.0
    with db.session_scope() as sa:
        record_extension(sa, 5.0, reason="bumped")
    s, txt = evaluate_alert(cfg)
    # Now at 67% of 15 → not warning
    assert s.warning is False
    assert txt is None


def test_spend_outside_month_excluded(db):
    last_month = utc_now().replace(day=1) - timedelta(days=5)  # ~25 days ago
    _add_spend(db, 50.0, when=last_month)
    _add_spend(db, 1.0)
    with db.session_scope() as sa:
        s = compute_status(sa, 10.0, 0.8)
    assert s.spend_usd == 1.0


def test_current_month_key_format():
    k = current_month_key(datetime(2026, 4, 5, tzinfo=UTC))
    assert k == "2026-04"
    k = current_month_key(datetime(2026, 12, 31, tzinfo=UTC))
    assert k == "2026-12"

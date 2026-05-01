"""Step 12 — end-to-end integration test.

Stub GitHub events → triage → session → draft → "would-send-to-Telegram"
assertion. No real network calls; LLM provider is stubbed; Telegram
Application's send_message is mocked.

Goal: prove the wiring between modules works for a realistic-shaped flow.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

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
    Draft,
    Event,
    LLMCall,
    Session,
)
from wire.drafting.drafter import (
    DraftItem,
    DraftResponse,
    draft_pending_sessions,
)
from wire.ingestion.filters import NormalizedEvent, apply_all, build_default_chain
from wire.llm.provider import LLMResponse
from wire.sessions.detector import (
    DetectorConfig,
    assign_sessions_for_repo,
    close_idle_sessions,
)


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
        # Use a window that does NOT include "now" so quiet-hours doesn't defer.
        quiet_hours=QuietHoursConfig(start="03:00", end="04:00", timezone="UTC"),
        session=SessionConfig(idle_minutes=30, max_hours=4, immediate_trigger_events=["release"]),
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


def _repos() -> ReposFile:
    return ReposFile(
        repos=[
            RepoEntry(
                name="winetrackr", visibility="public", notes="Public side project, post freely"
            ),
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


# ---------- stub LLM provider that returns a canned drafting response -------


class _StubProvider:
    def __init__(self, drafts_response: DraftResponse, model: str = "claude-sonnet-4-6"):
        self._payload = drafts_response.model_dump_json()
        self._model = model
        self.calls: list[dict] = []

    async def complete(self, task, system, messages, response_format=None, max_tokens=1500):
        self.calls.append(
            {
                "task": task,
                "system": system,
                "messages": messages,
                "response_format": response_format,
            }
        )
        return LLMResponse(
            content=self._payload,
            provider="claude",
            model=self._model,
            input_tokens=900,
            output_tokens=80,
            cache_read_tokens=600,
            cache_write_tokens=300,
            latency_ms=1500,
            cost_usd=0.0042,
            task=task,
        )


# ---------- e2e --------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_path_events_to_telegram(db, monkeypatch):
    cfg = _config()
    repos = _repos()

    # 1. Stub events as if ingestion just normalized them. We bypass GitHub
    #    by writing pre-normalized events through the filter chain and
    #    inserting them directly.
    base = datetime(2026, 4, 29, 12, 0, 0)
    raw_events = [
        NormalizedEvent(
            github_id="ev-1",
            repo="winetrackr",
            event_type="PushEvent",
            actor="jbellander",
            occurred_at=base,
            default_branch="main",
            branch="main",
            commit_messages=["feat: add price-history graph"],
            payload={
                "raw_payload": {
                    "commits": [{"sha": "abc", "message": "feat: add price-history graph"}]
                }
            },
        ),
        NormalizedEvent(
            github_id="ev-2",
            repo="winetrackr",
            event_type="PullRequestEvent",
            actor="jbellander",
            occurred_at=base + timedelta(minutes=10),
            default_branch="main",
            branch=None,
            pr_merged=True,
            payload={
                "raw_payload": {
                    "action": "closed",
                    "pull_request": {
                        "merged": True,
                        "title": "Add price-history graph",
                        "html_url": "x",
                    },
                }
            },
        ),
        # A bot push and a chore push — should be filtered out
        NormalizedEvent(
            github_id="ev-3",
            repo="winetrackr",
            event_type="PushEvent",
            actor="dependabot[bot]",
            occurred_at=base + timedelta(minutes=12),
            default_branch="main",
            branch="dependabot/bumps",
            commit_messages=["chore: bump foo"],
            payload={"raw_payload": {"commits": [{"sha": "x", "message": "chore: bump foo"}]}},
        ),
        NormalizedEvent(
            github_id="ev-4",
            repo="winetrackr",
            event_type="PushEvent",
            actor="jbellander",
            occurred_at=base + timedelta(minutes=15),
            default_branch="main",
            branch="main",
            commit_messages=["chore: format", "ci: tweak workflow", "docs: typo"],
            payload={"raw_payload": {"commits": []}},
        ),
        # Out-of-allowlist
        NormalizedEvent(
            github_id="ev-5",
            repo="visma-secret-thing",
            event_type="PushEvent",
            actor="jbellander",
            occurred_at=base,
            default_branch="main",
            branch="main",
            commit_messages=["feat: secrets"],
            payload={"raw_payload": {"commits": []}},
        ),
    ]

    chain = build_default_chain(
        allowlist=repos.names(),
        skip_commit_patterns=cfg.ingestion.skip_commit_patterns,
        first_run=False,
    )
    res = apply_all(raw_events, chain)
    assert len(res.kept) == 2  # ev-1, ev-2
    drop_reasons = {d[1] for d in res.dropped}
    assert any("allowlist" in r for r in drop_reasons)
    assert any("bot" in r for r in drop_reasons)
    assert any("skip patterns" in r for r in drop_reasons)

    with db.session_scope() as sa:
        for n in res.kept:
            sa.add(
                Event(
                    github_id=n.github_id,
                    repo=n.repo,
                    event_type=n.event_type,
                    actor=n.actor,
                    payload=n.payload or {},
                    occurred_at=n.occurred_at,
                    triage_score=0.7,  # pretend triage already scored these
                    triage_reason="real feature work",
                )
            )

    # 2. Run session detection.
    n_assigned = assign_sessions_for_repo(
        "winetrackr",
        DetectorConfig(
            idle_minutes=30, max_hours=4, immediate_trigger_events=frozenset({"release"})
        ),
    )
    assert n_assigned == 2

    # Force-close so drafting picks them up.
    later = base + timedelta(hours=2)
    close_idle_sessions(
        DetectorConfig(
            idle_minutes=30, max_hours=4, immediate_trigger_events=frozenset({"release"})
        ),
        now=later,
    )
    with db.session_scope() as sa:
        sessions = sa.query(Session).all()
        assert len(sessions) == 1
        assert sessions[0].closed_reason == "idle"
        sid = sessions[0].id

    # 3. Run drafter with a stub provider that returns a single draft.
    canned = DraftResponse(
        skip_reason=None,
        drafts=[
            DraftItem(
                text=(
                    "Just shipped a price-history graph in winetrackr — turns out "
                    "querying historical prices in chunks is way faster than one big "
                    "window function."
                ),
                reasoning="Concrete feature shipped + a small technical insight.",
                confidence=0.78,
            )
        ],
    )
    stub = _StubProvider(canned)
    # now=base (not in quiet hours UTC 03-04)
    results = await draft_pending_sessions(cfg, repos, stub, now=base)
    assert len(results) == 1
    assert results[0].drafts_created == 1
    assert results[0].deferred_quiet_hours is False

    with db.session_scope() as sa:
        drafts = sa.query(Draft).all()
        assert len(drafts) == 1
        assert drafts[0].session_id == sid
        assert drafts[0].text.startswith("Just shipped")
        # Provider call was logged
        calls = sa.query(LLMCall).all()
        assert len(calls) == 1
        assert calls[0].task == "drafting"

    # 4. Telegram send: don't run the real Application, just assert send_draft
    #    would be called. Stub the bot.
    from wire.telegram import bot as bot_mod

    fake_bot = MagicMock()
    fake_bot.bot_data = {"wire_chat_id": 999}
    sent = MagicMock()
    sent.message_id = 12345
    fake_bot.bot.send_message = AsyncMock(return_value=sent)

    msg_id = await bot_mod.send_draft(fake_bot, drafts[0].id)
    assert msg_id == 12345
    fake_bot.bot.send_message.assert_awaited_once()
    args = fake_bot.bot.send_message.await_args
    body_text = args.kwargs["text"]
    assert "Just shipped" in body_text
    # Persona voice renders the header lowercase: "📝 draft #N · repo".
    assert "📝 draft" in body_text
    # Inline keyboard with 4 buttons
    keyboard = args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard
    assert len(keyboard.inline_keyboard[0]) == 4

    with db.session_scope() as sa:
        d = sa.get(Draft, drafts[0].id)
        assert d.telegram_message_id == 12345


@pytest.mark.asyncio
async def test_full_path_skip_below_threshold_no_llm_call(db, monkeypatch):
    cfg = _config()
    repos = _repos()
    base = datetime(2026, 4, 29, 12, 0, 0)
    with db.session_scope() as sa:
        s = Session(
            repo="winetrackr",
            started_at=base,
            ended_at=base + timedelta(minutes=10),
            closed_reason="idle",
        )
        sa.add(s)
        sa.flush()
        sa.add_all(
            [
                Event(
                    github_id="a",
                    repo="winetrackr",
                    event_type="PushEvent",
                    payload={},
                    occurred_at=base,
                    session_id=s.id,
                    triage_score=0.1,
                    triage_reason="boring",
                ),
                Event(
                    github_id="b",
                    repo="winetrackr",
                    event_type="PushEvent",
                    payload={},
                    occurred_at=base + timedelta(minutes=5),
                    session_id=s.id,
                    triage_score=0.2,
                    triage_reason="version bump",
                ),
            ]
        )

    canned = DraftResponse(
        skip_reason=None,
        drafts=[
            DraftItem(
                text="x",
                reasoning="y",
                confidence=0.5,
            )
        ],
    )
    stub = _StubProvider(canned)
    results = await draft_pending_sessions(cfg, repos, stub, now=base)
    assert len(results) == 1
    assert results[0].drafts_created == 0
    assert results[0].skip_reason == "below_threshold"
    # Provider was NOT called
    assert len(stub.calls) == 0
    with db.session_scope() as sa:
        # Session still got drafted_at set so we don't reprocess
        sess = sa.query(Session).one()
        assert sess.drafted_at is not None

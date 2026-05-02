"""Tests for the conversational chat agent.

Stubs the LLM provider so behavior is deterministic. The tests assert
the agent's two responsibilities: (1) render `reply` to Telegram via
the existing `_reply` helper, (2) execute each `actions` entry by
delegating to the matching slash-command handler.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from wire.config import (
    ClaudeModelsConfig,
    DigestConfig,
    GithubConfig,
    LearningConfig,
    LLMConfig,
    MetricsConfig,
    OllamaConfig,
    PersonaConfig,
    QuietHoursConfig,
    ReposLocation,
    SessionConfig,
    TelegramConfig,
    TwitterConfig,
    WireConfig,
)
from wire.db import session as db_session
from wire.db.models import Base
from wire.llm.provider import LLMResponse
from wire.telegram import chat as chat_mod
from wire.telegram import commands as cmds


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
        persona=PersonaConfig(),
    )


class _StubProvider:
    """Returns a canned ChatResponse JSON. Records each call so tests can
    assert prompt structure."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[dict] = []

    async def complete(self, task, system, messages, response_format=None, max_tokens=500):
        self.calls.append({"task": task, "system": system, "messages": messages})
        return LLMResponse(
            content=json.dumps(self.payload),
            provider="ollama",
            model="m",
            input_tokens=30,
            output_tokens=15,
            cache_read_tokens=0,
            cache_write_tokens=0,
            latency_ms=20,
            cost_usd=0.0,
            task=task,
        )


def _make_update(text: str = "x", user_id: int = 42, chat_id: int = 1) -> MagicMock:
    update = MagicMock()
    update.callback_query = None
    update.message.text = text
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.effective_message.reply_text = AsyncMock()
    return update


def _make_context(provider) -> MagicMock:
    ctx = MagicMock()
    ctx.bot_data = {
        "wire_chat_id": 1,
        "wire_twitter": None,
        "wire_config": _config(),
        "wire_provider": provider,
        "wire_repos": None,
    }
    ctx.bot.send_message = AsyncMock()
    return ctx


# --- reply-only path --------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_reply_only_sends_one_message(db):
    """A reply with no actions: just rendered to Telegram."""
    stub = _StubProvider({"reply": "all clean, johan. no drafts pending.", "actions": []})
    update = _make_update("how are we doing")
    ctx = _make_context(stub)

    await chat_mod.handle_message("how are we doing", update, ctx)

    update.effective_message.reply_text.assert_awaited_once()
    text = update.effective_message.reply_text.await_args.args[0]
    assert text == "all clean, johan. no drafts pending."


@pytest.mark.asyncio
async def test_chat_logs_llm_call(db):
    """Every provider.complete must be followed by log_llm_call.
    Buckets under task='persona' alongside the other Telegram surfaces."""
    from wire.db.models import LLMCall

    stub = _StubProvider({"reply": "hi", "actions": []})
    await chat_mod.handle_message("hi", _make_update(), _make_context(stub))

    with db.session_scope() as sa:
        rows = sa.query(LLMCall).all()
        assert len(rows) == 1
        assert rows[0].task == "persona"


@pytest.mark.asyncio
async def test_chat_routes_through_persona_task(db):
    """Provider chain: local primary, Claude fallback. The agent uses the
    persona model_task ('triage' by default — Haiku-tier in fallback)."""
    stub = _StubProvider({"reply": "ok", "actions": []})
    await chat_mod.handle_message("hi", _make_update(), _make_context(stub))
    assert stub.calls[0]["task"] == "triage"


# --- snapshot inclusion -----------------------------------------------------


@pytest.mark.asyncio
async def test_chat_includes_state_snapshot_in_user_message(db):
    """The fresh snapshot must land in the user message every turn."""
    stub = _StubProvider({"reply": "ok", "actions": []})
    update = _make_update("status check")
    await chat_mod.handle_message("status check", update, _make_context(stub))

    user_msg = stub.calls[0]["messages"][-1]["content"]
    assert "─── bot state ───" in user_msg
    assert "month spend" in user_msg
    # Johan's actual message is appended after the snapshot.
    assert "status check" in user_msg


# --- action dispatch --------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_pause_action_calls_pause_cmd(db, monkeypatch):
    fake_pause = AsyncMock()
    monkeypatch.setattr(cmds, "pause_cmd", fake_pause)

    stub = _StubProvider(
        {
            "reply": "going dark for 2 hours.",
            "actions": [{"name": "pause", "args": {"hours": 2}}],
        }
    )
    update = _make_update("pause for 2 hours")
    ctx = _make_context(stub)

    await chat_mod.handle_message("pause for 2 hours", update, ctx)

    fake_pause.assert_awaited_once()
    # Pause cmd reads context.args = ["2"]
    assert ctx.args == ["2"]


@pytest.mark.asyncio
async def test_chat_resume_action_calls_resume_cmd(db, monkeypatch):
    fake_resume = AsyncMock()
    monkeypatch.setattr(cmds, "resume_cmd", fake_resume)

    stub = _StubProvider({"reply": "back online.", "actions": [{"name": "resume", "args": {}}]})
    update = _make_update("wake up")
    ctx = _make_context(stub)
    await chat_mod.handle_message("wake up", update, ctx)

    fake_resume.assert_awaited_once()
    assert ctx.args == []


@pytest.mark.asyncio
async def test_chat_extend_action_passes_usd(db, monkeypatch):
    fake_extend = AsyncMock()
    monkeypatch.setattr(cmds, "extend_cmd", fake_extend)

    stub = _StubProvider(
        {
            "reply": "raising the cap by $25.",
            "actions": [{"name": "extend", "args": {"usd": 25}}],
        }
    )
    update = _make_update("give me 25 more")
    ctx = _make_context(stub)
    await chat_mod.handle_message("give me 25 more", update, ctx)

    fake_extend.assert_awaited_once()
    assert ctx.args == ["25"]


@pytest.mark.asyncio
async def test_chat_extend_no_usd_uses_default(db, monkeypatch):
    """LLM may emit `extend` with no `usd` arg ('extend' = default $5).
    The chat dispatcher passes empty args; cmd defaults to 5 itself."""
    fake_extend = AsyncMock()
    monkeypatch.setattr(cmds, "extend_cmd", fake_extend)

    stub = _StubProvider({"reply": "extending.", "actions": [{"name": "extend", "args": {}}]})
    ctx = _make_context(stub)
    await chat_mod.handle_message("extend", _make_update("extend"), ctx)

    fake_extend.assert_awaited_once()
    assert ctx.args == []


@pytest.mark.asyncio
async def test_chat_force_draft_passes_event_id(db, monkeypatch):
    fake_draft = AsyncMock()
    monkeypatch.setattr(cmds, "draft_cmd", fake_draft)

    stub = _StubProvider(
        {
            "reply": "drafting event 42.",
            "actions": [{"name": "force_draft", "args": {"event_id": 42}}],
        }
    )
    ctx = _make_context(stub)
    await chat_mod.handle_message("draft event 42", _make_update("draft event 42"), ctx)

    fake_draft.assert_awaited_once()
    assert ctx.args == ["42"]


@pytest.mark.asyncio
async def test_chat_force_draft_no_id_skipped(db, monkeypatch):
    """If the LLM emits force_draft without an event_id, we skip the
    action rather than pass empty args (which would trigger the cmd's
    'usage' reply confusingly)."""
    fake_draft = AsyncMock()
    monkeypatch.setattr(cmds, "draft_cmd", fake_draft)

    stub = _StubProvider(
        {
            "reply": "tell me which event id and i'll draft it.",
            "actions": [{"name": "force_draft", "args": {}}],
        }
    )
    ctx = _make_context(stub)
    await chat_mod.handle_message("draft something", _make_update("draft something"), ctx)

    fake_draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_approve_draft_routes_to_handler(db, monkeypatch):
    """`approve_draft` is the publish-to-X action. The chat dispatch must
    delegate to `handlers.approve_draft` (which is the inline-keyboard
    code path under a public name) so the conversational and button
    flows post identically."""
    from wire.telegram import handlers as hnd_mod

    fake_approve = AsyncMock()
    monkeypatch.setattr(hnd_mod, "approve_draft", fake_approve)

    stub = _StubProvider(
        {
            "reply": "shipping it.",
            "actions": [{"name": "approve_draft", "args": {"draft_id": 51}}],
        }
    )
    update = _make_update("publish that draft")
    ctx = _make_context(stub)
    await chat_mod.handle_message("publish that draft", update, ctx)

    fake_approve.assert_awaited_once()
    args = fake_approve.await_args.args
    assert args[0] is update
    assert args[1] is ctx
    assert args[2] == 51  # int draft_id


@pytest.mark.asyncio
async def test_chat_approve_draft_coerces_string_id(db, monkeypatch):
    """Some local models emit numbers as strings. The dispatcher coerces."""
    from wire.telegram import handlers as hnd_mod

    fake_approve = AsyncMock()
    monkeypatch.setattr(hnd_mod, "approve_draft", fake_approve)

    stub = _StubProvider(
        {"reply": "ok", "actions": [{"name": "approve_draft", "args": {"draft_id": "51"}}]}
    )
    await chat_mod.handle_message("post 51", _make_update("post 51"), _make_context(stub))

    fake_approve.assert_awaited_once()
    assert fake_approve.await_args.args[2] == 51


@pytest.mark.asyncio
async def test_chat_approve_draft_missing_id_skipped(db, monkeypatch):
    """If the LLM hallucinates an approve action without a draft_id, the
    dispatcher skips it rather than calling the handler with garbage."""
    from wire.telegram import handlers as hnd_mod

    fake_approve = AsyncMock()
    monkeypatch.setattr(hnd_mod, "approve_draft", fake_approve)

    stub = _StubProvider(
        {"reply": "which one?", "actions": [{"name": "approve_draft", "args": {}}]}
    )
    await chat_mod.handle_message("ship it", _make_update("ship it"), _make_context(stub))

    fake_approve.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_reject_draft_routes_with_reason(db, monkeypatch):
    from wire.telegram import handlers as hnd_mod

    fake_reject = AsyncMock()
    monkeypatch.setattr(hnd_mod, "reject_draft", fake_reject)

    stub = _StubProvider(
        {
            "reply": "killed.",
            "actions": [
                {
                    "name": "reject_draft",
                    "args": {"draft_id": 52, "reason": "too internal"},
                }
            ],
        }
    )
    update = _make_update("kill 52, too internal")
    ctx = _make_context(stub)
    await chat_mod.handle_message("kill 52, too internal", update, ctx)

    fake_reject.assert_awaited_once()
    args = fake_reject.await_args.args
    assert args[2] == 52
    assert args[3] == "too internal"


@pytest.mark.asyncio
async def test_chat_reject_draft_default_reason(db, monkeypatch):
    """Reason defaults to 'via_chat' when the LLM omits it — keeps the
    rejected_reason column non-empty for the learning block."""
    from wire.telegram import handlers as hnd_mod

    fake_reject = AsyncMock()
    monkeypatch.setattr(hnd_mod, "reject_draft", fake_reject)

    stub = _StubProvider(
        {"reply": "scrapped.", "actions": [{"name": "reject_draft", "args": {"draft_id": 52}}]}
    )
    await chat_mod.handle_message("scrap that", _make_update("scrap that"), _make_context(stub))

    fake_reject.assert_awaited_once()
    assert fake_reject.await_args.args[3] == "via_chat"


@pytest.mark.asyncio
async def test_chat_edit_draft_routes_with_instruction(db, monkeypatch):
    from wire.telegram import handlers as hnd_mod

    fake_edit = AsyncMock()
    monkeypatch.setattr(hnd_mod, "edit_draft_via_chat", fake_edit)

    stub = _StubProvider(
        {
            "reply": "rewriting.",
            "actions": [
                {
                    "name": "edit_draft",
                    "args": {"draft_id": 51, "instruction": "drop the emoji"},
                }
            ],
        }
    )
    update = _make_update("drop the emoji from 51")
    ctx = _make_context(stub)
    await chat_mod.handle_message("drop the emoji from 51", update, ctx)

    fake_edit.assert_awaited_once()
    args = fake_edit.await_args.args
    assert args[2] == 51
    assert args[3] == "drop the emoji"


@pytest.mark.asyncio
async def test_chat_edit_draft_missing_args_skipped(db, monkeypatch):
    from wire.telegram import handlers as hnd_mod

    fake_edit = AsyncMock()
    monkeypatch.setattr(hnd_mod, "edit_draft_via_chat", fake_edit)

    # Missing instruction
    stub = _StubProvider(
        {"reply": "ok", "actions": [{"name": "edit_draft", "args": {"draft_id": 51}}]}
    )
    await chat_mod.handle_message("revise 51", _make_update("revise 51"), _make_context(stub))
    fake_edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_multiple_actions_all_execute(db, monkeypatch):
    fake_pause = AsyncMock()
    fake_extend = AsyncMock()
    monkeypatch.setattr(cmds, "pause_cmd", fake_pause)
    monkeypatch.setattr(cmds, "extend_cmd", fake_extend)

    stub = _StubProvider(
        {
            "reply": "pausing and extending.",
            "actions": [
                {"name": "extend", "args": {"usd": 5}},
                {"name": "pause", "args": {}},
            ],
        }
    )
    ctx = _make_context(stub)
    await chat_mod.handle_message("extend then pause", _make_update("…"), ctx)

    fake_extend.assert_awaited_once()
    fake_pause.assert_awaited_once()


# --- failure modes ----------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_provider_failure_replies_with_fallback(db):
    class _Failing:
        async def complete(self, **kwargs):
            raise RuntimeError("local model down + claude unreachable")

    update = _make_update("status")
    await chat_mod.handle_message("status", update, _make_context(_Failing()))

    update.effective_message.reply_text.assert_awaited_once()
    text = update.effective_message.reply_text.await_args.args[0]
    assert "/pause" in text or "signal" in text.lower() or "choppy" in text.lower()


@pytest.mark.asyncio
async def test_chat_invalid_json_replies_with_fallback(db):
    class _BadJson:
        async def complete(self, task, system, messages, response_format=None, max_tokens=500):
            return LLMResponse(
                content="not json at all",
                provider="ollama",
                model="m",
                input_tokens=10,
                output_tokens=4,
                cache_read_tokens=0,
                cache_write_tokens=0,
                latency_ms=10,
                cost_usd=0.0,
                task=task,
            )

    update = _make_update("status")
    await chat_mod.handle_message("status", update, _make_context(_BadJson()))

    update.effective_message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_unknown_action_logged_not_crashed(db, monkeypatch):
    """If the LLM emits an action name outside the Literal, pydantic
    validation fails before we dispatch — so we land on the static
    fallback reply rather than calling something we shouldn't."""
    stub = _StubProvider({"reply": "ok", "actions": [{"name": "drop_database", "args": {}}]})
    update = _make_update("hi")
    await chat_mod.handle_message("hi", update, _make_context(stub))

    # The schema-mismatch falls into the parse_failed branch → fallback reply.
    update.effective_message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_no_provider_silent_no_op(db):
    update = _make_update("hi")
    ctx = _make_context(provider=None)
    ctx.bot_data["wire_provider"] = None

    await chat_mod.handle_message("hi", update, ctx)
    update.effective_message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_empty_reply_no_message_sent(db, monkeypatch):
    """Action-only response: LLM returns empty reply, the action's own
    reply (from the cmd) is what the user sees."""
    fake_resume = AsyncMock()
    monkeypatch.setattr(cmds, "resume_cmd", fake_resume)

    stub = _StubProvider({"reply": "", "actions": [{"name": "resume", "args": {}}]})
    update = _make_update("resume")
    ctx = _make_context(stub)

    await chat_mod.handle_message("resume", update, ctx)

    # Empty reply → no top-level Telegram message from the agent.
    update.effective_message.reply_text.assert_not_awaited()
    fake_resume.assert_awaited_once()


# --- single-turn memory -----------------------------------------------------


@pytest.mark.asyncio
async def test_chat_history_carries_one_pair(db):
    """Last user/assistant pair is stored per user_id in bot_data so
    follow-ups like 'yes, do it' resolve."""
    stub = _StubProvider({"reply": "first reply", "actions": []})
    update = _make_update("first message", user_id=42)
    ctx = _make_context(stub)

    await chat_mod.handle_message("first message", update, ctx)

    history = ctx.bot_data["wire_chat_history"][42]
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "first message"
    assert history[1]["role"] == "assistant"
    assert history[1]["content"] == "first reply"


@pytest.mark.asyncio
async def test_chat_history_replaces_not_grows(db):
    """We deliberately keep only the last single pair — not a growing log.
    Second turn evicts the first."""
    stub = _StubProvider({"reply": "second reply", "actions": []})
    update = _make_update("second message", user_id=42)
    ctx = _make_context(stub)
    ctx.bot_data["wire_chat_history"] = {
        42: [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old reply"},
        ]
    }

    await chat_mod.handle_message("second message", update, ctx)

    history = ctx.bot_data["wire_chat_history"][42]
    assert len(history) == 2
    assert history[0]["content"] == "second message"  # not "old"


@pytest.mark.asyncio
async def test_chat_history_passed_to_provider_on_next_turn(db):
    """The previous turn's pair must be sent to the LLM as prior messages
    so a follow-up like 'yes' has context."""
    stub = _StubProvider({"reply": "doing it.", "actions": []})
    update = _make_update("yes do it", user_id=42)
    ctx = _make_context(stub)
    ctx.bot_data["wire_chat_history"] = {
        42: [
            {"role": "user", "content": "should i pause?"},
            {"role": "assistant", "content": "your call. pause for the rest of the day?"},
        ]
    }

    await chat_mod.handle_message("yes do it", update, ctx)

    sent_messages = stub.calls[0]["messages"]
    # The prior pair lands as messages[0] and messages[1].
    assert sent_messages[0]["role"] == "user"
    assert "should i pause?" in sent_messages[0]["content"]
    assert sent_messages[1]["role"] == "assistant"
    # The current turn (with snapshot) lands at the end.
    assert sent_messages[-1]["role"] == "user"
    assert "yes do it" in sent_messages[-1]["content"]

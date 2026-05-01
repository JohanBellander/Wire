"""Step 8 — Telegram handler tests.

Mocks python-telegram-bot's Update + Context. Verifies the state machine,
the diff format, the saved-draft expiry sweep, and the rendering helpers.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from wire.db import session as db_session
from wire.db.models import Base, Decision, Draft, Event, Post, Session, utc_now
from wire.telegram import bot as bot_mod
from wire.telegram import commands as cmds
from wire.telegram.bot import render_thread_for_telegram
from wire.telegram.handlers import (
    _diff_opcodes,
    _on_approve,
    _on_reject_reason,
    callback_handler,
    expire_old_saved_drafts,
    text_message_handler,
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


def _make_update_with_callback(
    callback_data: str, user_id: int = 42, chat_id: int = 1
) -> MagicMock:
    update = MagicMock()
    update.callback_query.data = callback_data
    update.callback_query.answer = AsyncMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.effective_message.reply_text = AsyncMock()
    return update


def _make_update_with_text(text: str, user_id: int = 42, chat_id: int = 1) -> MagicMock:
    update = MagicMock()
    update.callback_query = None
    update.message.text = text
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.effective_message.reply_text = AsyncMock()
    return update


def _make_context(twitter=None) -> MagicMock:
    ctx = MagicMock()
    ctx.bot_data = {"wire_chat_id": 1, "wire_twitter": twitter}
    ctx.bot.send_message = AsyncMock()
    return ctx


# ---------------- diff -------------------------------------------------------


def test_diff_opcodes_simple():
    diff = _diff_opcodes("hello world", "hello brave world")
    assert "opcodes" in diff
    assert diff["before_len"] == 11
    assert diff["after_len"] == 17
    # Should have at least one insert opcode
    tags = {op["tag"] for op in diff["opcodes"]}
    assert "insert" in tags or "replace" in tags
    # Round-trippable as JSON
    json.dumps(diff)


# ---------------- thread rendering ------------------------------------------


def test_render_single_tweet_unchanged():
    assert render_thread_for_telegram("just one tweet") == "just one tweet"


def test_render_thread_numbers_blocks():
    text = "first part\n---\nsecond part\n---\nthird part"
    rendered = render_thread_for_telegram(text)
    assert "1/3" in rendered
    assert "2/3" in rendered
    assert "3/3" in rendered
    assert "first part" in rendered


# ---------------- approve flow ----------------------------------------------


@pytest.mark.asyncio
async def test_approve_calls_twitter_and_records(db):
    with db.session_scope() as sa:
        d = Draft(text="ship it")
        sa.add(d)
        sa.flush()
        did = d.id

    fake_twitter = MagicMock()
    post_result = MagicMock()
    post_result.tweet_id = "tw-123"
    post_result.posted_text = "ship it"
    post_result.url = "https://x.com/user/status/tw-123"
    fake_twitter.post = AsyncMock(return_value=post_result)

    update = _make_update_with_callback(f"approve:{did}")
    ctx = _make_context(twitter=fake_twitter)
    await _on_approve(update, ctx, did)

    fake_twitter.post.assert_awaited_once_with("ship it")
    update.effective_message.reply_text.assert_awaited()

    with db.session_scope() as sa:
        d = sa.get(Draft, did)
        assert d.status == "approved"
        decisions = sa.query(Decision).filter_by(draft_id=did).all()
        assert len(decisions) == 1
        assert decisions[0].decision == "approved"
        posts = sa.query(Post).filter_by(draft_id=did).all()
        assert len(posts) == 1
        assert posts[0].twitter_id == "tw-123"


# ---------------- reject reason flow ----------------------------------------


@pytest.mark.asyncio
async def test_reject_reason_button_records_decision(db):
    with db.session_scope() as sa:
        d = Draft(text="boring fix")
        sa.add(d)
        sa.flush()
        did = d.id
    update = _make_update_with_callback(f"reject_reason:{did}:boring")
    ctx = _make_context()
    await _on_reject_reason(update, ctx, did, "boring")

    with db.session_scope() as sa:
        d = sa.get(Draft, did)
        assert d.status == "rejected"
        dec = sa.query(Decision).filter_by(draft_id=did).one()
        assert dec.decision == "rejected"
        assert dec.reject_reason == "boring"


@pytest.mark.asyncio
async def test_reject_reason_other_enters_state(db):
    with db.session_scope() as sa:
        d = Draft(text="hmm")
        sa.add(d)
        sa.flush()
        did = d.id

    update = _make_update_with_callback(f"reject_reason:{did}:other", user_id=99)
    ctx = _make_context()
    await _on_reject_reason(update, ctx, did, "other")
    # State should now be set
    state = ctx.bot_data["wire_pending_state"][99]
    assert state[0] == "reject_other"
    assert state[1] == did

    # Send a follow-up text reply
    text_update = _make_update_with_text("too generic", user_id=99)
    await text_message_handler(text_update, ctx)

    with db.session_scope() as sa:
        dec = sa.query(Decision).filter_by(draft_id=did).one()
        assert dec.decision == "rejected"
        assert dec.reject_reason.startswith("other:")
        assert "too generic" in dec.reject_reason


# ---------------- edit flow --------------------------------------------------


@pytest.mark.asyncio
async def test_edit_flow_records_diff_and_posts(db):
    with db.session_scope() as sa:
        d = Draft(text="original wording")
        sa.add(d)
        sa.flush()
        did = d.id

    fake_twitter = MagicMock()
    post_result = MagicMock()
    post_result.tweet_id = "tw-9"
    post_result.posted_text = "edited wording"
    post_result.url = "https://x.com/user/status/tw-9"
    fake_twitter.post = AsyncMock(return_value=post_result)

    ctx = _make_context(twitter=fake_twitter)
    # Simulate /edit click → state set
    ctx.bot_data["wire_pending_state"] = {77: ("edit", did, 9999999999.0)}
    text_update = _make_update_with_text("edited wording", user_id=77)
    await text_message_handler(text_update, ctx)

    fake_twitter.post.assert_awaited_once_with("edited wording")
    with db.session_scope() as sa:
        d = sa.get(Draft, did)
        assert d.status == "edited"
        dec = sa.query(Decision).filter_by(draft_id=did).one()
        assert dec.decision == "edited"
        assert dec.edited_text == "edited wording"
        assert dec.edit_diff is not None
        # Diff must be valid JSON
        diff = json.loads(dec.edit_diff)
        assert diff["before_len"] == len("original wording")
        assert diff["after_len"] == len("edited wording")


# ---------------- expiry sweep -----------------------------------------------


def test_expire_old_saved_drafts(db):
    now = utc_now()
    with db.session_scope() as sa:
        old = Draft(text="too old", status="pending")
        old.created_at = now - timedelta(hours=48)
        recent = Draft(text="fresh", status="pending")
        recent.created_at = now - timedelta(hours=1)
        sa.add_all([old, recent])
        sa.flush()
        old_id, recent_id = old.id, recent.id

    n = expire_old_saved_drafts(max_age_hours=24)
    assert n == 1
    with db.session_scope() as sa:
        assert sa.get(Draft, old_id).status == "expired"
        assert sa.get(Draft, recent_id).status == "pending"


# ---------------- callback router unknowns -----------------------------------


@pytest.mark.asyncio
async def test_unknown_callback_does_not_crash(db):
    update = _make_update_with_callback("totally:unknown:thing")
    ctx = _make_context()
    await callback_handler(update, ctx)  # should just no-op


# ---------------- /last + /draft ---------------------------------------------


def _make_command_update(args: list[str], chat_id: int = 1, user_id: int = 42) -> MagicMock:
    update = MagicMock()
    update.callback_query = None
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.effective_message.reply_text = AsyncMock()
    return update


def _make_command_context(args: list[str], provider=None) -> MagicMock:
    ctx = MagicMock()
    ctx.args = args
    # /last only needs wire_chat_id; /draft also uses wire_config / wire_repos / wire_provider.
    ctx.bot_data = {
        "wire_chat_id": 1,
        "wire_config": MagicMock(),
        "wire_repos": MagicMock(),
        "wire_provider": provider,
    }
    return ctx


def _seed_event_with_outcome(db_, *, github_id: str, occurred_at: datetime, **kw) -> int:
    """Insert one Event for the /last outcome tests. kw lets the caller wire
    its own session_id, drafts, etc."""
    with db_.session_scope() as sa:
        e = Event(
            github_id=github_id,
            repo="winetrackr",
            event_type="PushEvent",
            actor="me",
            payload={"raw_payload": {"commits": [{"message": "feat: x"}]}},
            occurred_at=occurred_at,
            triage_score=kw.get("triage_score", 0.5),
            triage_reason="seed",
            session_id=kw.get("session_id"),
        )
        sa.add(e)
        sa.flush()
        return e.id


@pytest.mark.asyncio
async def test_last_cmd_renders_all_outcome_buckets(db):
    base = datetime(2026, 4, 29, 12, 0, 0)
    with db.session_scope() as sa:
        # session-1: drafted with a Draft row (outcome 1: drafted #N (status))
        s1 = Session(
            repo="winetrackr",
            started_at=base,
            ended_at=base + timedelta(minutes=5),
            closed_reason="idle",
            drafted_at=base + timedelta(minutes=10),
        )
        sa.add(s1)
        sa.flush()
        d1 = Draft(session_id=s1.id, text="draft text", status="pending")
        sa.add(d1)
        sa.flush()
        s1_id, d1_id = s1.id, d1.id

        # session-2: drafted_at set, no drafts, skip_reason populated (outcome 2)
        s2 = Session(
            repo="winetrackr",
            started_at=base,
            ended_at=base + timedelta(minutes=5),
            closed_reason="idle",
            drafted_at=base + timedelta(minutes=10),
            skip_reason="too internal",
        )
        sa.add(s2)
        sa.flush()
        s2_id = s2.id

        # session-3: drafted_at set, no drafts, no skip_reason (outcome 3)
        s3 = Session(
            repo="winetrackr",
            started_at=base,
            ended_at=base + timedelta(minutes=5),
            closed_reason="idle",
            drafted_at=base + timedelta(minutes=10),
            skip_reason=None,
        )
        sa.add(s3)
        sa.flush()
        s3_id = s3.id

        # session-4: open / not closed yet (outcome 4: pending session close)
        s4 = Session(repo="winetrackr", started_at=base, ended_at=None)
        sa.add(s4)
        sa.flush()
        s4_id = s4.id

    # 5 events, one per outcome bucket. occurred_at descending → e5, e4, e3, e2, e1.
    e1 = _seed_event_with_outcome(
        db, github_id="ev-1", occurred_at=base, session_id=s1_id, triage_score=0.62
    )
    e2 = _seed_event_with_outcome(
        db,
        github_id="ev-2",
        occurred_at=base + timedelta(minutes=1),
        session_id=s2_id,
        triage_score=0.45,
    )
    e3 = _seed_event_with_outcome(
        db,
        github_id="ev-3",
        occurred_at=base + timedelta(minutes=2),
        session_id=s3_id,
        triage_score=0.18,
    )
    e4 = _seed_event_with_outcome(
        db,
        github_id="ev-4",
        occurred_at=base + timedelta(minutes=3),
        session_id=s4_id,
        triage_score=0.55,
    )
    e5 = _seed_event_with_outcome(
        db,
        github_id="ev-5",
        occurred_at=base + timedelta(minutes=4),
        session_id=None,
        triage_score=0.30,
    )

    update = _make_command_update(args=[])
    ctx = _make_command_context(args=["10"])
    await cmds.last_cmd(update, ctx)

    update.effective_message.reply_text.assert_awaited()
    text = update.effective_message.reply_text.await_args.args[0]
    # All outcome strings present
    assert f"drafted #{d1_id} (pending)" in text
    assert "LLM said skip: too internal" in text
    assert "below-threshold skip" in text
    assert "pending session close" in text
    assert "no session" in text
    # Ordered by occurred_at DESC: e5 line appears before e1 line
    assert text.index(f"[{e5}]") < text.index(f"[{e1}]")
    # All 5 ids referenced
    for eid in (e1, e2, e3, e4, e5):
        assert f"[{eid}]" in text


@pytest.mark.asyncio
async def test_last_cmd_default_n_is_5(db):
    base = datetime(2026, 4, 29, 12, 0, 0)
    for i in range(10):
        _seed_event_with_outcome(
            db,
            github_id=f"ev-{i}",
            occurred_at=base + timedelta(minutes=i),
            session_id=None,
        )
    update = _make_command_update(args=[])
    ctx = _make_command_context(args=[])  # no args
    await cmds.last_cmd(update, ctx)

    text = update.effective_message.reply_text.await_args.args[0]
    # Header line + 5 event lines = 6 lines
    assert text.startswith("🕓 last 5 events")
    assert text.count("\n[") == 5  # event lines start with "[id]"


@pytest.mark.asyncio
async def test_last_cmd_n_clamped_to_50(db):
    base = datetime(2026, 4, 29, 12, 0, 0)
    for i in range(70):
        _seed_event_with_outcome(
            db,
            github_id=f"ev-{i}",
            occurred_at=base + timedelta(minutes=i),
            session_id=None,
        )
    update = _make_command_update(args=[])
    ctx = _make_command_context(args=["200"])
    await cmds.last_cmd(update, ctx)

    text = update.effective_message.reply_text.await_args.args[0]
    assert text.startswith("🕓 last 50 events")
    assert text.count("\n[") == 50


@pytest.mark.asyncio
async def test_last_cmd_invalid_arg_replies_usage(db):
    update = _make_command_update(args=[])
    ctx = _make_command_context(args=["banana"])
    await cmds.last_cmd(update, ctx)
    text = update.effective_message.reply_text.await_args.args[0]
    assert "usage" in text.lower()


@pytest.mark.asyncio
async def test_draft_cmd_invokes_force_draft_and_send_draft(db, monkeypatch):
    fake_force = AsyncMock(return_value=(99, None))
    monkeypatch.setattr(cmds, "force_draft_for_event", fake_force)
    fake_send = AsyncMock(return_value=12345)
    monkeypatch.setattr(bot_mod, "send_draft", fake_send)

    update = _make_command_update(args=[])
    ctx = _make_command_context(args=["7"], provider=MagicMock())
    ctx.application = MagicMock()
    await cmds.draft_cmd(update, ctx)

    fake_force.assert_awaited_once()
    args = fake_force.await_args.args
    assert args[0] == 7  # event_id
    fake_send.assert_awaited_once_with(ctx.application, 99)
    text = update.effective_message.reply_text.await_args.args[0]
    assert "✅" in text
    assert "draft #99" in text
    assert "event #7" in text


@pytest.mark.asyncio
async def test_draft_cmd_skip_reason_path(db, monkeypatch):
    monkeypatch.setattr(cmds, "force_draft_for_event", AsyncMock(return_value=(None, "boring")))
    update = _make_command_update(args=[])
    ctx = _make_command_context(args=["3"], provider=MagicMock())
    await cmds.draft_cmd(update, ctx)

    text = update.effective_message.reply_text.await_args.args[0]
    assert "skip" in text.lower()
    assert "boring" in text


@pytest.mark.asyncio
async def test_draft_cmd_missing_arg(db):
    update = _make_command_update(args=[])
    ctx = _make_command_context(args=[], provider=MagicMock())
    await cmds.draft_cmd(update, ctx)
    text = update.effective_message.reply_text.await_args.args[0]
    assert "usage" in text.lower()


@pytest.mark.asyncio
async def test_draft_cmd_non_int_arg(db):
    update = _make_command_update(args=[])
    ctx = _make_command_context(args=["abc"], provider=MagicMock())
    await cmds.draft_cmd(update, ctx)
    text = update.effective_message.reply_text.await_args.args[0]
    assert "usage" in text.lower()


@pytest.mark.asyncio
async def test_draft_cmd_event_not_found(db, monkeypatch):
    from wire.drafting.drafter import EventNotFoundError

    monkeypatch.setattr(
        cmds,
        "force_draft_for_event",
        AsyncMock(side_effect=EventNotFoundError("nope")),
    )
    update = _make_command_update(args=[])
    ctx = _make_command_context(args=["404"], provider=MagicMock())
    await cmds.draft_cmd(update, ctx)
    text = update.effective_message.reply_text.await_args.args[0]
    assert "not found" in text
    assert "#404" in text


@pytest.mark.asyncio
async def test_draft_cmd_budget_paused(db, monkeypatch):
    from wire.drafting.drafter import BudgetPausedError

    monkeypatch.setattr(
        cmds,
        "force_draft_for_event",
        AsyncMock(side_effect=BudgetPausedError("month spend $11.00 / cap $10.00")),
    )
    update = _make_command_update(args=[])
    ctx = _make_command_context(args=["1"], provider=MagicMock())
    await cmds.draft_cmd(update, ctx)
    text = update.effective_message.reply_text.await_args.args[0]
    assert "cap" in text.lower()
    assert "/extend" in text

"""Step 8 — Telegram handler tests.

Mocks python-telegram-bot's Update + Context. Verifies the state machine,
the diff format, the saved-draft expiry sweep, and the rendering helpers.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from wire.db import session as db_session
from wire.db.models import Base, Decision, Draft, Post, Session, utc_now
from wire.telegram.bot import render_thread_for_telegram
from wire.telegram.handlers import (
    _commit_edit,
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


def _make_update_with_callback(callback_data: str, user_id: int = 42, chat_id: int = 1) -> MagicMock:
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
        d = Draft(text="ship it"); sa.add(d); sa.flush()
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
        d = Draft(text="boring fix"); sa.add(d); sa.flush()
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
        d = Draft(text="hmm"); sa.add(d); sa.flush()
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
        d = Draft(text="original wording"); sa.add(d); sa.flush()
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
        sa.add_all([old, recent]); sa.flush()
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

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


# ---------------- NL edit (revision) flow ------------------------------------


def _make_edit_context(*, revised_text: str, twitter=None, repo: str = "winetrackr") -> MagicMock:
    """Context wired for the NL edit flow. Stubs the LLM provider so
    `revise_draft` returns `revised_text`, and skips the actual Telegram
    re-send by stubbing `send_draft`."""
    ctx = MagicMock()
    ctx.bot_data = {
        "wire_chat_id": 1,
        "wire_twitter": twitter,
        "wire_config": MagicMock(),
        "wire_provider": MagicMock(),
        "wire_repos": MagicMock(),
    }
    ctx.bot.send_message = AsyncMock()
    ctx.application = MagicMock()
    return ctx


@pytest.mark.asyncio
async def test_edit_revision_replaces_text_and_lazy_fills_original(db, monkeypatch):
    """First revision: original_text is NULL → gets filled with current text;
    text is replaced with the LLM's revised version. No post happens here —
    user must explicitly approve afterwards."""
    with db.session_scope() as sa:
        d = Draft(text="original wording")
        sa.add(d)
        sa.flush()
        did = d.id

    from wire.drafting import drafter as drafter_mod
    from wire.telegram import bot as bot_mod

    fake_revise = AsyncMock(return_value="revised wording (shorter)")
    monkeypatch.setattr(drafter_mod, "revise_draft", fake_revise)
    fake_send = AsyncMock(return_value=12345)
    monkeypatch.setattr(bot_mod, "send_draft", fake_send)

    ctx = _make_edit_context(revised_text="revised wording (shorter)")
    ctx.bot_data["wire_pending_state"] = {77: ("edit", did, 9999999999.0)}
    text_update = _make_update_with_text("shorter", user_id=77)
    await text_message_handler(text_update, ctx)

    fake_revise.assert_awaited_once()
    # First positional arg = current text
    assert fake_revise.await_args.args[0] == "original wording"
    # Second positional arg = the user instruction
    assert fake_revise.await_args.args[1] == "shorter"

    with db.session_scope() as sa:
        d = sa.get(Draft, did)
        assert d.text == "revised wording (shorter)"
        assert d.original_text == "original wording"
        # Status stays pending — the user hasn't approved yet.
        assert d.status == "pending"
        # No Decision row yet either.
        assert sa.query(Decision).filter_by(draft_id=did).count() == 0

    # Edit-state was popped, fresh draft was re-sent.
    assert ctx.bot_data.get("wire_pending_state", {}).get(77) is None
    fake_send.assert_awaited_once()


@pytest.mark.asyncio
async def test_edit_revision_iterates_keeps_first_original(db, monkeypatch):
    """Second revision on the same draft must NOT overwrite original_text —
    that field is for the first LLM draft only, so the learning block sees
    'original → final' rather than 'previous-revision → final'."""
    with db.session_scope() as sa:
        d = Draft(text="LLM first try")
        sa.add(d)
        sa.flush()
        did = d.id

    from wire.drafting import drafter as drafter_mod
    from wire.telegram import bot as bot_mod

    revisions = ["second attempt", "third attempt"]
    fake_revise = AsyncMock(side_effect=revisions)
    monkeypatch.setattr(drafter_mod, "revise_draft", fake_revise)
    monkeypatch.setattr(bot_mod, "send_draft", AsyncMock(return_value=1))

    ctx = _make_edit_context(revised_text=revisions[0])
    # First revision
    ctx.bot_data["wire_pending_state"] = {77: ("edit", did, 9999999999.0)}
    await text_message_handler(_make_update_with_text("shorter", user_id=77), ctx)
    # Second revision (user taps Edit again)
    ctx.bot_data["wire_pending_state"] = {77: ("edit", did, 9999999999.0)}
    await text_message_handler(_make_update_with_text("drop the emoji", user_id=77), ctx)

    with db.session_scope() as sa:
        d = sa.get(Draft, did)
        assert d.text == "third attempt"
        # original_text is still the very first LLM draft.
        assert d.original_text == "LLM first try"


@pytest.mark.asyncio
async def test_edit_revision_failure_surfaces_error_keeps_state(db, monkeypatch):
    with db.session_scope() as sa:
        d = Draft(text="something")
        sa.add(d)
        sa.flush()
        did = d.id

    from wire.drafting import drafter as drafter_mod

    monkeypatch.setattr(
        drafter_mod, "revise_draft", AsyncMock(side_effect=RuntimeError("provider died"))
    )

    ctx = _make_edit_context(revised_text="")
    ctx.bot_data["wire_pending_state"] = {77: ("edit", did, 9999999999.0)}
    text_update = _make_update_with_text("less hype", user_id=77)
    await text_message_handler(text_update, ctx)

    text_update.effective_message.reply_text.assert_awaited()
    reply = text_update.effective_message.reply_text.await_args.args[0]
    assert "provider died" in reply
    # State preserved so the user can retry without re-tapping Edit.
    state = ctx.bot_data["wire_pending_state"].get(77)
    assert state is not None
    assert state[0] == "edit"

    # Draft text untouched.
    with db.session_scope() as sa:
        d = sa.get(Draft, did)
        assert d.text == "something"
        assert d.original_text is None


@pytest.mark.asyncio
async def test_approve_revised_draft_records_edited_decision(db, monkeypatch):
    """Once a draft has a non-NULL `original_text` (i.e. the user revised
    it through NL), approving it records decision='edited' with the diff
    rather than a plain 'approved'."""
    with db.session_scope() as sa:
        d = Draft(text="final revised text", original_text="LLM first try")
        sa.add(d)
        sa.flush()
        did = d.id

    fake_twitter = MagicMock()
    post_result = MagicMock()
    post_result.tweet_id = "tw-1"
    post_result.posted_text = "final revised text"
    post_result.url = "https://x.com/u/status/1"
    fake_twitter.post = AsyncMock(return_value=post_result)

    ctx = _make_context(twitter=fake_twitter)
    update = _make_update_with_callback(f"approve:{did}")
    await _on_approve(update, ctx, did)

    fake_twitter.post.assert_awaited_once_with("final revised text")
    with db.session_scope() as sa:
        d = sa.get(Draft, did)
        assert d.status == "edited"
        dec = sa.query(Decision).filter_by(draft_id=did).one()
        assert dec.decision == "edited"
        assert dec.edited_text == "final revised text"
        # Diff is JSON-encoded and references both lengths.
        diff = json.loads(dec.edit_diff)
        assert diff["before_len"] == len("LLM first try")
        assert diff["after_len"] == len("final revised text")


@pytest.mark.asyncio
async def test_approve_unrevised_draft_still_records_approved(db):
    """Regression: a draft that was never revised (original_text NULL) still
    records a plain decision='approved'."""
    with db.session_scope() as sa:
        d = Draft(text="straight from the LLM")
        sa.add(d)
        sa.flush()
        did = d.id

    fake_twitter = MagicMock()
    post_result = MagicMock()
    post_result.tweet_id = "tw-2"
    post_result.posted_text = "straight from the LLM"
    post_result.url = "https://x.com/u/status/2"
    fake_twitter.post = AsyncMock(return_value=post_result)

    ctx = _make_context(twitter=fake_twitter)
    update = _make_update_with_callback(f"approve:{did}")
    await _on_approve(update, ctx, did)

    with db.session_scope() as sa:
        d = sa.get(Draft, did)
        assert d.status == "approved"
        dec = sa.query(Decision).filter_by(draft_id=did).one()
        assert dec.decision == "approved"
        assert dec.edited_text is None


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


# ---------------- NL dispatch via text_message_handler ----------------------


def _make_nl_context(twitter=None) -> MagicMock:
    """Like _make_context but also wires wire_config + wire_provider so the
    NL classifier can run."""
    ctx = MagicMock()
    ctx.bot_data = {
        "wire_chat_id": 1,
        "wire_twitter": twitter,
        "wire_config": MagicMock(),
        "wire_provider": MagicMock(),
        "wire_repos": MagicMock(),
    }
    ctx.bot.send_message = AsyncMock()
    return ctx


def _stub_classify(monkeypatch, intent: str, args: dict | None = None) -> None:
    """Patch `intent.classify` to return a canned ClassifiedIntent without
    hitting any LLM."""
    from wire.telegram import handlers as hnd_mod
    from wire.telegram.intent import ClassifiedIntent

    async def _fake(text, cfg, provider):
        return ClassifiedIntent(intent=intent, args=args or {}, confidence=0.9)

    monkeypatch.setattr(hnd_mod.intent_mod, "classify", _fake)


@pytest.mark.asyncio
async def test_nl_dispatch_status_calls_status_cmd(db, monkeypatch):
    _stub_classify(monkeypatch, "status")
    fake_status = AsyncMock()
    monkeypatch.setattr(cmds, "status_cmd", fake_status)

    update = _make_update_with_text("how are you doing")
    ctx = _make_nl_context()
    await text_message_handler(update, ctx)

    fake_status.assert_awaited_once()
    # context.args is the empty list for arg-less intents.
    assert ctx.args == []


@pytest.mark.asyncio
async def test_nl_dispatch_extend_passes_amount_arg(db, monkeypatch):
    _stub_classify(monkeypatch, "extend", args={"usd": 25})
    fake_extend = AsyncMock()
    monkeypatch.setattr(cmds, "extend_cmd", fake_extend)

    update = _make_update_with_text("extend by 25")
    ctx = _make_nl_context()
    await text_message_handler(update, ctx)

    fake_extend.assert_awaited_once()
    assert ctx.args == ["25"]


@pytest.mark.asyncio
async def test_nl_dispatch_last_passes_n_arg(db, monkeypatch):
    _stub_classify(monkeypatch, "last", args={"n": 15})
    fake_last = AsyncMock()
    monkeypatch.setattr(cmds, "last_cmd", fake_last)

    update = _make_update_with_text("show me last 15 events")
    ctx = _make_nl_context()
    await text_message_handler(update, ctx)

    fake_last.assert_awaited_once()
    assert ctx.args == ["15"]


@pytest.mark.asyncio
async def test_nl_dispatch_draft_requires_event_id(db, monkeypatch):
    """draft intent without an event_id arg falls through to intent_unknown
    rather than calling draft_cmd with empty args."""
    _stub_classify(monkeypatch, "draft", args={})
    fake_draft = AsyncMock()
    monkeypatch.setattr(cmds, "draft_cmd", fake_draft)

    update = _make_update_with_text("force a draft")
    ctx = _make_nl_context()
    await text_message_handler(update, ctx)

    fake_draft.assert_not_awaited()
    update.effective_message.reply_text.assert_awaited()
    reply = update.effective_message.reply_text.await_args.args[0]
    assert "/help" in reply or "?" in reply or "didn't" in reply.lower() or "noisy" in reply.lower()


@pytest.mark.asyncio
async def test_nl_dispatch_unknown_replies_with_fallback(db, monkeypatch):
    _stub_classify(monkeypatch, "unknown")
    update = _make_update_with_text("hi wire what time is it")
    ctx = _make_nl_context()
    await text_message_handler(update, ctx)

    update.effective_message.reply_text.assert_awaited()


@pytest.mark.asyncio
async def test_state_machine_wins_over_nl_dispatch(db, monkeypatch):
    """If the user is mid-edit, their next text is the edit instruction —
    not an intent. The classifier must NOT be called."""
    with db.session_scope() as sa:
        d = Draft(text="original")
        sa.add(d)
        sa.flush()
        did = d.id

    fake_twitter = MagicMock()
    post_result = MagicMock()
    post_result.tweet_id = "tw-9"
    post_result.posted_text = "rewritten"
    post_result.url = "https://x.com/user/status/tw-9"
    fake_twitter.post = AsyncMock(return_value=post_result)

    classify_called = False

    async def _spy_classify(text, cfg, provider):
        nonlocal classify_called
        classify_called = True
        from wire.telegram.intent import ClassifiedIntent

        return ClassifiedIntent(intent="status", args={}, confidence=1.0)

    from wire.telegram import handlers as hnd_mod

    monkeypatch.setattr(hnd_mod.intent_mod, "classify", _spy_classify)

    ctx = _make_nl_context(twitter=fake_twitter)
    ctx.bot_data["wire_pending_state"] = {77: ("edit", did, 9999999999.0)}
    update = _make_update_with_text("rewritten", user_id=77)
    await text_message_handler(update, ctx)

    # State machine intercepted — classifier never ran.
    assert classify_called is False


@pytest.mark.asyncio
async def test_nl_dispatch_ignores_other_chats(db, monkeypatch):
    """The NL handler must not act on text from chats outside the
    configured one — even if the message looks like a valid intent."""
    classify_called = False

    async def _spy(text, cfg, provider):
        nonlocal classify_called
        classify_called = True
        from wire.telegram.intent import ClassifiedIntent

        return ClassifiedIntent(intent="status", args={}, confidence=1.0)

    from wire.telegram import handlers as hnd_mod

    monkeypatch.setattr(hnd_mod.intent_mod, "classify", _spy)

    ctx = _make_nl_context()
    update = _make_update_with_text("status", chat_id=999)  # wrong chat id
    await text_message_handler(update, ctx)

    assert classify_called is False
    update.effective_message.reply_text.assert_not_awaited()

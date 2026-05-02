"""Telegram bot wiring.

Builds the python-telegram-bot Application, registers handlers + slash
commands, and exposes async start/stop hooks for main.py to drive.

Sending drafts is in `bot.send_draft(...)`; the inline-keyboard click handlers
are in `handlers.py`.
"""

from __future__ import annotations

import os

import structlog
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from wire.config import ReposFile, WireConfig
from wire.db import session as db_session
from wire.db.models import Draft, Session
from wire.telegram import commands as cmds
from wire.telegram import handlers as hnd
from wire.telegram import persona as persona_mod
from wire.telegram.voice import say
from wire.util.repo_names import display_name_for

log = structlog.get_logger()


# --- token / chat helpers ----------------------------------------------------


def _bot_token(cfg: WireConfig) -> str:
    val = os.environ.get(cfg.telegram.bot_token_env)
    if not val:
        raise RuntimeError(f"Telegram bot token env var {cfg.telegram.bot_token_env} is empty")
    return val


def _chat_id(cfg: WireConfig) -> int:
    val = os.environ.get(cfg.telegram.chat_id_env)
    if not val:
        raise RuntimeError(f"Telegram chat id env var {cfg.telegram.chat_id_env} is empty")
    return int(val)


# --- builder -----------------------------------------------------------------


def build_application(
    cfg: WireConfig,
    repos: ReposFile,
    *,
    twitter_poster=None,  # injected; type wire.twitter.client.TwitterClient
    provider=None,  # injected; type wire.llm.provider.LLMProvider
) -> Application:
    app = Application.builder().token(_bot_token(cfg)).build()

    # Stash shared deps on bot_data so handlers can reach them.
    app.bot_data["wire_config"] = cfg
    app.bot_data["wire_repos"] = repos
    app.bot_data["wire_chat_id"] = _chat_id(cfg)
    app.bot_data["wire_twitter"] = twitter_poster
    app.bot_data["wire_provider"] = provider

    # Surface the twitter-wiring state at boot so logs alone tell us whether
    # an approve click would reach X or fall into the dry-run branch.
    log.info(
        "wire.telegram.bot_built",
        twitter_wired=twitter_poster is not None,
        provider_wired=provider is not None,
    )

    # Only safety-critical slash commands stay as slashes — /pause is an
    # always-available kill switch and /help is the discoverable cheat-sheet.
    # Everything else (status, budget, resume, saved, digest, repos, extend,
    # last, draft) routes through `text_message_handler`'s NL intent classifier.
    app.add_handler(CommandHandler("pause", cmds.pause_cmd))
    app.add_handler(CommandHandler("help", cmds.help_cmd))
    app.add_handler(CommandHandler("start", cmds.help_cmd))

    # Inline keyboard callbacks: approve/edit/reject/save + reject reasons
    app.add_handler(CallbackQueryHandler(hnd.callback_handler))

    # Free-text replies — state machine (edit / reject_other) wins, otherwise
    # NL intent classification + dispatch.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, hnd.text_message_handler))

    return app


# --- send a draft ------------------------------------------------------------


def _draft_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Post", callback_data=f"approve:{draft_id}"),
                InlineKeyboardButton("✏️ Edit", callback_data=f"edit:{draft_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject:{draft_id}"),
                InlineKeyboardButton("💤 Save", callback_data=f"save:{draft_id}"),
            ]
        ]
    )


def reject_reason_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Boring", callback_data=f"reject_reason:{draft_id}:boring"),
                InlineKeyboardButton(
                    "Wrong tone", callback_data=f"reject_reason:{draft_id}:wrong_tone"
                ),
            ],
            [
                InlineKeyboardButton(
                    "Too internal", callback_data=f"reject_reason:{draft_id}:too_internal"
                ),
                InlineKeyboardButton(
                    "Already covered", callback_data=f"reject_reason:{draft_id}:already_covered"
                ),
            ],
            [
                InlineKeyboardButton(
                    "Other (free text reply)", callback_data=f"reject_reason:{draft_id}:other"
                ),
            ],
        ]
    )


def render_thread_for_telegram(text: str) -> str:
    """Threads use '\\n---\\n' separators inside `text`. Render numbered
    blocks for human review. Single tweets pass through unchanged."""
    if "\n---\n" not in text:
        return text
    parts = [p.strip() for p in text.split("\n---\n") if p.strip()]
    n = len(parts)
    return "\n\n".join(f"{i + 1}/{n}\n{p}" for i, p in enumerate(parts))


async def send_draft(app: Application, draft_id: int) -> int:
    """Send one draft to the configured chat. Returns the telegram_message_id
    so we can update the row."""
    chat_id: int = app.bot_data["wire_chat_id"]
    repos: ReposFile | None = app.bot_data.get("wire_repos")

    with db_session.session_scope() as sa:
        d = sa.get(Draft, draft_id)
        if d is None:
            raise ValueError(f"Draft {draft_id} not found")
        repo_raw = "?"
        if d.session_id is not None:
            sess = sa.get(Session, d.session_id)
            if sess is not None:
                repo_raw = sess.repo
        text = d.text
        reasoning = d.reasoning or ""

    repo = display_name_for(repo_raw, repos) if repo_raw != "?" else "?"
    rendered = render_thread_for_telegram(text)
    header = say("draft_header", draft_id=draft_id, repo=repo)

    # Persona intro is best-effort: if the bot wasn't wired with a full
    # config / provider (some tests skip this), fall through to no intro.
    cfg: WireConfig | None = app.bot_data.get("wire_config")
    provider = app.bot_data.get("wire_provider")
    intro = (
        await persona_mod.intro_for_draft(cfg, provider, thread_text=rendered, repo=repo)
        if cfg is not None
        else None
    )

    parts: list[str] = []
    if intro:
        parts.append(intro)
    parts.append(header)
    parts.append("")
    parts.append(rendered)
    if reasoning:
        parts.append("")
        parts.append(f"reasoning: {reasoning}")
    body = "\n".join(parts)

    msg = await app.bot.send_message(
        chat_id=chat_id,
        text=body,
        reply_markup=_draft_keyboard(draft_id),
    )

    with db_session.session_scope() as sa:
        d = sa.get(Draft, draft_id)
        if d is not None:
            d.telegram_message_id = msg.message_id

    log.info("wire.telegram.draft_sent", draft_id=draft_id, message_id=msg.message_id)
    return msg.message_id


async def send_pending_drafts_after_quiet(app: Application) -> int:
    """Post-quiet-hours sweep: send any pending drafts that don't yet have a
    telegram_message_id (i.e. they were generated before quiet hours and held
    back). Per SPEC §7.3 — drafts generated earlier still get sent the moment
    quiet hours end."""
    from sqlalchemy import select

    sent = 0
    with db_session.session_scope() as sa:
        rows = (
            sa.execute(
                select(Draft)
                .where(Draft.status == "pending")
                .where(Draft.telegram_message_id.is_(None))
            )
            .scalars()
            .all()
        )
        ids = [r.id for r in rows]
    for did in ids:
        await send_draft(app, did)
        sent += 1
    return sent

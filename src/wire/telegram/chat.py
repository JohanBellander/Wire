"""Conversational chat agent for Wire's Telegram surface.

Replaces the fixed-intent classifier with a tool-using chat: every
free-text message gets a fresh state snapshot, goes through the
configured LLM provider (local model first, Claude fallback), and the
LLM decides per turn whether to reply, take side-effect actions, or both.

Best-effort by design — any provider failure or schema mismatch falls
back to a static "couldn't reach my brain" reply rather than crashing.
The state machine (edit revision / reject_other) wins over chat-agent
dispatch upstream in `text_message_handler`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field
from telegram import Update
from telegram.ext import ContextTypes

from wire.config import ReposFile, WireConfig
from wire.llm.alerts import is_drafting_blocked_by_budget
from wire.llm.budget import log_llm_call
from wire.llm.provider import LLMProvider, parse_json_lenient
from wire.telegram.state_snapshot import build_state_snapshot

log = structlog.get_logger()

CHAT_PROMPT_PATH = Path(__file__).resolve().parents[1] / "llm" / "prompts" / "chat.txt"

ActionName = Literal["pause", "resume", "extend", "force_draft", "force_digest"]


class ChatAction(BaseModel):
    name: ActionName
    args: dict[str, Any] = Field(default_factory=dict)


class ChatResponse(BaseModel):
    reply: str = ""
    actions: list[ChatAction] = Field(default_factory=list)


def _load_prompt() -> str:
    return CHAT_PROMPT_PATH.read_text(encoding="utf-8")


_DEAD_CHANNEL_REPLY = "signal's choppy. /pause and /help still work as slashes if you need them."


async def handle_message(
    text: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Route a free-text message through the chat agent.

    Caller must have already passed the auth check and confirmed the user
    is not in an edit-state. Provider chain is configured-local-primary,
    Claude-fallback (via FallbackProvider).
    """
    cfg: WireConfig | None = context.bot_data.get("wire_config")
    repos: ReposFile | None = context.bot_data.get("wire_repos")
    provider: LLMProvider | None = context.bot_data.get("wire_provider")

    if cfg is None or provider is None:
        # Provider not wired (test or partial-init path) — silent no-op.
        return

    if is_drafting_blocked_by_budget(cfg):
        # Cap hit — don't burn another LLM call. Static reply.
        await _reply(update, "budget cap hit. /extend through the slash if you want more runway.")
        return

    snapshot = build_state_snapshot(cfg, repos)
    history = _get_history(context, update.effective_user.id if update.effective_user else 0)

    system = _load_prompt()
    user_message = f"{snapshot}\n\n─── johan ───\n{text}"
    messages = [*history, {"role": "user", "content": user_message}]

    try:
        resp = await provider.complete(
            task=cfg.persona.model_task,
            system=system,
            messages=messages,
            response_format=ChatResponse,
            max_tokens=500,
        )
    except Exception as e:  # noqa: BLE001 — chat must never crash the handler
        log.warning("wire.chat.llm_failed", error=str(e), error_type=type(e).__name__)
        await _reply(update, _DEAD_CHANNEL_REPLY)
        return

    resp.task = "persona"
    log_llm_call(resp)

    try:
        parsed = parse_json_lenient(resp.content)
        validated = ChatResponse.model_validate(parsed)
    except Exception as e:  # noqa: BLE001
        log.warning("wire.chat.parse_failed", error=str(e), raw=resp.content[:200])
        await _reply(update, _DEAD_CHANNEL_REPLY)
        return

    log.info(
        "wire.chat.routed",
        reply_len=len(validated.reply),
        action_count=len(validated.actions),
        actions=[a.name for a in validated.actions],
    )

    # Send the LLM's prose first (if any). Some actions emit follow-up
    # messages of their own (force_draft, force_digest); the reply lands
    # in chronological order before those.
    if validated.reply.strip():
        await _reply(update, validated.reply.strip())

    for action in validated.actions:
        await _execute_action(action, update, context)

    # Single-turn memory: keep just the last user/assistant pair so
    # follow-ups like "yes, do it" or "actually no, cancel that" still
    # resolve. Resets on container restart, which is fine.
    user_id = update.effective_user.id if update.effective_user else 0
    _update_history(context, user_id, text, validated.reply)


async def _execute_action(
    action: ChatAction,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Run a single action by delegating to the matching slash-command
    handler. The cmd writes its own confirmation message — that's by
    design: the LLM's reply gives the conversational frame, the cmd
    gives the precise after-state (timestamps, ids, etc.)."""
    from wire.telegram import commands as cmds

    name = action.name
    args = action.args or {}

    try:
        if name == "pause":
            hours = args.get("hours")
            context.args = [str(hours)] if hours is not None else []
            await cmds.pause_cmd(update, context)
        elif name == "resume":
            context.args = []
            await cmds.resume_cmd(update, context)
        elif name == "extend":
            usd = args.get("usd")
            context.args = [str(usd)] if usd is not None else []
            await cmds.extend_cmd(update, context)
        elif name == "force_draft":
            event_id = args.get("event_id")
            if event_id is None:
                log.warning("wire.chat.force_draft_no_id")
                return
            context.args = [str(event_id)]
            await cmds.draft_cmd(update, context)
        elif name == "force_digest":
            context.args = []
            await cmds.digest_cmd(update, context)
    except Exception:
        log.exception("wire.chat.action_failed", action=name)


async def _reply(update: Update, text: str) -> None:
    msg = update.effective_message
    if msg is not None:
        await msg.reply_text(text)


# --- single-turn history (in-memory) -----------------------------------------


def _get_history(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> list[dict[str, str]]:
    history_dict = context.bot_data.get("wire_chat_history") or {}
    return list(history_dict.get(user_id, []))


def _update_history(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    user_text: str,
    assistant_reply: str,
) -> None:
    history_dict = context.bot_data.setdefault("wire_chat_history", {})
    # We keep ONLY the last user/assistant pair — enough for "yes, do it"
    # follow-ups, not enough to drift. Bigger windows can come later if
    # conversations feel stilted.
    history_dict[user_id] = [
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": assistant_reply},
    ]

"""Telegram adapter for AutoCode remote control."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from ..config import Config
from .formatting import render_session_list, render_turn_result, split_message
from .manager import RemoteManager

logger = logging.getLogger(__name__)


def main():
    config = Config.from_env()
    _validate_config(config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    application = _build_application(config)
    application.run_polling(allowed_updates=None)


def _build_application(config: Config):
    telegram = _import_telegram()
    manager = RemoteManager(config)

    app = telegram["Application"].builder().token(config.telegram_bot_token).build()
    app.bot_data["manager"] = manager
    app.bot_data["allowed_chat_ids"] = set(config.telegram_allowed_chat_ids)

    app.add_handler(telegram["CommandHandler"]("start", _start))
    app.add_handler(telegram["CommandHandler"]("help", _start))
    app.add_handler(telegram["CommandHandler"]("reset", _reset))
    app.add_handler(telegram["CommandHandler"]("turn", _turn))
    app.add_handler(telegram["CommandHandler"]("sessions", _sessions))
    app.add_handler(telegram["CommandHandler"]("trace", _trace))
    app.add_handler(telegram["CommandHandler"]("approve", _approve))
    app.add_handler(telegram["CommandHandler"]("approve_scope", _approve_scope))
    app.add_handler(telegram["CommandHandler"]("reject", _reject))
    app.add_handler(telegram["CommandHandler"]("resume", _resume))
    app.add_handler(
        telegram["MessageHandler"](
            telegram["filters"].TEXT & ~telegram["filters"].COMMAND,
            _handle_message,
        )
    )
    return app


def _validate_config(config: Config):
    if not config.model:
        raise SystemExit("No model configured. Set AUTOCODE_MODEL before starting Telegram control.")
    if not config.api_key:
        raise SystemExit("No API key configured. Set AUTOCODE_API_KEY before starting Telegram control.")
    if not config.telegram_bot_token:
        raise SystemExit("No Telegram bot token configured. Set AUTOCODE_TELEGRAM_BOT_TOKEN.")
    if not config.telegram_allowed_chat_ids:
        raise SystemExit(
            "No allowed Telegram chats configured. Set AUTOCODE_TELEGRAM_ALLOWED_CHATS to a comma-separated list."
        )


def _import_telegram():
    try:
        from telegram.constants import ChatAction
        from telegram.ext import Application, CommandHandler, MessageHandler, filters
    except ImportError as exc:
        raise SystemExit(
            "Telegram support is optional. Install it with: pip install -e '.[telegram]'"
        ) from exc
    return {
        "Application": Application,
        "CommandHandler": CommandHandler,
        "MessageHandler": MessageHandler,
        "filters": filters,
        "ChatAction": ChatAction,
    }


def _is_allowed_chat(update, context) -> bool:
    chat = getattr(update, "effective_chat", None)
    if chat is None:
        return False
    allowed = context.application.bot_data.get("allowed_chat_ids", set())
    return chat.id in allowed


async def _start(update, context):
    if not _is_allowed_chat(update, context):
        return
    text = (
        "AutoCode Telegram control is ready.\n\n"
        "Commands:\n"
        "/turn - show current turn\n"
        "/sessions - list recent sessions\n"
        "/trace - show trace for the current session\n"
        "/approve - approve the pending tool call\n"
        "/approve_scope - approve and allow this scope for the current turn\n"
        "/reject - reject the pending tool call\n"
        "/resume <session_id> - restore a session into this chat\n"
        "/reset - clear the in-memory chat session\n\n"
        "Any non-command text is sent to the coding agent."
    )
    await _reply_text(update.message, text)


async def _reset(update, context):
    if not _is_allowed_chat(update, context):
        return
    manager: RemoteManager = context.application.bot_data["manager"]
    manager.reset_chat(update.effective_chat.id)
    await _reply_text(update.message, "Chat session cleared.")


async def _turn(update, context):
    if not _is_allowed_chat(update, context):
        return
    manager: RemoteManager = context.application.bot_data["manager"]
    try:
        text = await asyncio.to_thread(manager.current_turn_summary, update.effective_chat.id)
    except ValueError as exc:
        text = str(exc)
    await _reply_text(update.message, text)


async def _sessions(update, context):
    if not _is_allowed_chat(update, context):
        return
    manager: RemoteManager = context.application.bot_data["manager"]
    sessions = await asyncio.to_thread(manager.list_recent_sessions)
    await _reply_text(update.message, render_session_list(sessions))


async def _trace(update, context):
    if not _is_allowed_chat(update, context):
        return
    manager: RemoteManager = context.application.bot_data["manager"]
    try:
        text = await asyncio.to_thread(manager.current_trace, update.effective_chat.id)
    except ValueError as exc:
        text = str(exc)
    await _reply_text(update.message, text)


async def _approve(update, context):
    await _handle_approval(update, context, approved=True)


async def _approve_scope(update, context):
    await _handle_approval(update, context, approved=True, grant_scope=True)


async def _reject(update, context):
    await _handle_approval(update, context, approved=False)


async def _handle_approval(update, context, approved: bool, grant_scope: bool = False):
    if not _is_allowed_chat(update, context):
        return
    manager: RemoteManager = context.application.bot_data["manager"]
    try:
        result = await asyncio.to_thread(
            manager.resolve_next_approval,
            update.effective_chat.id,
            approved=approved,
            grant_scope=grant_scope,
        )
        text = render_turn_result(result)
    except ValueError as exc:
        text = str(exc)
    await _reply_text(update.message, text)


async def _resume(update, context):
    if not _is_allowed_chat(update, context):
        return
    if not context.args:
        await _reply_text(update.message, "Usage: /resume <session_id>")
        return

    manager: RemoteManager = context.application.bot_data["manager"]
    session_id = context.args[0].strip()
    try:
        result = await asyncio.to_thread(manager.resume_session, update.effective_chat.id, session_id)
        text = render_turn_result(result)
    except ValueError as exc:
        text = str(exc)
    await _reply_text(update.message, text)


async def _handle_message(update, context):
    if not _is_allowed_chat(update, context):
        return
    if not update.message or not update.message.text:
        return

    telegram = _import_telegram()
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=telegram["ChatAction"].TYPING)

    manager: RemoteManager = context.application.bot_data["manager"]
    try:
        result = await asyncio.to_thread(manager.submit, update.effective_chat.id, update.message.text.strip())
        text = render_turn_result(result)
    except Exception as exc:
        logger.exception("Telegram agent turn failed")
        text = f"Error: {exc}"
    await _reply_text(update.message, text)


async def _reply_text(message: Any, text: str):
    for chunk in split_message(text):
        await message.reply_text(chunk)


if __name__ == "__main__":
    main()


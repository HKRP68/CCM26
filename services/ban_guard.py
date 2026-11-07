"""Enforce account bans before Telegram handlers and Mini App actions."""

import asyncio
from html import escape
import logging

from telegram.ext import ApplicationHandlerStop

from services.command_token import (command_token_from_text, command_token_from_update,
                                    is_command_for_this_bot)

logger = logging.getLogger(__name__)


def banned_error(user):
    if user is not None and user.is_banned:
        return {"ok": False, "error": "banned",
                "message": "This account is suspended."}, 403
    return None


async def check_ban(update, context):
    """Stop banned senders silently except for slash commands to this bot.

    Return the reset/debut flag for the main bot's separate onboarding gate.
    Read current state on every update: bans can change in another process
    through the admin website or the dedicated auction bot.
    """
    sender = update.effective_user
    if sender is None:
        return False

    def account_state():
        from database import get_session
        from models import User
        with get_session() as db:
            user = (db.query(User.is_banned, User.ban_reason, User.needs_debut)
                    .filter(User.telegram_id == sender.id).first())
            return (bool(user and user.is_banned),
                    (user.ban_reason or "").strip() if user else "",
                    bool(user and user.needs_debut))

    try:
        is_banned, reason, needs_debut = await asyncio.to_thread(account_state)
    except Exception:
        logger.exception("Account access check failed; stopping update")
        raise ApplicationHandlerStop

    if not is_banned:
        return needs_debut

    username = getattr(context.bot, "username", None)
    message = getattr(update, "message", None)
    caption_command = command_token_from_text(getattr(message, "caption", None))
    if (command_token_from_update(update, username)
            or (caption_command and is_command_for_this_bot(caption_command, username))):
        text = ("🚫 <b>You are banned from CricMaster Ultra.</b>\n"
                + (f"\n<i>Reason: {escape(reason)}</i>" if reason else ""))
        try:
            await update.message.reply_text(text, parse_mode="HTML")
        except Exception:
            logger.debug("Could not deliver ban notice", exc_info=True)
    raise ApplicationHandlerStop

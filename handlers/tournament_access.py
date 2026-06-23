"""Admin commands to manage the Challenge League Tournament command allowlist.

These let a bot admin control, from Telegram chat, exactly who is allowed to use
the Challenge League Tournament command (the per-league ``tournament_command``).
The allowlist itself lives in ``GameConfig.tournament_allowed_ids`` (a
comma-separated Telegram-ID string) and is also editable from the admin website.

Empty list = open to everyone (restriction off). Once at least one ID is added,
only those IDs (and bot admins) may start tournament matches. The enforcement
lives in ``handlers.challenge._handle_tournament_command`` via
``services.tournament_service.is_tournament_command_allowed``.

Commands (admin-only, gated by ``is_admin``):
  /tourallow <id>   — add a Telegram ID (or reply to a user) to the allowlist.
  /tourblock <id>   — remove a Telegram ID (or reply to a user) from the list.
  /tourallowlist    — show the current allowlist and whether it is ON/OFF.
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import GameConfig
from services import tournament_service
from services.config_service import _refresh as _refresh_cfg
from services.admin_ids import is_admin

logger = logging.getLogger(__name__)

NOT_ADMIN = "⛔ Only bot admins can use this command."


def _resolve_target_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pick a Telegram user ID from the command arg or a replied-to message."""
    if context.args:
        token = context.args[0].strip().lstrip("@")
        try:
            value = int(token)
            if value > 0:
                return value
        except ValueError:
            return None
    message = update.effective_message
    reply = getattr(message, "reply_to_message", None) if message else None
    target = getattr(reply, "from_user", None) if reply else None
    if target and not getattr(target, "is_bot", False):
        return int(target.id)
    return None


def _load_config_row(session):
    """Return the single GameConfig row, creating it if missing."""
    row = session.query(GameConfig).first()
    if not row:
        row = GameConfig()
        session.add(row)
        session.flush()
    return row


def _save_ids(session, row, ids):
    """Persist a set of IDs back to the row (sorted) and invalidate the cache."""
    row.tournament_allowed_ids = ", ".join(str(i) for i in sorted(ids)) or None
    session.commit()
    _refresh_cfg(session)


async def tourallow_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tourallow <id> — add a Telegram ID to the tournament-command allowlist."""
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    if not is_admin(user.id):
        await message.reply_text(NOT_ADMIN)
        return
    target_id = _resolve_target_id(update, context)
    if not target_id:
        await message.reply_text(
            "Usage: /tourallow <telegram_id>\n"
            "Or reply to a user's message with /tourallow.")
        return
    session = get_session()
    try:
        row = _load_config_row(session)
        ids = tournament_service.parse_allowed_ids(row.tournament_allowed_ids)
        if target_id in ids:
            await message.reply_text(
                f"ℹ️ <code>{target_id}</code> is already allowed.", parse_mode="HTML")
            return
        ids.add(target_id)
        _save_ids(session, row, ids)
        await message.reply_text(
            f"✅ Added <code>{target_id}</code> to the Tournament command allowlist.\n"
            f"Now {len(ids)} user(s) allowed (restriction ON).", parse_mode="HTML")
    except Exception:
        session.rollback()
        logger.exception("/tourallow failed")
        await message.reply_text("⚠️ Could not update the allowlist.")
    finally:
        session.close()


async def tourblock_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tourblock <id> — remove a Telegram ID from the tournament-command allowlist."""
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    if not is_admin(user.id):
        await message.reply_text(NOT_ADMIN)
        return
    target_id = _resolve_target_id(update, context)
    if not target_id:
        await message.reply_text(
            "Usage: /tourblock <telegram_id>\n"
            "Or reply to a user's message with /tourblock.")
        return
    session = get_session()
    try:
        row = _load_config_row(session)
        ids = tournament_service.parse_allowed_ids(row.tournament_allowed_ids)
        if target_id not in ids:
            await message.reply_text(
                f"ℹ️ <code>{target_id}</code> is not on the allowlist.", parse_mode="HTML")
            return
        ids.discard(target_id)
        _save_ids(session, row, ids)
        state = (f"{len(ids)} user(s) allowed (restriction ON)." if ids
                 else "List is now empty — the command is open to everyone.")
        await message.reply_text(
            f"✅ Removed <code>{target_id}</code> from the Tournament command allowlist.\n{state}",
            parse_mode="HTML")
    except Exception:
        session.rollback()
        logger.exception("/tourblock failed")
        await message.reply_text("⚠️ Could not update the allowlist.")
    finally:
        session.close()


async def tourallowlist_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tourallowlist — show the current tournament-command allowlist."""
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    if not is_admin(user.id):
        await message.reply_text(NOT_ADMIN)
        return
    session = get_session()
    try:
        row = session.query(GameConfig).first()
        raw = row.tournament_allowed_ids if row else None
        ids = sorted(tournament_service.parse_allowed_ids(raw))
        if not ids:
            await message.reply_text(
                "🏆 <b>Tournament Command Access</b>\n"
                "Status: <b>OFF</b> — open to everyone.\n\n"
                "Add someone with /tourallow &lt;id&gt; (or reply to their message).",
                parse_mode="HTML")
            return
        lines = "\n".join(f"• <code>{i}</code>" for i in ids)
        await message.reply_text(
            "🏆 <b>Tournament Command Access</b>\n"
            f"Status: <b>ON</b> — {len(ids)} allowed user(s) (plus bot admins):\n\n"
            f"{lines}\n\n"
            "Use /tourblock &lt;id&gt; to remove someone.",
            parse_mode="HTML")
    except Exception:
        logger.exception("/tourallowlist failed")
        await message.reply_text("⚠️ Could not load the allowlist.")
    finally:
        session.close()

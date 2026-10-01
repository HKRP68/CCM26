"""Who reviews player submissions — team logos and CMU News stories.

Review DMs go to the bot admins/owners from the environment plus the extra
reviewers stored in ``GameConfig.approval_reviewer_ids`` (also editable on the
website's Maintenance page). Nobody else — in particular not the
maintenance-bypass testers. See ``services/admin_ids.py``.

One command, environment admins/owners only (an added reviewer cannot add
more):
  /approvers               — list who currently receives review DMs.
  /approvers add <id>      — add a reviewer (or reply to their message).
  /approvers remove <id>   — remove a reviewer (or reply to their message).
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import GameConfig
from services.admin_ids import (
    can_manage_reviewers, env_admin_ids, extra_reviewer_ids, format_id_list,
    parse_id_list,
)
from services.config_service import _refresh as _refresh_cfg

logger = logging.getLogger(__name__)

NOT_ADMIN = "⛔ Only bot admins can manage approval reviewers."


def _target_id(update, args):
    """A Telegram user id from the argument or a replied-to message."""
    if args:
        ids = parse_id_list([args[0].lstrip("@")])
        return next(iter(ids), None)
    message = update.effective_message
    reply = getattr(message, "reply_to_message", None) if message else None
    who = getattr(reply, "from_user", None) if reply else None
    if who and not getattr(who, "is_bot", False):
        return int(who.id)
    return None


def _config_row(session):
    row = session.query(GameConfig).first()
    if row is None:
        row = GameConfig()
        session.add(row)
        session.flush()
    return row


def _save(session, row, ids):
    row.approval_reviewer_ids = format_id_list(ids)
    session.commit()
    # Already committed; a cache hiccup must not read as a failed save.
    try:
        _refresh_cfg(session)
    except Exception:
        logger.exception("Approval reviewers saved, but config cache refresh failed")


async def _change(update, args, *, add):
    message = update.effective_message
    command = "/approvers add" if add else "/approvers remove"
    target = _target_id(update, args)
    if not target:
        await message.reply_text(
            f"Usage: {command} <telegram_id>\n"
            f"Or reply to the person's message with {command}.")
        return
    session = get_session()
    try:
        row = _config_row(session)
        ids = parse_id_list([row.approval_reviewer_ids])
        if add and target in ids:
            await message.reply_text(
                f"ℹ️ <code>{target}</code> is already a reviewer.", parse_mode="HTML")
            return
        if not add and target not in ids:
            note = (" They are a bot admin from the environment, so they always "
                    "review." if target in env_admin_ids() else "")
            await message.reply_text(
                f"ℹ️ <code>{target}</code> is not an added reviewer.{note}",
                parse_mode="HTML")
            return
        if add:
            ids.add(target)
        else:
            ids.discard(target)
        _save(session, row, ids)
        verb = "Added" if add else "Removed"
        await message.reply_text(
            f"✅ {verb} <code>{target}</code> "
            f"{'to' if add else 'from'} the approval reviewers.\n"
            "They " + ("will now get" if add else "will no longer get")
            + " team logo and CMU News review DMs.",
            parse_mode="HTML")
    except Exception:
        session.rollback()
        logger.exception("%s failed", command)
        await message.reply_text("⚠️ Could not update the reviewer list.")
    finally:
        session.close()


async def approvers_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    if not can_manage_reviewers(user.id):
        await message.reply_text(NOT_ADMIN)
        return
    args = list(context.args or [])
    action = args[0].lower() if args else ""
    if action in ("add", "remove", "rm", "del"):
        await _change(update, args[1:], add=(action == "add"))
        return
    admins = sorted(env_admin_ids())
    extra = sorted(extra_reviewer_ids() - set(admins))

    def _lines(ids):
        return "\n".join(f"• <code>{i}</code>" for i in ids) or "• (none)"

    await message.reply_text(
        "🛂 <b>Approval reviewers</b>\n"
        "These people get team logo and CMU News review DMs.\n\n"
        f"<b>Bot admins</b> (from the environment):\n{_lines(admins)}\n\n"
        f"<b>Added reviewers</b>:\n{_lines(extra)}\n\n"
        "Add: /approvers add &lt;id&gt;\n"
        "Remove: /approvers remove &lt;id&gt;",
        parse_mode="HTML")

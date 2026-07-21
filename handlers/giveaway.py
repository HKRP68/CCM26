"""Giveaway participation — the "🎉 Participate" button callback.

Every eligibility rule is enforced here, server-side, from the authoritative
``update.effective_user.id`` (Telegram signs callback_data to the message, so a
user can't forge another identity or an arbitrary giveaway id):

  * giveaway must be ``running`` and inside its [start, end] window,
  * the tapper must have a ``User`` row (has done /debut) and not be banned,
  * the tapper must be a live member of the Official GC,
  * optional min-activity gate (``require_min_activity``),
  * exactly one entry per user (enforced by the DB unique index in record_entry).
"""

import logging
from datetime import datetime

from telegram import Update
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import ContextTypes

from database import get_session
from models import User, Giveaway, GameConfig
from services import giveaway_service

logger = logging.getLogger(__name__)

# Telegram member statuses that count as "in the group".
_MEMBER_STATUSES = {"creator", "administrator", "member", "restricted"}


async def giveaway_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    try:
        giveaway_id = int(query.data.split("_", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Invalid giveaway.", show_alert=True)
        return

    tg_user = update.effective_user
    session = get_session()
    try:
        giveaway = session.query(Giveaway).filter(Giveaway.id == giveaway_id).first()
        if not giveaway:
            await query.answer("This giveaway no longer exists.", show_alert=True)
            return

        # Window / status check (server-side; ignores anything the client says).
        now = datetime.utcnow()
        if giveaway.status != "running" or not (
                giveaway.start_time <= now <= giveaway.end_time):
            await query.answer("⏰ This giveaway isn't open for entries.",
                               show_alert=True)
            return

        # Must be a registered player.
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await query.answer("❌ Do /debut first to join giveaways!",
                               show_alert=True)
            return

        if user.is_banned:
            await query.answer("🚫 You can't participate.", show_alert=True)
            return

        # Optional alt-account gate.
        if giveaway.require_min_activity and (user.matches_played or 0) < 1:
            await query.answer(
                "❌ Play at least one match before joining giveaways.",
                show_alert=True)
            return

        # Official GC membership gate. Fail closed: if no Official GC is
        # configured we can't verify membership, so refuse rather than silently
        # turning a GC-gated giveaway into an open one.
        cfg = session.query(GameConfig).first()
        official_group_id = cfg.official_group_id if cfg else None
        if not official_group_id:
            await query.answer("This giveaway isn't available right now.",
                               show_alert=True)
            return
        is_member = await _is_group_member(context, official_group_id, tg_user.id)
        if not is_member:
            await _answer_join_gc(query, cfg)
            return

        # Record the entry (one-per-user enforced by the DB unique index).
        status = giveaway_service.record_entry(session, giveaway, user, tg_user.id)
        if status == "already":
            count = giveaway_service.entry_count(session, giveaway.id)
            await query.answer(f"✅ You already joined! ({count} in so far)",
                               show_alert=False)
            return
        if status == "error":
            await query.answer("⚠️ Something went wrong. Try again.",
                               show_alert=True)
            return

        session.commit()
        count = giveaway_service.entry_count(session, giveaway.id)
        await query.answer(f"🎉 You're in! ({count} participants)",
                           show_alert=False)
    except Exception:
        session.rollback()
        logger.exception("giveaway_join_callback failed")
        try:
            await query.answer("⚠️ Something went wrong. Try again.",
                               show_alert=True)
        except TelegramError:
            pass
    finally:
        session.close()


async def _is_group_member(context, group_id, user_id) -> bool:
    try:
        member = await context.bot.get_chat_member(group_id, user_id)
    except (BadRequest, Forbidden):
        return False
    except TelegramError:
        # Transient Telegram error — be lenient rather than block unfairly.
        return True
    status = getattr(member, "status", None)
    if status not in _MEMBER_STATUSES:
        return False
    if status == "restricted":
        return bool(getattr(member, "is_member", False))
    return True


async def _answer_join_gc(query, cfg):
    """Tell the non-member (privately, via an alert popup — no group spam) to
    join the Official Group first, naming its @handle when there is one."""
    handle = giveaway_service.group_handle(cfg)
    if handle:
        msg = (f"🔒 Join our Official Group {handle} to participate "
               "in this giveaway! 🎁")
    else:
        msg = ("🔒 Join our Official Group to participate in this giveaway! 🎁")
    await query.answer(msg, show_alert=True)

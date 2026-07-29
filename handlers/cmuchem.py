"""Handler for /cmuchem — the Team Chemistry breakdown for your Playing XI.

Re-cuts the 80/20 chemistry score (``services.chemistry``) by role, so a
manager can see *which unit* is badly connected instead of one opaque number.
Each role line reads ``country/20 + xi/20 = +bonus/max``; the squad-wide
bonuses and the honest sum of every part follow underneath.
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import User
from services import chemistry
from handlers.lineup import _get_ordered_roster

logger = logging.getLogger(__name__)


async def cmuchem_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    logger.info(f"/cmuchem from user {tg_user.id}")

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        roster = _get_ordered_roster(session, user.id)
        players = [player for _entry, player in roster[:11]]

        if len(players) < 11:
            await update.message.reply_text(
                f"🧪 <b>TEAM CHEMISTRY</b>\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"You need <b>11 players</b> to read your chemistry "
                f"(you have <b>{len(players)}</b>).\n\n"
                f"🎁 <code>/claim</code> — free daily player\n"
                f"📦 <code>/buypack</code> — open a pack\n\n"
                f"<i>Then set your order with /pxi.</i>",
                parse_mode="HTML",
            )
            return

        await update.message.reply_text(
            chemistry.render_chemistry_card(players), parse_mode="HTML")

    except Exception:
        logger.exception(f"Chemistry error for user {tg_user.id}")
        await update.message.reply_text(
            "❌ Couldn't read your chemistry right now. Try again shortly.")
    finally:
        session.close()

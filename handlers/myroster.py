"""Handler for /myroster — paginated roster with stats and release buttons."""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User
from config import MAX_ROSTER, ROSTER_PAGE_SIZE, get_sell_value
from services.roster_service import get_user_roster, get_roster_stats

logger = logging.getLogger(__name__)


def _build_roster_message(user, entries, stats, page, total, total_pages):
    """Build the roster text and keyboard for a given page."""
    lines = []
    global_idx = (page - 1) * ROSTER_PAGE_SIZE

    # Track duplicate player_ids across the FULL roster (not just this page)
    # We already have duplicate count in stats

    for i, (entry, player) in enumerate(entries, 1):
        idx = global_idx + i
        sell = get_sell_value(player.rating)
        lines.append(
            f"{idx}. {player.name} - {player.rating} OVR | {player.category}\n"
            f"   💸 Sell: {sell:,} 🪙"
        )

    roster_text = "\n\n".join(lines)

    text = (
        f"📊 <b>YOUR ROSTER</b> ({total}/{MAX_ROSTER})\n\n"
        f"📈 <b>Roster Stats:</b>\n"
        f"• Avg Rating: {stats['avg_rating']}\n"
        f"• Total Value: {stats['total_value']:,} 🪙\n"
        f"• Duplicates: {stats['duplicates']}\n\n"
        f"👥 <b>Players (Page {page}/{total_pages}):</b>\n\n"
        f"{roster_text}"
    )

    # Build navigation + action buttons
    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton("◀️ Previous", callback_data=f"roster_page_{page - 1}"))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton("Next ▶️", callback_data=f"roster_page_{page + 1}"))

    keyboard_rows = []
    if nav_buttons:
        keyboard_rows.append(nav_buttons)

    keyboard = InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None
    return text, keyboard


async def myroster_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    logger.info(f"/myroster from user {tg_user.id}")

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        page = 1
        if context.args:
            try:
                page = max(1, int(context.args[0]))
            except ValueError:
                pass

        entries, total, total_pages = get_user_roster(session, user.id, page, ROSTER_PAGE_SIZE)

        if total == 0:
            await update.message.reply_text("📊 Your roster is empty. Use /claim to get players!")
            return

        stats = get_roster_stats(session, user.id)
        text, keyboard = _build_roster_message(user, entries, stats, page, total, total_pages)

        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

    except Exception:
        logger.exception(f"MyRoster error for user {tg_user.id}")
        await update.message.reply_text("⚠️ Database error. Please try again later.")
    finally:
        session.close()


async def roster_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle pagination button clicks."""
    query = update.callback_query
    await query.answer()
    tg_user = query.from_user

    page = int(query.data.split("_")[-1])

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            return

        entries, total, total_pages = get_user_roster(session, user.id, page, ROSTER_PAGE_SIZE)
        stats = get_roster_stats(session, user.id)
        text, keyboard = _build_roster_message(user, entries, stats, page, total, total_pages)

        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)

    except Exception:
        logger.exception(f"Roster page error for user {tg_user.id}")
    finally:
        session.close()

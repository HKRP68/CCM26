"""Handler for /releasepl [name] and /releasemultiple."""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, Player, UserRoster
from config import get_sell_value
from services.activity_service import log_activity
from services.roster_service import find_roster_entry, release_player, get_duplicate_entries, release_duplicates

logger = logging.getLogger(__name__)


async def releasepl_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user

    if not context.args:
        await update.message.reply_text("Usage: /releasepl <player name>\nExample: /releasepl Virat Kohli")
        return

    search_name = " ".join(context.args).strip()
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        entry, player = find_roster_entry(session, user.id, search_name)
        if not entry:
            await update.message.reply_text(f"❌ No player named '{search_name}' in your roster")
            return

        sell_val = get_sell_value(player.rating)

        text = (
            "🔴 <b>RELEASE PLAYER?</b>\n\n"
            f"Player: {player.name}\n"
            f"Rating: {player.rating} OVR\n"
            f"Category: {player.category}\n\n"
            f"💸 You will receive: {sell_val:,} 🪙"
        )

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm Release", callback_data=f"rlconfirm_{entry.id}"),
            InlineKeyboardButton("❌ Cancel", callback_data="rlcancel"),
        ]])

        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

    except Exception:
        logger.exception(f"ReleasePl error")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


async def release_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tg_user = query.from_user

    roster_entry_id = int(query.data.split("_")[1])

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await query.edit_message_reply_markup(reply_markup=None)
            return

        result = release_player(session, user, roster_entry_id)

        if not result["success"]:
            await query.edit_message_text(f"❌ {result['error']}")
            return

        log_activity(session, user.id, "releasepl",
                     f"Released {result['name']} for {result['sell_value']:,}",
                     coins_change=result["sell_value"],
                     player_name=result["name"], player_rating=result["rating"])
        session.commit()

        await query.edit_message_text(
            f"✅ <b>PLAYER RELEASED!</b>\n\n"
            f"{result['name']} - {result['rating']} OVR\n\n"
            f"💸 Received: {result['sell_value']:,} 🪙\n"
            f"💰 New Balance: {result['new_balance']:,}\n"
            f"📊 Roster: {result['new_count']}/25",
            parse_mode="HTML")

    except Exception:
        session.rollback()
        logger.exception("Release confirm error")
    finally:
        session.close()


async def release_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Cancelled")
    try:
        await query.edit_message_text("❌ Release cancelled.")
    except Exception:
        pass


async def releasemultiple_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        dupes = get_duplicate_entries(session, user.id)
        if not dupes:
            await update.message.reply_text("📊 No duplicate players in your roster!")
            return

        lines = []
        buttons = []
        for i, (player, qty) in enumerate(dupes, 1):
            sell = get_sell_value(player.rating)
            lines.append(f"{i}. {player.name} - {player.rating} OVR (×{qty})\n   💸 {sell:,} 🪙 each")
            row = [InlineKeyboardButton(
                f"Release 1× {player.name[:15]}",
                callback_data=f"rldup_{player.id}_1")]
            if qty > 2:
                row.append(InlineKeyboardButton(f"Release {qty-1}×", callback_data=f"rldup_{player.id}_{qty-1}"))
            buttons.append(row)

        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="rlcancel")])

        await update.message.reply_text(
            f"📋 <b>YOUR DUPLICATE PLAYERS</b>\n\n" + "\n\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons))

    except Exception:
        logger.exception("ReleaseMultiple error")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


async def release_dup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tg_user = query.from_user

    parts = query.data.split("_")
    player_id, count = int(parts[1]), int(parts[2])

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            return

        result = release_duplicates(session, user, player_id, count)
        session.commit()

        if not result["success"]:
            await query.edit_message_text(f"❌ {result['error']}")
            return

        await query.edit_message_text(
            f"✅ <b>DUPLICATES RELEASED!</b>\n\n"
            f"📛 {result['name']} - {result['rating']} OVR\n"
            f"🔢 Released: {result['released_count']}×\n\n"
            f"💸 Total: {result['total_value']:,} 🪙\n"
            f"💰 Balance: {result['new_balance']:,}\n"
            f"📊 Roster: {result['new_count']}/25",
            parse_mode="HTML")

    except Exception:
        session.rollback()
        logger.exception("ReleaseDup error")
    finally:
        session.close()

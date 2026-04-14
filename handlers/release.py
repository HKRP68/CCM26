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
    """/releasemultiple 7 11 — release players from position 7 to 11."""
    tg_user = update.effective_user
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        if not context.args or len(context.args) < 2:
            await update.message.reply_text(
                "Usage: /releasemultiple <from> <to>\n"
                "Example: /releasemultiple 7 11\n"
                "Releases roster positions 7 through 11")
            return

        try:
            pos_from = int(context.args[0])
            pos_to = int(context.args[1])
        except ValueError:
            await update.message.reply_text("❌ Positions must be numbers.")
            return

        if pos_from > pos_to:
            pos_from, pos_to = pos_to, pos_from

        # Get ordered roster
        entries = (
            session.query(UserRoster, Player)
            .join(Player, UserRoster.player_id == Player.id)
            .filter(UserRoster.user_id == user.id)
            .order_by(UserRoster.order_position)
            .all()
        )

        if pos_from < 1 or pos_to > len(entries):
            await update.message.reply_text(f"❌ Positions must be 1-{len(entries)}")
            return

        to_release = entries[pos_from - 1:pos_to]
        total_sell = 0
        lines = []
        for entry, player in to_release:
            sv = get_sell_value(player.rating)
            total_sell += sv
            lines.append(f"• {player.name} - {player.rating} OVR | 💸 {sv:,}")

        # Store in bot_data for confirm
        release_data = [(entry.id, player.name, player.rating, get_sell_value(player.rating))
                        for entry, player in to_release]
        context.bot_data[f"relm_{tg_user.id}"] = release_data

        text = (
            f"🔴 <b>RELEASE PLAYERS?</b>\n\n"
            f"Position {pos_from} to {pos_to} ({len(to_release)} players):\n\n"
            + "\n".join(lines) +
            f"\n\n💸 Total: {total_sell:,} 🪙"
        )

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Release All", callback_data=f"relmconf_{user.id}"),
            InlineKeyboardButton("❌ Cancel", callback_data="rlcancel"),
        ]])

        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

    except Exception:
        logger.exception("ReleaseMultiple error")
        await update.message.reply_text("⚠️ Error.")
    finally:
        session.close()


async def releasemultiple_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tg_user = query.from_user
    uid = int(query.data.split("_")[1])

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user or user.id != uid:
            await query.answer("Not yours!")
            return
        await query.answer()

        release_data = context.bot_data.pop(f"relm_{tg_user.id}", [])
        if not release_data:
            await query.edit_message_text("❌ Nothing to release.")
            return

        total_coins = 0
        released = 0
        for roster_id, name, rating, sell_val in release_data:
            entry = session.query(UserRoster).filter(
                UserRoster.id == roster_id, UserRoster.user_id == user.id).first()
            if entry:
                session.delete(entry)
                user.roster_count -= 1
                user.total_coins += sell_val
                total_coins += sell_val
                released += 1
                log_activity(session, user.id, "release", f"Released {name} for {sell_val:,}",
                             coins_change=sell_val, player_name=name, player_rating=rating)

        session.commit()

        await query.edit_message_text(
            f"✅ <b>RELEASED {released} PLAYERS!</b>\n\n"
            f"💸 Total: {total_coins:,} 🪙\n"
            f"💰 Balance: {user.total_coins:,}\n"
            f"📊 Roster: {user.roster_count}/25",
            parse_mode="HTML")

    except Exception:
        session.rollback()
        logger.exception("ReleaseMultiple confirm error")
    finally:
        session.close()


async def release_dup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Backward compat — old dup release buttons."""
    query = update.callback_query
    await query.answer("Use /releasemultiple <from> <to> instead")

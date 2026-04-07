"""Handler for /buypl <player_name> — buy a player from the market."""

import io
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, Player, UserRoster
from services.card_generator import generate_card
from config import get_buy_value, get_sell_value, MAX_ROSTER
from services.activity_service import log_activity
from services.card_text import format_player_card

logger = logging.getLogger(__name__)


async def buypl_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user

    if not context.args:
        await update.message.reply_text("Usage: /buypl <player name>\nExample: /buypl Virat Kohli")
        return

    search = " ".join(context.args).strip()
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        if user.roster_count >= MAX_ROSTER:
            await update.message.reply_text("❌ Roster full (25/25)! Release players first.")
            return

        player = (
            session.query(Player)
            .filter(Player.name.ilike(f"%{search}%"), Player.is_active == True)
            .first()
        )
        if not player:
            await update.message.reply_text(f"❌ No player found matching '{search}'")
            return

        buy_val = get_buy_value(player.rating)

        text = (
            format_player_card(player) + "\n\n"
            f"💳 Your Balance: {user.total_coins:,} 🪙"
        )

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("💰 Buy", callback_data=f"buypl_{player.id}_{user.id}"),
            InlineKeyboardButton("❌ Cancel", callback_data="buycancel"),
        ]])

        card_bytes = generate_card(player)
        if card_bytes:
            await update.message.reply_photo(
                photo=io.BytesIO(card_bytes), caption=text,
                parse_mode="HTML", reply_markup=keyboard,
            )
        else:
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

    except Exception:
        logger.exception(f"BuyPl error for {tg_user.id}")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


async def buypl_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tg_user = query.from_user

    parts = query.data.split("_")  # buypl_{player_id}_{user_id}
    player_id = int(parts[1])
    owner_user_id = int(parts[2])

    session = get_session()
    try:
        user = session.query(User).get(owner_user_id)
        if not user or user.telegram_id != tg_user.id:
            await query.edit_message_reply_markup(reply_markup=None)
            return

        player = session.query(Player).get(player_id)
        if not player:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("❌ Player no longer available")
            return

        buy_val = get_buy_value(player.rating)
        username = tg_user.username or tg_user.first_name

        if user.total_coins < buy_val:
            shortage = buy_val - user.total_coins
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                f"❌ @{username} needs {shortage:,} more coins to buy {player.name}\n"
                f"💰 Balance: {user.total_coins:,} | Price: {buy_val:,}"
            )
            return

        if user.roster_count >= MAX_ROSTER:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("❌ Roster full! Release players first.")
            return

        # Buy the player
        user.total_coins -= buy_val
        entry = UserRoster(
            user_id=user.id, player_id=player.id,
            order_position=user.roster_count + 1,
            acquired_date=datetime.utcnow(),
        )
        session.add(entry)
        user.roster_count += 1

        log_activity(session, user.id, "buy",
                     f"Bought {player.name} ({player.rating} OVR) for {buy_val:,}",
                     coins_change=-buy_val,
                     player_name=player.name, player_rating=player.rating)
        session.commit()

        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            f"✅ <b>PURCHASED!</b>\n\n"
            f"📛 {player.name} - {player.rating} OVR\n"
            f"💰 Paid: {buy_val:,} 🪙\n"
            f"💳 Balance: {user.total_coins:,} 🪙\n"
            f"📊 Roster: {user.roster_count}/25",
            parse_mode="HTML",
        )

    except Exception:
        session.rollback()
        logger.exception(f"BuyPl confirm error")
    finally:
        session.close()


async def buypl_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Cancelled")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

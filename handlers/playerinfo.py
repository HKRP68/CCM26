"""Handler for /playerinfo [name] — show full player details."""

import io
import logging
from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import User, Player, UserRoster
from services.card_generator import generate_card
from config import get_buy_value, get_sell_value

logger = logging.getLogger(__name__)


async def playerinfo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    logger.info(f"/playerinfo from user {tg_user.id}, args={context.args}")

    if not context.args:
        await update.message.reply_text(
            "Usage: /playerinfo <player name>\nExample: /playerinfo Virat Kohli"
        )
        return

    search_name = " ".join(context.args).strip()

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        # Search by name (case-insensitive partial match)
        player = (
            session.query(Player)
            .filter(Player.name.ilike(f"%{search_name}%"))
            .first()
        )

        if not player:
            await update.message.reply_text(f"❌ Player not found: {search_name}")
            return

        # Check if user owns this player
        owned = (
            session.query(UserRoster)
            .filter(UserRoster.user_id == user.id, UserRoster.player_id == player.id)
            .first()
        )

        buy_val = get_buy_value(player.rating)
        sell_val = get_sell_value(player.rating)
        acquired_str = owned.acquired_date.strftime("%d %b %Y") if owned else "Not owned"

        text = (
            f"📛 <b>{player.name}</b>\n"
            f"⭐ Rating: {player.rating} OVR\n\n"
            f"👤 <b>Bio:</b>\n"
            f"🎯 Category: {player.category}\n"
            f"🏏 Bat Hand: {player.bat_hand}\n"
            f"🎳 Bowl Hand: {player.bowl_hand}\n"
            f"🌀 Bowl Style: {player.bowl_style}\n"
            f"🌍 Country: {player.country}\n"
            f"📋 Version: {player.version}\n\n"
            f"📊 <b>Batting Stats:</b>\n"
            f"• Bat Rating: {player.bat_rating}\n"
            f"• Career Runs: {player.runs:,}\n"
            f"• Average: {player.bat_avg:.1f}\n"
            f"• Strike Rate: {player.strike_rate:.1f}\n"
            f"• Centuries: {player.centuries}\n\n"
            f"📊 <b>Bowling Stats:</b>\n"
            f"• Bowl Rating: {player.bowl_rating}\n"
            f"• Average: {player.bowl_avg:.1f}\n"
            f"• Economy: {player.economy:.1f}\n"
            f"• Career Wickets: {player.wickets:,}\n\n"
            f"💰 <b>Buy Value:</b> {buy_val:,} 🪙\n"
            f"💸 <b>Sell Value:</b> {sell_val:,} 🪙\n"
            f"📅 <b>Acquired:</b> {acquired_str}"
        )

        card_bytes = generate_card(player)
        if card_bytes:
            await update.message.reply_photo(
                photo=io.BytesIO(card_bytes),
                caption=text,
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(text, parse_mode="HTML")

        logger.info(f"PlayerInfo: user {tg_user.id} viewed {player.name}")

    except Exception:
        logger.exception(f"PlayerInfo error for user {tg_user.id}")
        await update.message.reply_text("⚠️ Database error. Please try again later.")
    finally:
        session.close()

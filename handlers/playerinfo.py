"""Handler for /playerinfo [name] — simplified display."""

import io
import logging
from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import User, Player, UserRoster
from services.card_generator import generate_card
from services.card_text import format_player_card

logger = logging.getLogger(__name__)


async def playerinfo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user

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

        player = session.query(Player).filter(Player.name.ilike(f"%{search_name}%")).first()
        if not player:
            await update.message.reply_text(f"❌ Player not found: {search_name}")
            return

        owned = (
            session.query(UserRoster)
            .filter(UserRoster.user_id == user.id, UserRoster.player_id == player.id)
            .first()
        )
        acq_date = owned.acquired_date if owned else None

        text = format_player_card(player, acq_date)

        card_bytes = generate_card(player)
        if card_bytes:
            await update.message.reply_photo(
                photo=io.BytesIO(card_bytes), caption=text, parse_mode="HTML",
            )
        else:
            await update.message.reply_text(text, parse_mode="HTML")

    except Exception:
        logger.exception(f"PlayerInfo error for {tg_user.id}")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()

"""Admin bot command to manually seed a player's card file_id.

Usage (bot private chat, admin only):
    /setcardid <player name> | <telegram_file_id>
    /setcardid <player name> <telegram_file_id>

The file_id must be a Telegram photo file_id (starts with AgAC...).
After setting, the next /playerinfo or /buypl for that player will
use this file_id directly instead of generating or uploading the card.

To clear a cached file_id:
    /setcardid <player name> | clear
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import Player

logger = logging.getLogger(__name__)


async def setcardid_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from handlers.forward_broadcast import is_forward_admin
    user = update.effective_user
    if not is_forward_admin(user.id if user else None):
        await update.message.reply_text("❌ Admin only.")
        return

    raw = " ".join(context.args).strip() if context.args else ""
    if not raw:
        await update.message.reply_text(
            "Usage: /setcardid <player name> | <file_id>\n"
            "Use 'clear' as file_id to remove the cached value.\n"
            "Example: /setcardid Virat Kohli | AgACAgU..."
        )
        return

    # Accept either pipe-separated or last-token-as-file_id formats.
    if "|" in raw:
        name_part, _, fid_part = raw.partition("|")
        player_name = name_part.strip()
        file_id = fid_part.strip()
    else:
        tokens = raw.split()
        # Last token is the file_id (file_ids have no spaces)
        file_id = tokens[-1]
        player_name = " ".join(tokens[:-1]).strip()

    if not player_name or not file_id:
        await update.message.reply_text("❌ Could not parse player name or file_id.")
        return

    clearing = file_id.lower() == "clear"

    session = get_session()
    try:
        player = (
            session.query(Player)
            .filter(Player.name.ilike(f"%{player_name}%"), Player.is_active == True)
            .order_by(Player.rating.desc())
            .first()
        )
        if not player:
            await update.message.reply_text(f"❌ No active player found matching '{player_name}'.")
            return

        if clearing:
            player.card_file_id = None
            session.commit()
            await update.message.reply_text(
                f"✅ Cleared card_file_id for <b>{player.name}</b> (ID {player.id}).",
                parse_mode="HTML",
            )
        else:
            player.card_file_id = file_id
            session.commit()
            await update.message.reply_text(
                f"✅ Set card_file_id for <b>{player.name}</b> (ID {player.id}).\n"
                f"Next /playerinfo or /buypl will use this file_id directly.",
                parse_mode="HTML",
            )
    except Exception:
        session.rollback()
        logger.exception("setcardid_handler failed")
        await update.message.reply_text("⚠️ Error. Check logs.")
    finally:
        session.close()

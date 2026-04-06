"""Handler for /debut command."""

import logging
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import User, UserRoster, UserStats
from services.player_service import get_players_for_debut
from config import DEBUT_COINS, DEBUT_GEMS

logger = logging.getLogger(__name__)


async def debut_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    logger.info(f"/debut from user {tg_user.id} ({tg_user.username})")

    session = get_session()
    try:
        existing = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if existing:
            await update.message.reply_text(
                "⚠️ You've already debuted! Use /myroster to see your team."
            )
            return

        user = User(
            telegram_id=tg_user.id,
            username=tg_user.username or "",
            first_name=tg_user.first_name or "",
            total_coins=DEBUT_COINS,
            total_gems=DEBUT_GEMS,
            roster_count=0,
        )
        session.add(user)
        session.flush()

        stats = UserStats(user_id=user.id)
        session.add(stats)

        players = get_players_for_debut(session)
        if not players:
            await update.message.reply_text(
                "⚠️ No players available in the database. Please contact admin."
            )
            session.rollback()
            return

        for p in players:
            entry = UserRoster(
                user_id=user.id, player_id=p.id, acquired_date=datetime.utcnow(),
            )
            session.add(entry)

        user.roster_count = len(players)
        session.commit()

        lines = []
        for i, p in enumerate(players, 1):
            lines.append(f"  {i}. {p.name} - {p.rating} OVR | {p.category}")

        text = (
            "🎉 <b>Welcome to Cricket Bot!</b>\n"
            "✅ Your debut is complete!\n"
            f"✅ You received {len(players)} starting players\n\n"
            + "\n".join(lines) + "\n\n"
            f"📊 Your Roster: {len(players)}/25 players\n"
            f"💰 Coins: {DEBUT_COINS:,}\n"
            f"💎 Gems: {DEBUT_GEMS}\n\n"
            "<b>Commands:</b>\n"
            "/claim - Get 1 player + 500 coins (hourly)\n"
            "/myroster - View your players\n"
            "/playerinfo [name] - Player details\n"
            "/daily - Daily reward (24h cooldown)\n"
            "/gspin - Spin wheel (8h cooldown)"
        )
        await update.message.reply_text(text, parse_mode="HTML")
        logger.info(f"Debut complete for user {tg_user.id}, {len(players)} players assigned")

    except Exception:
        session.rollback()
        logger.exception(f"Debut error for user {tg_user.id}")
        await update.message.reply_text("⚠️ Database error. Please try again later.")
    finally:
        session.close()

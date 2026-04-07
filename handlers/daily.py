"""Handler for /daily command — coins + 2 players + streak."""

import logging
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import User, UserRoster, UserStats
from services.player_service import get_random_player_by_rarity, get_random_player_by_rating_range
from services.cooldown_service import check_cooldown, format_remaining
from services.streak_service import update_streak
from config import DAILY_COOLDOWN, DAILY_COINS, STREAK_MILESTONE
from services.activity_service import log_activity

logger = logging.getLogger(__name__)


async def daily_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    logger.info(f"/daily from user {tg_user.id}")

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        stats = session.query(UserStats).filter(UserStats.user_id == user.id).first()
        ready, remaining = check_cooldown(stats, "last_daily", DAILY_COOLDOWN)
        if not ready:
            await update.message.reply_text(
                f"⏳ Daily on cooldown. Try again in <b>{format_remaining(remaining)}</b>",
                parse_mode="HTML",
            )
            return

        # Update streak BEFORE setting last_daily
        streak_count, milestone = update_streak(stats)

        # Award coins
        user.total_coins += DAILY_COINS

        # Generate 2 random players
        players = []
        for _ in range(2):
            p = get_random_player_by_rarity(session)
            if p:
                players.append(p)
                entry = UserRoster(
                    user_id=user.id,
                    player_id=p.id,
                    acquired_date=datetime.utcnow(),
                )
                session.add(entry)
                user.roster_count += 1

        # Milestone bonus
        milestone_player = None
        if milestone:
            milestone_player = get_random_player_by_rating_range(session, 81, 85)
            if milestone_player:
                entry = UserRoster(
                    user_id=user.id,
                    player_id=milestone_player.id,
                    acquired_date=datetime.utcnow(),
                )
                session.add(entry)
                user.roster_count += 1

        stats.last_daily = datetime.utcnow()
        pnames = ', '.join(p.name for p in players)
        log_activity(session, user.id, 'daily', f'Daily: +{DAILY_COINS} coins, players: {pnames}, streak {streak_count}', coins_change=DAILY_COINS)
        session.commit()

        # Build message
        player_lines = ""
        for p in players:
            player_lines += f"✅ {p.name} - {p.rating} OVR\n"

        remaining_days = STREAK_MILESTONE - streak_count if streak_count > 0 else STREAK_MILESTONE

        text = (
            "📅 <b>Daily Reward Claimed!</b>\n\n"
            f"✅ +{DAILY_COINS:,} coins\n"
            f"{player_lines}\n"
            f"📊 <b>Streak:</b> {streak_count}/{STREAK_MILESTONE}\n"
            f"⏳ {remaining_days} days until 81-85 OVR bonus card"
        )

        if milestone and milestone_player:
            text += (
                f"\n\n🎉 <b>MILESTONE REACHED!</b>\n"
                f"🏆 {milestone_player.name} - {milestone_player.rating} OVR (Streak Bonus)\n"
                f"📊 Streak resets to 1"
            )

        await update.message.reply_text(text, parse_mode="HTML")
        logger.info(f"Daily: user {tg_user.id}, streak={streak_count}, milestone={milestone}")

    except Exception:
        session.rollback()
        logger.exception(f"Daily error for user {tg_user.id}")
        await update.message.reply_text("⚠️ Database error. Please try again later.")
    finally:
        session.close()

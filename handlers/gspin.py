"""Handler for /gspin command — spin wheel with 5 outcomes."""

import random
import logging
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import User, UserRoster, UserStats
from services.player_service import get_random_player_by_rating_range
from services.cooldown_service import check_cooldown, format_remaining
from config import GSPIN_COOLDOWN, GSPIN_OUTCOMES, GSPIN_EMOJIS, GSPIN_NAMES
from services.activity_service import log_activity

logger = logging.getLogger(__name__)


async def gspin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    logger.info(f"/gspin from user {tg_user.id}")

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        stats = session.query(UserStats).filter(UserStats.user_id == user.id).first()
        ready, remaining = check_cooldown(stats, "last_gspin", GSPIN_COOLDOWN)
        if not ready:
            await update.message.reply_text(
                f"⏳ GSpin on cooldown. Try again in <b>{format_remaining(remaining)}</b>",
                parse_mode="HTML",
            )
            return

        # Spin the wheel
        roll = random.random()
        colour = outcome_type = None
        outcome_range = (0, 0)
        for threshold, col, otype, orange in GSPIN_OUTCOMES:
            if roll <= threshold:
                colour, outcome_type, outcome_range = col, otype, orange
                break

        emoji = GSPIN_EMOJIS[colour]
        colour_name = GSPIN_NAMES[colour]
        reward_lines = ""

        if outcome_type == "coins":
            amount = random.randint(outcome_range[0], outcome_range[1])
            user.total_coins += amount
            reward_lines = f"CONGRATULATIONS YOU GET {amount:,} COINS!\n💰 +{amount:,} coins added"

        elif outcome_type == "gems":
            amount = random.randint(outcome_range[0], outcome_range[1])
            user.total_gems += amount
            reward_lines = f"CONGRATULATIONS YOU GET GEMS!\n💎 +{amount} gems added"

        elif outcome_type == "player":
            low, high = outcome_range
            player = get_random_player_by_rating_range(session, low, high)
            if player:
                entry = UserRoster(
                    user_id=user.id,
                    player_id=player.id,
                    acquired_date=datetime.utcnow(),
                )
                session.add(entry)
                user.roster_count += 1
                reward_lines = (
                    f"CONGRATULATIONS YOU GET NEW PLAYER!\n"
                    f"🎉 {player.name} - {player.rating} OVR\n"
                    f"🎯 {player.category} | {player.country}"
                )
            else:
                amount = random.randint(5000, 10000)
                user.total_coins += amount
                reward_lines = f"No players in range — awarded {amount:,} coins instead!\n💰 +{amount:,} coins added"

        stats.last_gspin = datetime.utcnow()
        log_activity(session, user.id, 'gspin', f'GSpin: {colour} → {outcome_type}')
        session.commit()

        text = (
            "🎡 <b>GSPIN Wheel Result!</b>\n\n"
            f"{emoji} <b>{colour_name}</b>\n\n"
            f"{reward_lines}\n\n"
            "✅ Reward added to your account!"
        )
        await update.message.reply_text(text, parse_mode="HTML")
        logger.info(f"GSpin: user {tg_user.id} → {colour} ({outcome_type})")

    except Exception:
        session.rollback()
        logger.exception(f"GSpin error for user {tg_user.id}")
        await update.message.reply_text("⚠️ Database error. Please try again later.")
    finally:
        session.close()

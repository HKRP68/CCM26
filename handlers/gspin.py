"""Handler for /gspin — button-first, then result."""

import random
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, UserRoster, UserStats
from services.player_service import get_random_player_by_rating_range
from services.cooldown_service import check_cooldown, format_remaining
from services.card_text import format_player_card
from services.activity_service import log_activity
from config import (GSPIN_COOLDOWN, GSPIN_OUTCOMES, GSPIN_EMOJIS, GSPIN_NAMES,
                    MAX_ROSTER, get_sell_value)

logger = logging.getLogger(__name__)


async def gspin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
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
                parse_mode="HTML")
            return

        text = (
            "🎡 <b>GSPIN Wheel</b>\n\n"
            "🟥 <b>Red:</b> 5,000 to 10,000 coins\n"
            "🟨 <b>Yellow:</b> Random 79-85 OVR card\n"
            "🟦 <b>Blue:</b> 10-500 Gems\n"
            "🟩 <b>Green:</b> Random 85-90 OVR card\n\n"
            "Tap to spin!"
        )

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🎰 Spin the Wheel", callback_data=f"gspin_{user.id}")
        ]])

        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

    except Exception:
        logger.exception("GSpin error")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


async def gspin_spin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tg_user = query.from_user

    user_id = int(query.data.split("_")[1])

    session = get_session()
    try:
        user = session.query(User).get(user_id)
        if not user or user.telegram_id != tg_user.id:
            return

        stats = session.query(UserStats).filter(UserStats.user_id == user.id).first()
        ready, _ = check_cooldown(stats, "last_gspin", GSPIN_COOLDOWN)
        if not ready:
            await query.edit_message_text("⏳ Already spun!")
            return

        # Spin
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
                if user.roster_count < MAX_ROSTER:
                    entry = UserRoster(user_id=user.id, player_id=player.id,
                                       order_position=user.roster_count + 1,
                                       acquired_date=datetime.utcnow())
                    session.add(entry)
                    user.roster_count += 1
                    reward_lines = (
                        f"CONGRATULATIONS YOU GET NEW PLAYER!\n"
                        f"🎉 {player.name} - {player.rating} OVR\n"
                        f"🎯 {player.category} | {player.country}\n"
                        f"✅ Added to squad ({user.roster_count}/25)"
                    )
                else:
                    # Squad full — show claim buttons
                    sell_val = get_sell_value(player.rating)
                    reward_lines = (
                        f"CONGRATULATIONS YOU GET NEW PLAYER!\n"
                        f"🎉 {player.name} - {player.rating} OVR\n"
                        f"⚠️ Squad full — choose below"
                    )
                    stats.last_gspin = datetime.utcnow()
                    log_activity(session, user.id, "gspin", f"GSpin: {colour} → {player.name}")
                    session.commit()

                    text = (
                        f"🎡 <b>GSPIN Wheel Result!</b>\n\n"
                        f"{emoji} <b>{colour_name}</b>\n\n"
                        f"{reward_lines}\n\n"
                        "✅ Reward added!"
                    )
                    await query.edit_message_text(text, parse_mode="HTML")

                    # Send claim card for the player
                    claim_text = f"⚠️ <b>Squad full — decide:</b>\n\n" + format_player_card(player)
                    buttons = [
                        [InlineKeyboardButton("🔴 Release",
                                              callback_data=f"release_{player.id}_{user.id}_{sell_val}")],
                        [InlineKeyboardButton("⚪ Replace",
                                              callback_data=f"replace_{player.id}_{user.id}_{sell_val}")],
                    ]
                    await query.message.reply_text(claim_text, parse_mode="HTML",
                                                   reply_markup=InlineKeyboardMarkup(buttons))
                    return
            else:
                amount = random.randint(5000, 10000)
                user.total_coins += amount
                reward_lines = f"No players in range — {amount:,} coins instead!\n💰 +{amount:,}"

        stats.last_gspin = datetime.utcnow()
        log_activity(session, user.id, "gspin", f"GSpin: {colour} → {outcome_type}")
        session.commit()

        text = (
            f"🎡 <b>GSPIN Wheel Result!</b>\n\n"
            f"{emoji} <b>{colour_name}</b>\n\n"
            f"{reward_lines}\n\n"
            "✅ Reward added to your account!"
        )
        await query.edit_message_text(text, parse_mode="HTML")

    except Exception:
        session.rollback()
        logger.exception("GSpin spin error")
    finally:
        session.close()

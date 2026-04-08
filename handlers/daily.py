"""Handler for /daily — button first, claim flow for players if squad full."""

import io
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, Player, UserRoster, UserStats
from services.player_service import get_random_player_by_rarity, get_random_player_by_rating_range
from services.cooldown_service import check_cooldown, format_remaining
from services.streak_service import update_streak
from services.card_text import format_player_card
from services.activity_service import log_activity
from config import DAILY_COOLDOWN, DAILY_COINS, STREAK_MILESTONE, MAX_ROSTER, get_sell_value

logger = logging.getLogger(__name__)


async def daily_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
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
                parse_mode="HTML")
            return

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🎁 Claim Daily Reward",
                                 callback_data=f"dailyclaim_{user.id}")
        ]])

        await update.message.reply_text(
            "📅 <b>Daily Reward Available!</b>\n\n"
            "Tap below to claim your reward:\n"
            "+5,000 coins\n"
            "+2 Players\n"
            "+1 Streak\n",
            parse_mode="HTML", reply_markup=keyboard)

    except Exception:
        logger.exception(f"Daily error")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


async def daily_claim_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        ready, _ = check_cooldown(stats, "last_daily", DAILY_COOLDOWN)
        if not ready:
            await query.edit_message_text("⏳ Already claimed today!")
            return

        # Update streak
        streak_count, milestone = update_streak(stats)

        # Award coins always
        user.total_coins += DAILY_COINS

        # Generate 2 players
        players = []
        for _ in range(2):
            p = get_random_player_by_rarity(session)
            if p:
                players.append(p)

        # Milestone bonus
        milestone_player = None
        if milestone:
            milestone_player = get_random_player_by_rating_range(session, 81, 85)

        stats.last_daily = datetime.utcnow()

        remaining_days = STREAK_MILESTONE - streak_count if streak_count > 0 else STREAK_MILESTONE

        # Build result text
        lines = [
            f"📅 <b>Daily Reward Claimed!</b>\n",
            f"✅ +{DAILY_COINS:,} coins",
            f"📊 Streak: {streak_count}/{STREAK_MILESTONE}",
            f"⏳ {remaining_days} days until bonus card\n",
        ]

        if milestone and milestone_player:
            lines.append(f"🎉 <b>MILESTONE!</b> 🏆 {milestone_player.name} - {milestone_player.rating} OVR\n")

        # Add players to roster if space, otherwise queue for claim flow
        players_to_claim = []
        all_players = players + ([milestone_player] if milestone_player else [])

        for p in all_players:
            if not p:
                continue
            if user.roster_count < MAX_ROSTER:
                entry = UserRoster(user_id=user.id, player_id=p.id,
                                   order_position=user.roster_count + 1,
                                   acquired_date=datetime.utcnow())
                session.add(entry)
                user.roster_count += 1
                lines.append(f"✅ {p.name} - {p.rating} OVR added to squad")
            else:
                players_to_claim.append(p)
                lines.append(f"⚠️ {p.name} - {p.rating} OVR (squad full — choose below)")

        pnames = ", ".join(p.name for p in all_players if p)
        log_activity(session, user.id, "daily",
                     f"Daily: +{DAILY_COINS} coins, players: {pnames}, streak {streak_count}",
                     coins_change=DAILY_COINS)
        session.commit()

        await query.edit_message_text("\n".join(lines), parse_mode="HTML")

        # For each player that couldn't auto-add (squad full), send a claim card
        for p in players_to_claim:
            sell_val = get_sell_value(p.rating)
            text = (
                f"⚠️ <b>Squad full — decide for this player:</b>\n\n"
                + format_player_card(p)
            )
            buttons = [
                [InlineKeyboardButton("🔴 Release", callback_data=f"release_{p.id}_{user.id}_{sell_val}")],
                [InlineKeyboardButton("⚪ Replace", callback_data=f"replace_{p.id}_{user.id}_{sell_val}")],
            ]
            await query.message.reply_text(text, parse_mode="HTML",
                                           reply_markup=InlineKeyboardMarkup(buttons))

    except Exception:
        session.rollback()
        logger.exception("Daily claim error")
    finally:
        session.close()

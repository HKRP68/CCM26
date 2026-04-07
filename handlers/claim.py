"""Handler for /claim command with Retain / Release buttons."""

import io
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, Player, UserRoster, UserStats
from services.player_service import get_random_player_by_rarity, get_player_values
from services.cooldown_service import check_cooldown, format_remaining
from services.card_generator import generate_card
from config import CLAIM_COOLDOWN, CLAIM_COINS, MAX_ROSTER
from services.activity_service import log_activity

logger = logging.getLogger(__name__)


async def claim_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    logger.info(f"/claim from user {tg_user.id}")

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        stats = session.query(UserStats).filter(UserStats.user_id == user.id).first()
        ready, remaining = check_cooldown(stats, "last_claim", CLAIM_COOLDOWN)
        if not ready:
            await update.message.reply_text(
                f"⏳ Claim on cooldown. Try again in <b>{format_remaining(remaining)}</b>",
                parse_mode="HTML",
            )
            return

        player = get_random_player_by_rarity(session)
        if not player:
            await update.message.reply_text("⚠️ No players available. Contact admin.")
            return

        buy_val, sell_val = get_player_values(player.rating)

        # Award claim coins
        user.total_coins += CLAIM_COINS
        stats.last_claim = datetime.utcnow()
        log_activity(session, user.id, 'claim', f'Claimed {player.name} ({player.rating} OVR)', coins_change=CLAIM_COINS, player_name=player.name, player_rating=player.rating)
        session.commit()

        text = (
            "🎉 <b>New Player, Retain or Release!</b>\n\n"
            f"📛 {player.name}\n"
            f"⭐ <b>Rating:</b> {player.rating} OVR\n"
            f"🎯 <b>Category:</b> {player.category}\n"
            f"🏏 <b>Bat Hand:</b> {player.bat_hand}\n"
            f"🎳 <b>Bowl Hand:</b> {player.bowl_hand}\n"
            f"🌀 <b>Bowl Style:</b> {player.bowl_style}\n"
            f"💰 <b>Card Value:</b> {buy_val:,} 🪙\n"
            f"💸 <b>Sell Value:</b> {sell_val:,} 🪙\n\n"
            f"💰 +{CLAIM_COINS:,} coins added!"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Retain", callback_data=f"retain_{player.id}_{user.id}"),
                InlineKeyboardButton("❌ Release", callback_data=f"release_{player.id}_{user.id}_{sell_val}"),
            ]
        ])

        card_bytes = generate_card(player)
        if card_bytes:
            await update.message.reply_photo(
                photo=io.BytesIO(card_bytes),
                caption=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        else:
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

        logger.info(f"Claim: user {tg_user.id} got {player.name} ({player.rating})")

    except Exception:
        session.rollback()
        logger.exception(f"Claim error for user {tg_user.id}")
        await update.message.reply_text("⚠️ Database error. Please try again later.")
    finally:
        session.close()


async def retain_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the Retain button press."""
    query = update.callback_query
    await query.answer()
    tg_user = query.from_user

    parts = query.data.split("_")
    player_id = int(parts[1])
    owner_user_id = int(parts[2])

    session = get_session()
    try:
        user = session.query(User).filter(User.id == owner_user_id).first()
        if not user or user.telegram_id != tg_user.id:
            await query.edit_message_reply_markup(reply_markup=None)
            return

        if user.roster_count >= MAX_ROSTER:
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await query.message.reply_text("❌ Your roster is full! Release players to claim more.")
            return

        # Double-click protection
        exists = (
            session.query(UserRoster)
            .filter(UserRoster.user_id == user.id, UserRoster.player_id == player_id)
            .first()
        )

        if not exists:
            entry = UserRoster(
                user_id=user.id, player_id=player_id, acquired_date=datetime.utcnow(),
            )
            session.add(entry)
            user.roster_count += 1
            session.commit()

        player = session.query(Player).get(player_id)
        name = player.name if player else "Unknown"
        username = tg_user.username or tg_user.first_name

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        log_activity(session, user.id, 'retain', f'Retained {name}', player_name=name)
        session.commit()
        await query.message.reply_text(f"✅ {name} Added to @{username}'s roster!")
        logger.info(f"Retain: user {tg_user.id} kept {name}")

    except Exception:
        session.rollback()
        logger.exception(f"Retain error for user {tg_user.id}")
    finally:
        session.close()


async def release_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the Release button press."""
    query = update.callback_query
    await query.answer()
    tg_user = query.from_user

    parts = query.data.split("_")
    player_id = int(parts[1])
    owner_user_id = int(parts[2])
    sell_val = int(parts[3])

    session = get_session()
    try:
        user = session.query(User).filter(User.id == owner_user_id).first()
        if not user or user.telegram_id != tg_user.id:
            await query.edit_message_reply_markup(reply_markup=None)
            return

        user.total_coins += sell_val
        session.commit()

        player = session.query(Player).get(player_id)
        name = player.name if player else "Unknown"
        username = tg_user.username or tg_user.first_name

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        log_activity(session, user.id, 'release', f'Released {name} for {sell_val:,} coins', coins_change=sell_val, player_name=name)
        session.commit()
        await query.message.reply_text(
            f"🔄 {name} Released by @{username}\n💰 +{sell_val:,} coins added to purse"
        )
        logger.info(f"Release: user {tg_user.id} released {name}, +{sell_val} coins")

    except Exception:
        session.rollback()
        logger.exception(f"Release error for user {tg_user.id}")
    finally:
        session.close()

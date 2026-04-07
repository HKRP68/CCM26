"""Handler for /claim with 60s auto-retain/release timer."""

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
from config import CLAIM_COOLDOWN, CLAIM_COINS, MAX_ROSTER, get_sell_value
from services.activity_service import log_activity
from services.card_text import format_player_card

logger = logging.getLogger(__name__)

AUTO_TIMEOUT = 60  # seconds


async def _auto_resolve(context: ContextTypes.DEFAULT_TYPE):
    """Called after 60s if user hasn't clicked Retain/Release."""
    data = context.job.data
    player_id = data["player_id"]
    user_id = data["user_id"]
    sell_val = data["sell_val"]
    chat_id = data["chat_id"]
    message_id = data["message_id"]

    session = get_session()
    try:
        user = session.query(User).get(user_id)
        if not user:
            return
        player = session.query(Player).get(player_id)
        name = player.name if player else "Unknown"

        if user.roster_count < MAX_ROSTER:
            # Auto-retain
            entry = UserRoster(
                user_id=user.id, player_id=player_id,
                order_position=user.roster_count + 1,
                acquired_date=datetime.utcnow(),
            )
            session.add(entry)
            user.roster_count += 1
            log_activity(session, user.id, "auto_retain",
                         f"Auto-retained {name} (60s timeout)",
                         player_name=name, player_rating=player.rating if player else 0)
            session.commit()
            result_text = f"⏰ Time's up! {name} auto-added to your roster."
        else:
            # Auto-release
            user.total_coins += sell_val
            log_activity(session, user.id, "auto_release",
                         f"Auto-released {name} for {sell_val:,} (roster full, 60s timeout)",
                         coins_change=sell_val, player_name=name,
                         player_rating=player.rating if player else 0)
            session.commit()
            result_text = f"⏰ Time's up! Roster full — {name} auto-released.\n💰 +{sell_val:,} coins"

        # Remove buttons
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=None
            )
        except Exception:
            pass

        await context.bot.send_message(chat_id=chat_id, text=result_text)

    except Exception:
        session.rollback()
        logger.exception("Auto-resolve error")
    finally:
        session.close()


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

        user.total_coins += CLAIM_COINS
        stats.last_claim = datetime.utcnow()
        log_activity(session, user.id, "claim",
                     f"Claimed {player.name} ({player.rating} OVR)",
                     coins_change=CLAIM_COINS,
                     player_name=player.name, player_rating=player.rating)
        session.commit()

        text = (
            "🎉 <b>New Player, Retain or Release!</b>\n\n"
            + format_player_card(player) + "\n\n"
            f"💰 +{CLAIM_COINS:,} coins added!\n"
            f"⏰ Auto-decides in {AUTO_TIMEOUT}s"
        )

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Retain", callback_data=f"retain_{player.id}_{user.id}"),
            InlineKeyboardButton("❌ Release", callback_data=f"release_{player.id}_{user.id}_{sell_val}"),
        ]])

        card_bytes = generate_card(player)
        if card_bytes:
            msg = await update.message.reply_photo(
                photo=io.BytesIO(card_bytes), caption=text,
                parse_mode="HTML", reply_markup=keyboard,
            )
        else:
            msg = await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

        # Schedule auto-resolve after 60s
        job_name = f"claim_auto_{user.id}_{player.id}"
        # Cancel any existing auto-resolve for this user
        for job in context.job_queue.get_jobs_by_name(f"claim_auto_{user.id}"):
            job.schedule_removal()

        context.job_queue.run_once(
            _auto_resolve, AUTO_TIMEOUT, name=job_name,
            data={
                "player_id": player.id, "user_id": user.id,
                "sell_val": sell_val, "chat_id": msg.chat_id,
                "message_id": msg.message_id,
            },
        )

    except Exception:
        session.rollback()
        logger.exception(f"Claim error for user {tg_user.id}")
        await update.message.reply_text("⚠️ Database error. Please try again later.")
    finally:
        session.close()


async def retain_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tg_user = query.from_user

    parts = query.data.split("_")
    player_id = int(parts[1])
    owner_user_id = int(parts[2])

    session = get_session()
    try:
        user = session.query(User).get(owner_user_id)
        if not user or user.telegram_id != tg_user.id:
            await query.edit_message_reply_markup(reply_markup=None)
            return

        if user.roster_count >= MAX_ROSTER:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("❌ Roster full! Release players first.")
            return

        exists = session.query(UserRoster).filter(
            UserRoster.user_id == user.id, UserRoster.player_id == player_id
        ).first()

        if not exists:
            entry = UserRoster(
                user_id=user.id, player_id=player_id,
                order_position=user.roster_count + 1,
                acquired_date=datetime.utcnow(),
            )
            session.add(entry)
            user.roster_count += 1

        player = session.query(Player).get(player_id)
        name = player.name if player else "Unknown"

        log_activity(session, user.id, "retain", f"Retained {name}", player_name=name)
        session.commit()

        await query.edit_message_reply_markup(reply_markup=None)

        # Cancel auto-resolve job
        for job in context.job_queue.get_jobs_by_name(f"claim_auto_{user.id}"):
            job.schedule_removal()

        username = tg_user.username or tg_user.first_name
        await query.message.reply_text(f"✅ {name} Added to @{username}'s roster!")

    except Exception:
        session.rollback()
        logger.exception(f"Retain error")
    finally:
        session.close()


async def release_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tg_user = query.from_user

    parts = query.data.split("_")
    player_id = int(parts[1])
    owner_user_id = int(parts[2])
    sell_val = int(parts[3])

    session = get_session()
    try:
        user = session.query(User).get(owner_user_id)
        if not user or user.telegram_id != tg_user.id:
            await query.edit_message_reply_markup(reply_markup=None)
            return

        user.total_coins += sell_val

        player = session.query(Player).get(player_id)
        name = player.name if player else "Unknown"

        log_activity(session, user.id, "release",
                     f"Released {name} for {sell_val:,}",
                     coins_change=sell_val, player_name=name)
        session.commit()

        await query.edit_message_reply_markup(reply_markup=None)

        # Cancel auto-resolve job
        for job in context.job_queue.get_jobs_by_name(f"claim_auto_{user.id}"):
            job.schedule_removal()

        username = tg_user.username or tg_user.first_name
        await query.message.reply_text(
            f"🔄 {name} Released by @{username}\n💰 +{sell_val:,} coins added to purse"
        )

    except Exception:
        session.rollback()
        logger.exception(f"Release error")
    finally:
        session.close()

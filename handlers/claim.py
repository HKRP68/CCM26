"""Handler for /claim — robust version with separated concerns."""

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
from services.card_text import format_player_card
from services.activity_service import log_activity
from config import CLAIM_COOLDOWN, CLAIM_COINS, MAX_ROSTER, get_sell_value

logger = logging.getLogger(__name__)
AUTO_TIMEOUT = 60
_processed = set()


def _is_done(key):
    if key in _processed:
        return True
    _processed.add(key)
    if len(_processed) > 5000:
        for k in list(_processed)[:2500]:
            _processed.discard(k)
    return False


def _cancel_timer(context, user_id):
    try:
        if context.job_queue:
            for job in context.job_queue.get_jobs_by_name(f"claim_{user_id}"):
                job.schedule_removal()
    except Exception:
        pass


async def _remove_buttons(query):
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


async def _send(context, chat_id, text):
    try:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    except Exception as e:
        logger.exception(f"Failed to send message to {chat_id}")


# ── Auto-release after 60s ──────────────────────────────────────────

async def _auto_release(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data
    key = f"claim_{d['user_id']}_{d['player_id']}"
    if _is_done(key):
        return

    session = get_session()
    try:
        user = session.query(User).get(d["user_id"])
        player = session.query(Player).get(d["player_id"])
        if not user or not player:
            return
        sell_val = d["sell_val"]
        user.total_coins += sell_val
        log_activity(session, user.id, "auto_release",
                     f"Auto-released {player.name} (timeout)", coins_change=sell_val,
                     player_name=player.name, player_rating=player.rating)
        session.commit()

        try:
            await context.bot.edit_message_reply_markup(
                chat_id=d["chat_id"], message_id=d["message_id"], reply_markup=None)
        except Exception:
            pass

        await _send(context, d["chat_id"],
                    f"⌛ <b>Time Expired</b>\n\n"
                    f"🏏 {player.name} has been released.\n"
                    f"{player.name}, {player.rating} OVR\n"
                    f"💰 +{sell_val:,} coins added")
    except Exception:
        session.rollback()
        logger.exception("Auto-release error")
    finally:
        session.close()


# ── /claim ───────────────────────────────────────────────────────────

async def claim_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    chat_id = update.effective_chat.id

    # ── Step 1: DB work ──────────────────────────────────────────
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
                parse_mode="HTML")
            return

        player = get_random_player_by_rarity(session)
        if not player:
            await update.message.reply_text("⚠️ No players available.")
            return

        # Save player info before committing
        player_id = player.id
        player_name = player.name
        player_rating = player.rating
        player_category = player.category
        player_bat_hand = player.bat_hand
        player_bowl_hand = player.bowl_hand
        player_bowl_style = player.bowl_style
        player_bat_rating = player.bat_rating
        player_bowl_rating = player.bowl_rating
        player_country = player.country
        player_version = player.version
        roster_count = user.roster_count
        user_id = user.id

        buy_val, sell_val = get_player_values(player_rating)

        user.total_coins += CLAIM_COINS
        stats.last_claim = datetime.utcnow()
        log_activity(session, user.id, "claim", f"Claimed {player_name} ({player_rating})",
                     coins_change=CLAIM_COINS, player_name=player_name, player_rating=player_rating)
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Claim DB error")
        await update.message.reply_text("⚠️ Database error. Try again.")
        return
    finally:
        session.close()

    # ── Step 2: Build text and buttons ───────────────────────────
    text = format_player_card(player) + f"\n\n💰 +{CLAIM_COINS:,} coins added!"

    buttons = [
        [InlineKeyboardButton("🟢 Retain", callback_data=f"retain_{player_id}_{user_id}_{sell_val}")],
        [InlineKeyboardButton("🔴 Release", callback_data=f"release_{player_id}_{user_id}_{sell_val}")],
    ]
    if roster_count >= MAX_ROSTER:
        buttons.append([
            InlineKeyboardButton("⚪ Replace", callback_data=f"replace_{player_id}_{user_id}_{sell_val}")
        ])
    keyboard = InlineKeyboardMarkup(buttons)

    # ── Step 3: Send message ────────────────────────────────────
    msg = None
    try:
        card_bytes = generate_card(player)
        if card_bytes:
            # Send photo with SHORT caption, then buttons as separate message
            await update.message.reply_photo(
                photo=io.BytesIO(card_bytes),
                caption=f"📛 <b>{player_name}</b> — {player_rating} OVR | {player_category}",
                parse_mode="HTML")
            # Send full text + buttons as text message
            msg = await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
        else:
            msg = await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception:
        logger.exception("Claim send error")
        # Try text-only fallback
        try:
            msg = await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
        except Exception:
            logger.exception("Claim text fallback also failed")
            return

    # ── Step 4: Schedule timer (optional — claim works even if this fails)
    if msg:
        try:
            _cancel_timer(context, user_id)
            if context.job_queue:
                context.job_queue.run_once(
                    _auto_release, AUTO_TIMEOUT, name=f"claim_{user_id}",
                    data={"player_id": player_id, "user_id": user_id, "sell_val": sell_val,
                          "chat_id": chat_id, "message_id": msg.message_id})
        except Exception:
            logger.warning(f"Job queue not available — no auto-release timer for user {user_id}")


# ── 🟢 Retain ────────────────────────────────────────────────────────

async def retain_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tg_user = query.from_user
    chat_id = query.message.chat_id

    parts = query.data.split("_")
    player_id, user_id = int(parts[1]), int(parts[2])
    sell_val = int(parts[3]) if len(parts) > 3 else 0

    key = f"claim_{user_id}_{player_id}"
    if _is_done(key):
        await query.answer("Already processed!")
        return
    await query.answer()
    await _remove_buttons(query)
    _cancel_timer(context, user_id)

    session = get_session()
    try:
        user = session.query(User).get(user_id)
        if not user or user.telegram_id != tg_user.id:
            return

        player = session.query(Player).get(player_id)
        if not player:
            return

        if user.roster_count >= MAX_ROSTER:
            _processed.discard(key)
            await _send(context, chat_id,
                        "❌ Your squad is full (25/25).\nUse ⚪ Replace or /releasepl <name>")
            return

        entry = UserRoster(user_id=user.id, player_id=player_id,
                           order_position=user.roster_count + 1,
                           acquired_date=datetime.utcnow())
        session.add(entry)
        user.roster_count += 1
        log_activity(session, user.id, "retain", f"Retained {player.name}", player_name=player.name)
        session.commit()

        username = tg_user.username or tg_user.first_name
        await _send(context, chat_id,
                    f"🏏 {player.name} has been added to your squad. @{username}\n\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"👤 Category: {player.category}\n"
                    f"⭐ Rating: {player.rating}\n"
                    f"📊 Bat Rating: {player.bat_rating}\n"
                    f"📊 Bowl Rating: {player.bowl_rating}\n"
                    f"🏏 Bat: {player.bat_hand}\n"
                    f"🎯 Bowl: {player.bowl_style}\n"
                    f"━━━━━━━━━━━━━━\n\n"
                    f"✅ Your Squad Size: {user.roster_count}/25")

    except Exception:
        session.rollback()
        logger.exception("Retain error")
    finally:
        session.close()


# ── 🔴 Release ───────────────────────────────────────────────────────

async def release_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tg_user = query.from_user
    chat_id = query.message.chat_id

    parts = query.data.split("_")
    player_id, user_id, sell_val = int(parts[1]), int(parts[2]), int(parts[3])

    key = f"claim_{user_id}_{player_id}"
    if _is_done(key):
        await query.answer("Already processed!")
        return
    await query.answer()
    await _remove_buttons(query)
    _cancel_timer(context, user_id)

    session = get_session()
    try:
        user = session.query(User).get(user_id)
        if not user or user.telegram_id != tg_user.id:
            return

        player = session.query(Player).get(player_id)
        name = player.name if player else "Unknown"
        rating = player.rating if player else 0
        username = tg_user.username or tg_user.first_name

        user.total_coins += sell_val
        log_activity(session, user.id, "release", f"Released {name} for {sell_val:,}",
                     coins_change=sell_val, player_name=name)
        session.commit()

        await _send(context, chat_id,
                    f"🏏 {name} has been released by @{username}\n\n"
                    f"{name}, {rating} OVR")

    except Exception:
        session.rollback()
        logger.exception("Release error")
    finally:
        session.close()


# ── ⚪ Replace ───────────────────────────────────────────────────────

async def replace_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tg_user = query.from_user

    parts = query.data.split("_")
    new_player_id, user_id, sell_val = int(parts[1]), int(parts[2]), int(parts[3])

    session = get_session()
    try:
        user = session.query(User).get(user_id)
        if not user or user.telegram_id != tg_user.id:
            return

        roster = (
            session.query(UserRoster, Player)
            .join(Player, UserRoster.player_id == Player.id)
            .filter(UserRoster.user_id == user.id)
            .order_by(Player.rating.asc())
            .limit(10)
            .all()
        )
        if not roster:
            return

        buttons = []
        for entry, player in roster:
            buttons.append([InlineKeyboardButton(
                f"🔄 {player.name} ({player.rating} OVR)",
                callback_data=f"repl_{new_player_id}_{entry.id}_{user_id}"
            )])
        buttons.append([InlineKeyboardButton(
            "❌ Cancel (Release instead)",
            callback_data=f"release_{new_player_id}_{user_id}_{sell_val}"
        )])

        try:
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))
        except Exception:
            pass

    except Exception:
        logger.exception("Replace error")
    finally:
        session.close()


async def replace_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tg_user = query.from_user
    chat_id = query.message.chat_id

    parts = query.data.split("_")
    new_player_id, old_roster_id, user_id = int(parts[1]), int(parts[2]), int(parts[3])

    key = f"repl_{user_id}_{new_player_id}_{old_roster_id}"
    if _is_done(key):
        await query.answer("Already processed!")
        return
    await query.answer()
    await _remove_buttons(query)
    _is_done(f"claim_{user_id}_{new_player_id}")
    _cancel_timer(context, user_id)

    session = get_session()
    try:
        user = session.query(User).get(user_id)
        if not user or user.telegram_id != tg_user.id:
            return

        old_entry = session.query(UserRoster).filter(
            UserRoster.id == old_roster_id, UserRoster.user_id == user.id).first()
        if not old_entry:
            await _send(context, chat_id, "❌ Player no longer in roster")
            return

        old_player = session.query(Player).get(old_entry.player_id)
        new_player = session.query(Player).get(new_player_id)
        old_name = old_player.name if old_player else "Unknown"
        new_name = new_player.name if new_player else "Unknown"

        old_entry.player_id = new_player_id
        old_entry.acquired_date = datetime.utcnow()

        log_activity(session, user.id, "replace", f"Replaced {old_name} with {new_name}",
                     player_name=new_name, player_rating=new_player.rating if new_player else 0)
        session.commit()

        await _send(context, chat_id,
                    f"🔁 <b>Player SUCCESSFULLY REPLACED!</b>\n\n"
                    f"⬅ Removed: {old_name}\n"
                    f"➡ Added: {new_name}\n\n"
                    f"✅ Squad Updated: {user.roster_count}/25")

    except Exception:
        session.rollback()
        logger.exception("Replace confirm error")
    finally:
        session.close()

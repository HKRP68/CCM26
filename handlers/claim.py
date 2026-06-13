"""Handler for /claim — keeps session open through entire flow."""

import io
import logging
from datetime import datetime
from types import SimpleNamespace
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, Player, UserRoster, UserStats
from utils.idempotency import claim_once, release
from services.player_service import get_random_player_by_rarity, get_player_values
from services.cooldown_service import check_cooldown, format_remaining
from services.card_generator import generate_card
from services.activity_service import log_activity
from services.flags import get_flag
from services.fancy_text import compact_value
from config import CLAIM_COOLDOWN, CLAIM_COINS, MAX_ROSTER, get_sell_value, get_buy_value

logger = logging.getLogger(__name__)
AUTO_TIMEOUT = 60
# Claim/retain/release/replace are TERMINAL one-shot decisions coordinated
# between the inline buttons and the 60s auto-decide timer. They share a key
# (claim_<user>_<player>) so whichever fires first blocks the rest. Guard them
# well beyond the auto-decide window (and beyond the default 30s) so a delayed
# duplicate callback can't replay a terminal decision and double-credit coins.
CLAIM_GUARD_TTL = 3600.0


def _claim_done(key):
    """True if this terminal claim decision was already taken (long-lived guard)."""
    return not claim_once(key, ttl=CLAIM_GUARD_TTL)


def _cancel_timer(context, user_id):
    try:
        if context.job_queue:
            for job in context.job_queue.get_jobs_by_name(f"claim_{user_id}"):
                job.schedule_removal()
    except Exception:
        pass


def _build_card_text(p, coin_reward=0, mention=None):
    """Build the stylised /claim caption from a dict (not ORM object)."""
    flag = get_flag(p["country"])
    buy = compact_value(get_buy_value(p["rating"]))
    sell = compact_value(get_sell_value(p["rating"]))
    bat_hand = f"{p['bat_hand']} Hand Batsman" if p.get("bat_hand") else "—"

    lines = [
        f"<blockquote>👤 <b>{p['name']}</b>  {flag}</blockquote>",
        f"⭐{p['rating']} 🏏{p['bat_rating']} 🎯{p['bowl_rating']}",
        f"| {p['category']} | {bat_hand} | {p['bowl_style']}",
        "",
        f"|💰 B:{buy} | S:{sell} 🪙",
    ]
    if coin_reward:
        lines.append(f"|💰 +{coin_reward:,} Coin Added")
    lines.append("")
    who = mention or "You"
    lines.append(f"{who} Click 🟢 to retain or 🔴 to release")
    return "\n".join(lines)


def _player_to_dict(player):
    """Convert ORM player to plain dict while session is open."""
    return {
        "id": player.id, "name": player.name, "rating": player.rating,
        "category": player.category, "country": player.country,
        "bat_hand": player.bat_hand, "bowl_hand": player.bowl_hand,
        "bowl_style": player.bowl_style, "bat_rating": player.bat_rating,
        "bowl_rating": player.bowl_rating, "version": player.version,
    }


# ── Auto decide after 60s — auto-retain if space, auto-release if full ──

async def _auto_decide(context: ContextTypes.DEFAULT_TYPE):
    """Called by job queue. If user didn't click within timeout:
       - Roster has space  → AUTO-RETAIN (add to roster, keep player)
       - Roster is full    → AUTO-RELEASE (sell back, get coins)
    """
    d = context.job.data
    key = f"claim_{d['user_id']}_{d['player_id']}"
    if _claim_done(key):
        return

    session = get_session()
    try:
        user = session.query(User).get(d["user_id"])
        player = session.query(Player).get(d["player_id"])
        if not user or not player:
            return

        sell_val = d["sell_val"]
        roster_full = user.roster_count >= MAX_ROSTER

        if not roster_full:
            # AUTO-RETAIN
            try:
                entry = UserRoster(user_id=user.id, player_id=player.id,
                                   order_position=user.roster_count + 1,
                                   acquired_date=datetime.utcnow())
                session.add(entry)
                user.roster_count += 1
                log_activity(session, user.id, "auto_retain",
                             f"Auto-retained {player.name} (timeout)",
                             player_name=player.name, player_rating=player.rating)
                try:
                    from services.quest_service import safe_track
                    safe_track(session, user.id, "claim", 1)
                except Exception:
                    pass
                session.commit()
                action_msg = (f"⏱ <b>Time Expired — Auto-Retained</b>\n\n"
                              f"🏏 {player.name} ({player.rating} OVR) added to your roster.")
            except Exception:
                session.rollback()
                logger.exception("Auto-retain failed, falling back to auto-release")
                user = session.query(User).get(d["user_id"])
                user.total_coins += sell_val
                log_activity(session, user.id, "auto_release",
                             f"Auto-released {player.name} (retain failed)",
                             coins_change=sell_val, player_name=player.name,
                             player_rating=player.rating)
                session.commit()
                action_msg = (f"⏱ <b>Time Expired — Auto-Released</b>\n\n"
                              f"🏏 {player.name} ({player.rating} OVR)\n"
                              f"💰 +{sell_val:,} coins added")
        else:
            # AUTO-RELEASE (roster full, can't retain without picking who to drop)
            user.total_coins += sell_val
            log_activity(session, user.id, "auto_release",
                         f"Auto-released {player.name} (roster full, timeout)",
                         coins_change=sell_val, player_name=player.name,
                         player_rating=player.rating)
            session.commit()
            action_msg = (f"⏱ <b>Time Expired — Roster Full</b>\n\n"
                          f"🏏 {player.name} ({player.rating} OVR) auto-released.\n"
                          f"💰 +{sell_val:,} coins added")

        # Strip buttons + post outcome
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=d["chat_id"], message_id=d["message_id"], reply_markup=None)
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=d["chat_id"], text=action_msg, parse_mode="HTML")
    except Exception:
        session.rollback()
        logger.exception("_auto_decide error")
    finally:
        session.close()


# Keep the old name pointed at the new function for any leftover references
_auto_release = _auto_decide


# ── /claim ───────────────────────────────────────────────────────────

async def claim_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    chat_id = update.effective_chat.id

    session = get_session()
    try:
        # Check enabled flag
        from services.command_config_service import (
            is_command_enabled, get_disabled_message, get_cooldown,
            get_coin_amount,
        )
        if not is_command_enabled(session, "claim"):
            await update.message.reply_text(
                get_disabled_message(session, "claim"), parse_mode="HTML")
            return

        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            from services.message_service import get_msg
            await update.message.reply_text(get_msg("not_debuted"))
            return

        stats = session.query(UserStats).filter(UserStats.user_id == user.id).first()
        effective_cooldown = get_cooldown(session, "claim", CLAIM_COOLDOWN)
        ready, remaining = check_cooldown(stats, "last_claim", effective_cooldown)
        if not ready:
            from services.message_service import get_msg
            await update.message.reply_text(
                get_msg("claim_cooldown", remaining=format_remaining(remaining)),
                parse_mode="HTML")
            return

        player = get_random_player_by_rarity(session)
        if not player:
            from services.message_service import get_msg
            await update.message.reply_text(get_msg("claim_no_players"))
            return

        # Save to plain data WHILE session is open.  SQLAlchemy expires ORM
        # objects on commit; sending from a snapshot avoids lazy reloads during
        # image generation and keeps /claim responsive.
        p = _player_to_dict(player)
        player_snapshot = SimpleNamespace(**p)
        buy_val, sell_val = get_player_values(p["rating"])
        roster_count = user.roster_count
        user_id = user.id

        # DB updates — use admin-configurable claim coin amount
        coin_reward = get_coin_amount(session, "claim", CLAIM_COINS)
        user.total_coins += coin_reward
        stats.last_claim = datetime.utcnow()
        log_activity(session, user_id, "claim", f"Claimed {p['name']} ({p['rating']})",
                     coins_change=coin_reward, player_name=p["name"], player_rating=p["rating"])
        session.commit()

        # Build text from dict (safe after commit)
        mention = f"@{tg_user.username}" if tg_user.username else (tg_user.first_name or "You")
        text = _build_card_text(p, coin_reward=coin_reward, mention=mention)

        # Build buttons
        buttons = [
            [InlineKeyboardButton("🟢 Retain", callback_data=f"retain_{p['id']}_{user_id}_{sell_val}")],
            [InlineKeyboardButton("🔴 Release", callback_data=f"release_{p['id']}_{user_id}_{sell_val}")],
        ]
        if roster_count >= MAX_ROSTER:
            buttons.append([
                InlineKeyboardButton("⚪ Replace", callback_data=f"replace_{p['id']}_{user_id}_{sell_val}")
            ])
        keyboard = InlineKeyboardMarkup(buttons)

        # Send card image + text with buttons via card_sender
        from services.card_sender import send_player_card
        msg = await send_player_card(
            bot=context.bot,
            chat_id=update.effective_chat.id,
            player=player_snapshot,
            caption=text,
            reply_markup=keyboard,
            reply_to_message_id=update.message.message_id,
            session=session,
        )

        # Schedule auto-release timer (optional)
        if msg:
            try:
                _cancel_timer(context, user_id)
                if context.job_queue:
                    context.job_queue.run_once(
                        _auto_decide, AUTO_TIMEOUT, name=f"claim_{user_id}",
                        data={"player_id": p["id"], "user_id": user_id, "sell_val": sell_val,
                              "chat_id": chat_id, "message_id": msg.message_id})
            except Exception:
                logger.warning("Job queue unavailable — no auto-release timer")

    except Exception:
        session.rollback()
        logger.exception("Claim error")
        try:
            await update.message.reply_text("⚠️ Error. Try again.")
        except Exception:
            pass
    finally:
        session.close()


# ── 🟢 Retain ────────────────────────────────────────────────────────

async def retain_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tg_user = query.from_user
    chat_id = query.message.chat_id

    parts = query.data.split("_")
    player_id, user_id = int(parts[1]), int(parts[2])
    sell_val = int(parts[3]) if len(parts) > 3 else 0

    key = f"claim_{user_id}_{player_id}"
    if _claim_done(key):
        await query.answer("Already processed!")
        return
    await query.answer()

    session = get_session()
    try:
        user = session.query(User).get(user_id)
        if not user or user.telegram_id != tg_user.id:
            # Not the owner — don't mutate the card UI or cancel their timer.
            release(key)
            return

        player = session.query(Player).get(player_id)
        if not player:
            release(key)
            return

        # Owner confirmed — now it's safe to consume the card UI + timer.
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        _cancel_timer(context, user_id)

        if user.roster_count >= MAX_ROSTER:
            release(key)
            await context.bot.send_message(chat_id=chat_id,
                text="❌ Your squad is full (25/25).\nUse ⚪ Replace or /releasepl <name>",
                parse_mode="HTML")
            return

        # Add to roster
        entry = UserRoster(user_id=user.id, player_id=player_id,
                           order_position=user.roster_count + 1,
                           acquired_date=datetime.utcnow())
        session.add(entry)
        user.roster_count += 1
        new_count = user.roster_count

        # Track quest progress
        try:
            from services.quest_service import safe_track
            safe_track(session, user.id, "claim", 1)
        except Exception:
            pass

        # Read player data while session is open
        name = player.name
        category = player.category
        rating = player.rating
        bat_rating = player.bat_rating
        bowl_rating = player.bowl_rating
        bat_hand = player.bat_hand
        bowl_style = player.bowl_style

        log_activity(session, user.id, "retain", f"Retained {name}", player_name=name)
        session.commit()

        mention = f"@{tg_user.username}" if tg_user.username else (tg_user.first_name or "")
        await context.bot.send_message(chat_id=chat_id,
            text=(f"🏏 {name} has been added to your squad. {mention}\n"
                  f"━━━━━━━━━━━━━━\n"
                  f"<blockquote expandable>"
                  f"👤 Category: {category}\n"
                  f"⭐ Rating: {rating}\n"
                  f"📊 Bat Rating: {bat_rating}\n"
                  f"📊 Bowl Rating: {bowl_rating}\n"
                  f"🏏 Bat: {bat_hand}\n"
                  f"🎯 Bowl: {bowl_style}"
                  f"</blockquote>\n"
                  f"━━━━━━━━━━━━━━\n"
                  f"✅ Your Squad Size: {new_count}/{MAX_ROSTER}"),
            parse_mode="HTML")

        # Achievement check (post-commit so it sees fresh roster_count)
        try:
            from services.achievement_service import check_and_notify
            await check_and_notify(context, chat_id, session, user.id)
        except Exception:
            pass

    except Exception:
        session.rollback()
        release(key)
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
    if _claim_done(key):
        await query.answer("Already processed!")
        return
    await query.answer()

    session = get_session()
    try:
        user = session.query(User).get(user_id)
        if not user or user.telegram_id != tg_user.id:
            # Not the owner — don't mutate the card UI or cancel their timer.
            release(key)
            return

        # Owner confirmed — now consume the card UI + timer.
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        _cancel_timer(context, user_id)

        player = session.query(Player).get(player_id)
        name = player.name if player else "Unknown"
        rating = player.rating if player else 0
        username = tg_user.username or tg_user.first_name

        user.total_coins += sell_val
        log_activity(session, user.id, "release", f"Released {name} for {sell_val:,}",
                     coins_change=sell_val, player_name=name)
        session.commit()

        await context.bot.send_message(chat_id=chat_id,
            text=(f"🏏 {name} has been released by @{username}\n\n"
                  f"{name}, {rating} OVR"),
            parse_mode="HTML")

    except Exception:
        session.rollback()
        release(key)
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
    if _claim_done(key):
        await query.answer("Already processed!")
        return
    await query.answer()

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    # Mark the originating claim consumed so its auto-decide timer won't also act.
    _claim_done(f"claim_{user_id}_{new_player_id}")
    _cancel_timer(context, user_id)

    session = get_session()
    try:
        user = session.query(User).get(user_id)
        if not user or user.telegram_id != tg_user.id:
            return

        old_entry = session.query(UserRoster).filter(
            UserRoster.id == old_roster_id, UserRoster.user_id == user.id).first()
        if not old_entry:
            await context.bot.send_message(chat_id=chat_id, text="❌ Player no longer in roster")
            return

        old_player = session.query(Player).get(old_entry.player_id)
        new_player = session.query(Player).get(new_player_id)
        old_name = old_player.name if old_player else "Unknown"
        new_name = new_player.name if new_player else "Unknown"
        count = user.roster_count

        old_entry.player_id = new_player_id
        old_entry.acquired_date = datetime.utcnow()

        log_activity(session, user.id, "replace", f"Replaced {old_name} with {new_name}",
                     player_name=new_name, player_rating=new_player.rating if new_player else 0)
        session.commit()

        await context.bot.send_message(chat_id=chat_id,
            text=(f"🔁 <b>Player SUCCESSFULLY REPLACED!</b>\n\n"
                  f"⬅ Removed: {old_name}\n"
                  f"➡ Added: {new_name}\n\n"
                  f"✅ Squad Updated: {count}/25"),
            parse_mode="HTML")

    except Exception:
        session.rollback()
        logger.exception("Replace confirm error")
    finally:
        session.close()

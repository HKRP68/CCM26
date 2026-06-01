"""Handler for /gspin — button-first, then result.

Rewards are read from the GSpinReward table (admin-configurable via the
website at /gspin-rewards). Falls back to the legacy GSPIN_OUTCOMES config
constants if the table is empty (first deploy).
"""

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
from services.gspin_reward_service import pick_reward
from config import (GSPIN_COOLDOWN, GSPIN_OUTCOMES, GSPIN_EMOJIS, GSPIN_NAMES,
                    MAX_ROSTER, get_sell_value)

logger = logging.getLogger(__name__)


def _format_segments_text(session):
    """Build the user-facing wheel description from DB rewards (or fallback)."""
    from models import GSpinReward
    rows = (session.query(GSpinReward)
                   .filter(GSpinReward.enabled == True)
                   .order_by(GSpinReward.sort_order, GSpinReward.id)
                   .all())
    if not rows:
        return (
            "🟥 <b>Red:</b> 5,000 to 10,000 coins\n"
            "🟨 <b>Yellow:</b> Random 65-78 OVR card\n"
            "🟦 <b>Blue:</b> 10-500 Gems\n"
            "🟩 <b>Green:</b> Random 79-84 OVR card\n"
            "⭐ <b>Purple:</b> Random 85-90 OVR card"
        )
    lines = []
    for r in rows:
        emoji = r.emoji or "•"
        if r.reward_type == "coins":
            desc = f"{r.amount_min:,}-{r.amount_max:,} coins"
        elif r.reward_type == "gems":
            desc = f"{r.amount_min}-{r.amount_max} gems"
        elif r.reward_type == "quest_points":
            desc = f"{r.amount_min}-{r.amount_max} QP"
        elif r.reward_type == "player":
            desc = f"Random {r.player_rating_min}-{r.player_rating_max} OVR card"
        elif r.reward_type == "pack":
            desc = "Mystery pack"
        else:
            desc = "Reward"
        lines.append(f"{emoji} <b>{r.label}:</b> {desc}")
    return "\n".join(lines)


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
            from services.message_service import get_msg
            await update.message.reply_text(
                get_msg("gspin_cooldown", remaining=format_remaining(remaining)),
                parse_mode="HTML")
            return

        # Redirect to Mini App when configured.
        # WebApp buttons only work in PRIVATE chats — Telegram rejects them in
        # groups. In groups, use a `url=` button with a t.me deep link that
        # launches the Mini App DIRECTLY (?startapp=spin) — never `?start=`,
        # which would bounce the user into a DM chat instead.
        import os as _os
        webapp_url = _os.getenv("WEBAPP_URL", "").strip()
        chat_type = (update.effective_chat.type
                     if update.effective_chat else "private")
        is_private = (chat_type == "private")

        if webapp_url and webapp_url.startswith("https://"):
            from telegram import WebAppInfo
            text = (
                "🎡 <b>GSPIN Wheel</b>\n\n"
                "📺 <b>Watch a quick ad to spin the wheel</b> and win coins, "
                "gems, players, or packs!\n\n"
                "<i>Tap below to open the Mini App and claim your spin.</i>"
            )
            if is_private:
                # WebApp button — full Mini App experience inline
                btn = InlineKeyboardButton(
                    "🎡 Open Mini App to Spin",
                    web_app=WebAppInfo(url=webapp_url + "#spin"),
                )
            else:
                # Group chat — must use url= button (WebApp not allowed here).
                # Both forms below open the Mini App directly (no DM bounce).
                bot_username = _os.getenv("BOT_USERNAME", "").strip().lstrip("@")
                miniapp_name = _os.getenv("MINIAPP_NAME", "").strip()
                if bot_username and miniapp_name:
                    # Named Mini App — opens straight into the spin tab
                    deep_link = f"https://t.me/{bot_username}/{miniapp_name}?startapp=spin"
                elif bot_username:
                    # Bot's main Mini App (BotFather) — `startapp` (not `start`)
                    # launches the app directly instead of opening a DM chat
                    deep_link = f"https://t.me/{bot_username}?startapp=spin"
                else:
                    # No bot username configured — fall through to legacy callback
                    deep_link = None

                if deep_link:
                    btn = InlineKeyboardButton("🎡 Open Mini App to Spin", url=deep_link)
                else:
                    btn = None  # falls through to legacy below

            if btn is not None:
                keyboard = InlineKeyboardMarkup([[btn]])
                await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
                return

        # Legacy fallback (no Mini App URL OR group chat with no bot username)
        text = (
            "🎡 <b>GSPIN Wheel</b>\n\n"
            + _format_segments_text(session)
            + "\n\nTap to spin!"
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

        reward_row = pick_reward(session)
        reward_lines = ""
        colour_label = ""
        emoji = ""

        if reward_row is not None:
            colour_label = reward_row.label
            emoji = reward_row.emoji or "🎁"
            rt = reward_row.reward_type

            if rt == "coins":
                lo = reward_row.amount_min or 0
                hi = max(reward_row.amount_max or 0, lo)
                amount = random.randint(lo, hi) if hi > 0 else 0
                user.total_coins += amount
                reward_lines = f"YOU GOT COINS!\n💰 +{amount:,} coins"

            elif rt == "gems":
                lo = reward_row.amount_min or 0
                hi = max(reward_row.amount_max or 0, lo)
                amount = random.randint(lo, hi) if hi > 0 else 0
                user.total_gems += amount
                reward_lines = f"YOU GOT GEMS!\n💎 +{amount} gems"

            elif rt == "quest_points":
                lo = reward_row.amount_min or 0
                hi = max(reward_row.amount_max or 0, lo)
                amount = random.randint(lo, hi) if hi > 0 else 0
                user.quest_points = (user.quest_points or 0) + amount
                reward_lines = f"YOU GOT QUEST POINTS!\n🎯 +{amount} QP"

            elif rt == "player":
                low = reward_row.player_rating_min or 50
                high = max(reward_row.player_rating_max or low, low)
                player = get_random_player_by_rating_range(session, low, high)
                if player:
                    if user.roster_count < MAX_ROSTER:
                        entry = UserRoster(user_id=user.id, player_id=player.id,
                                            order_position=user.roster_count + 1,
                                            acquired_date=datetime.utcnow())
                        session.add(entry)
                        user.roster_count += 1
                        reward_lines = (
                            f"YOU GOT A NEW PLAYER!\n"
                            f"🎉 {player.name} - {player.rating} OVR\n"
                            f"🎯 {player.category} | {player.country}\n"
                            f"✅ Added to squad ({user.roster_count}/25)"
                        )
                    else:
                        sell_val = get_sell_value(player.rating)
                        reward_lines = (
                            f"YOU GOT A NEW PLAYER!\n"
                            f"🎉 {player.name} - {player.rating} OVR\n"
                            f"⚠️ Squad full — choose below"
                        )
                        stats.last_gspin = datetime.utcnow()
                        log_activity(session, user.id, "gspin",
                                     f"GSpin: {colour_label} → {player.name}")
                        session.commit()

                        text = (
                            f"🎡 <b>GSPIN Wheel Result!</b>\n\n"
                            f"{emoji} <b>{colour_label}</b>\n\n"
                            f"{reward_lines}\n\n"
                            "✅ Decide what to do with this player below."
                        )
                        await query.edit_message_text(text, parse_mode="HTML")

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
                    reward_lines = f"No players in {low}-{high} range — {amount:,} coins instead!\n💰 +{amount:,}"

            elif rt == "pack":
                from models import Pack, UserPack
                pid = reward_row.pack_id
                pack = session.query(Pack).get(pid) if pid else None
                if pack:
                    up = UserPack(user_id=user.id, pack_id=pack.id,
                                   acquired_at=datetime.utcnow())
                    session.add(up)
                    reward_lines = (f"YOU GOT A PACK!\n"
                                    f"📦 {pack.name} — added to your inventory.\n"
                                    f"Open with /openpack")
                else:
                    amount = random.randint(5000, 10000)
                    user.total_coins += amount
                    reward_lines = f"Pack reward misconfigured — {amount:,} coins instead!\n💰 +{amount:,}"

            else:
                amount = random.randint(1000, 5000)
                user.total_coins += amount
                reward_lines = f"Bonus coins!\n💰 +{amount:,}"

        else:
            # ── Legacy fallback (DB empty) ─────────────────────────────
            roll = random.random()
            colour = outcome_type = None
            outcome_range = (0, 0)
            for threshold, col, otype, orange in GSPIN_OUTCOMES:
                if roll <= threshold:
                    colour, outcome_type, outcome_range = col, otype, orange
                    break
            emoji = GSPIN_EMOJIS[colour]
            colour_label = GSPIN_NAMES[colour]

            if outcome_type == "coins":
                amount = random.randint(outcome_range[0], outcome_range[1])
                user.total_coins += amount
                reward_lines = f"YOU GOT COINS!\n💰 +{amount:,} coins"
            elif outcome_type == "gems":
                from services.config_service import get_config
                cfg = get_config(session)
                lo = cfg["gspin_gem_min"] if cfg["gspin_gem_min"] > 0 else outcome_range[0]
                hi = cfg["gspin_gem_max"] if cfg["gspin_gem_max"] > 0 else outcome_range[1]
                if hi < lo: hi = lo
                amount = random.randint(lo, hi)
                user.total_gems += amount
                reward_lines = f"YOU GOT GEMS!\n💎 +{amount} gems"
            elif outcome_type == "player":
                low, high = outcome_range
                player = get_random_player_by_rating_range(session, low, high)
                if player and user.roster_count < MAX_ROSTER:
                    entry = UserRoster(user_id=user.id, player_id=player.id,
                                        order_position=user.roster_count + 1,
                                        acquired_date=datetime.utcnow())
                    session.add(entry)
                    user.roster_count += 1
                    reward_lines = (f"YOU GOT A NEW PLAYER!\n"
                                    f"🎉 {player.name} - {player.rating} OVR\n"
                                    f"✅ Added to squad ({user.roster_count}/25)")
                elif player:
                    sell_val = get_sell_value(player.rating)
                    stats.last_gspin = datetime.utcnow()
                    session.commit()
                    text = (f"🎡 <b>GSPIN!</b>\n\n{emoji} {colour_label}\n\n"
                            f"🎉 {player.name} ({player.rating})\n⚠️ Squad full")
                    await query.edit_message_text(text, parse_mode="HTML")
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
        log_activity(session, user.id, "gspin", f"GSpin: {colour_label}")
        try:
            from services.quest_service import safe_track
            safe_track(session, user.id, "gspin", 1)
        except Exception:
            pass
        session.commit()

        text = (
            f"🎡 <b>GSPIN Wheel Result!</b>\n\n"
            f"{emoji} <b>{colour_label}</b>\n\n"
            f"{reward_lines}\n\n"
            "✅ Reward added to your account!"
        )
        await query.edit_message_text(text, parse_mode="HTML")

    except Exception:
        session.rollback()
        logger.exception("GSpin spin error")
    finally:
        session.close()

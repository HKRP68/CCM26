"""/myquest — view and claim quest progress."""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User
from services.quest_service import (
    get_user_quests, claim_quest_reward, claim_all_completed,
)
from services.button_timeout import schedule_button_timeout

logger = logging.getLogger(__name__)


def _progress_bar(percent):
    """Return a Unicode progress bar for the given percent (0-100)."""
    filled = int(percent / 10)
    return "█" * filled + "░" * (10 - filled)


def _render_quest_list(quests_data, quest_type, user, owner_tg):
    """Render the message text + keyboard for a quest tab."""
    type_label = "DAILY" if quest_type == "daily" else "MONTHLY"
    type_icon = "📅" if quest_type == "daily" else "🗓️"
    reset_label = "every 24 hours" if quest_type == "daily" else "every 30 days"

    lines = [
        f"{type_icon} <b>{type_label} QUESTS</b>",
        f"💎 Quest Points: <b>{user.quest_points or 0}</b>  |  Resets {reset_label}",
        "━━━━━━━━━━━━━━━━━━━",
    ]

    if not quests_data:
        lines.append(f"\n<i>No active {quest_type} quests right now.</i>")
        lines.append(f"<i>Check back later or ask admin to add some.</i>")

    completed_unclaimed = 0
    for it in quests_data:
        q = it["quest"]
        bar = _progress_bar(it["percent"])
        status = ""
        if it["claimed"]:
            status = " ✅ <i>Claimed</i>"
        elif it["completed"]:
            status = " 🎁 <b>READY TO CLAIM</b>"
            completed_unclaimed += 1
        rewards = []
        if q.reward_points:
            rewards.append(f"+{q.reward_points} pts")
        if q.reward_coins:
            rewards.append(f"+{q.reward_coins:,} 🪙")
        if q.reward_gems:
            rewards.append(f"+{q.reward_gems} 💎")
        reward_str = " · ".join(rewards) if rewards else ""

        lines.append(
            f"\n{q.emoji} <b>{q.name}</b>{status}\n"
            f"   <i>{q.description}</i>\n"
            f"   {bar} <code>{it['progress']}/{it['target']}</code>"
            f"{'  · ' + reward_str if reward_str else ''}"
        )

    # Build keyboard — tab switcher + claim buttons + claim-all
    btns = []
    # Tab switcher row
    daily_label = "📅 Daily" + ("  •" if quest_type == "daily" else "")
    monthly_label = "🗓️ Monthly" + ("  •" if quest_type == "monthly" else "")
    btns.append([
        InlineKeyboardButton(daily_label, callback_data=f"qst_tab_{owner_tg}_daily"),
        InlineKeyboardButton(monthly_label, callback_data=f"qst_tab_{owner_tg}_monthly"),
    ])

    # Per-quest claim buttons (only for completed-unclaimed)
    for it in quests_data:
        if it["completed"] and not it["claimed"]:
            q = it["quest"]
            btns.append([InlineKeyboardButton(
                f"🎁 Claim: {q.emoji} {q.name}",
                callback_data=f"qst_claim_{owner_tg}_{q.id}",
            )])

    # Claim All button (if multiple ready)
    if completed_unclaimed >= 2:
        btns.append([InlineKeyboardButton(
            f"🎁 CLAIM ALL ({completed_unclaimed})",
            callback_data=f"qst_claimall_{owner_tg}_{quest_type}",
        )])

    # Close
    btns.append([InlineKeyboardButton("❌ Close", callback_data=f"qst_close_{owner_tg}")])

    return "\n".join(lines), InlineKeyboardMarkup(btns)


# ════════════════════════════════════════════════════════════════════
# /myquest command
# ════════════════════════════════════════════════════════════════════

async def myquest_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        quests_data = get_user_quests(session, user.id, "daily")
        text, kb = _render_quest_list(quests_data, "daily", user, tg.id)

        sent = await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
        try:
            schedule_button_timeout(context, sent.chat_id, sent.message_id, delay_seconds=120)
        except Exception:
            pass

    except Exception:
        logger.exception("myquest_handler error")
        await update.message.reply_text("⚠️ Error loading quests.")
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# Tab switcher callback — qst_tab_<owner_tg>_<type>
# ════════════════════════════════════════════════════════════════════

async def quest_tab_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        owner_tg = int(parts[2])
        quest_type = parts[3]
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return

    if tg.id != owner_tg:
        await q.answer("Not yours!", show_alert=True)
        return

    if quest_type not in ("daily", "monthly"):
        await q.answer("Unknown tab")
        return

    await q.answer()
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            return
        quests_data = get_user_quests(session, user.id, quest_type)
        text, kb = _render_quest_list(quests_data, quest_type, user, tg.id)
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
    except Exception:
        logger.exception("quest_tab_callback error")
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# Claim single quest — qst_claim_<owner_tg>_<quest_id>
# ════════════════════════════════════════════════════════════════════

async def quest_claim_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        owner_tg = int(parts[2])
        quest_id = int(parts[3])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return

    if tg.id != owner_tg:
        await q.answer("Not yours!", show_alert=True)
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await q.answer("Not found")
            return

        ok, msg, reward = claim_quest_reward(session, user.id, quest_id)
        if not ok:
            session.rollback()
            await q.answer(msg, show_alert=True)
            return

        session.commit()

        bits = []
        if reward.get("points"): bits.append(f"+{reward['points']} pts")
        if reward.get("coins"):  bits.append(f"+{reward['coins']:,} 🪙")
        if reward.get("gems"):   bits.append(f"+{reward['gems']} 💎")
        await q.answer(f"🎁 {' · '.join(bits)}")

        # Re-render the current tab — figure out which one we're on
        # by checking which quest type was just claimed
        from models import Quest as Q
        cq = session.query(Q).get(quest_id)
        quest_type = cq.quest_type if cq else "daily"
        quests_data = get_user_quests(session, user.id, quest_type)
        text, kb = _render_quest_list(quests_data, quest_type, user, tg.id)
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
    except Exception:
        session.rollback()
        logger.exception("quest_claim_callback error")
        await q.answer("⚠️ Error", show_alert=True)
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# Claim all — qst_claimall_<owner_tg>_<type>
# ════════════════════════════════════════════════════════════════════

async def quest_claimall_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        owner_tg = int(parts[2])
        quest_type = parts[3]
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return

    if tg.id != owner_tg:
        await q.answer("Not yours!", show_alert=True)
        return

    if quest_type not in ("daily", "monthly"):
        await q.answer("Unknown")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            return

        count, total = claim_all_completed(session, user.id, quest_type)
        if count == 0:
            await q.answer("Nothing to claim.", show_alert=True)
            return
        session.commit()

        bits = []
        if total["points"]: bits.append(f"+{total['points']} pts")
        if total["coins"]:  bits.append(f"+{total['coins']:,} 🪙")
        if total["gems"]:   bits.append(f"+{total['gems']} 💎")
        await q.answer(f"🎁 Claimed {count}: {' · '.join(bits)}")

        quests_data = get_user_quests(session, user.id, quest_type)
        text, kb = _render_quest_list(quests_data, quest_type, user, tg.id)
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
    except Exception:
        session.rollback()
        logger.exception("quest_claimall_callback error")
        await q.answer("⚠️ Error", show_alert=True)
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# Close — qst_close_<owner_tg>
# ════════════════════════════════════════════════════════════════════

async def quest_close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tg = q.from_user
    try:
        owner_tg = int(q.data.split("_")[2])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return

    if tg.id != owner_tg:
        await q.answer("Not yours!", show_alert=True)
        return

    await q.answer("Closed")
    try:
        await q.edit_message_text("✨ <i>Quest panel closed.</i>", parse_mode="HTML")
    except Exception:
        pass

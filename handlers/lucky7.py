"""/lucky7 <bet> — Lucky 7 dice game. Bet on dice sum: below/above/exactly 7."""
import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from models import User
from services.activity_service import log_activity

logger = logging.getLogger(__name__)
MIN_BET, MAX_BET = 10, 5000
# user_id -> {bet, chat_id}
PENDING: dict = {}


def _usage():
    return (
        "🎲 <b>Lucky 7</b> — Dice Betting\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "Usage: <code>/lucky7 &lt;bet&gt;</code>\n"
        f"Bet: {MIN_BET}–{MAX_BET} coins\n\n"
        "• <b>Below 7</b> → 2× payout\n"
        "• <b>Above 7</b> → 2× payout\n"
        "• <b>Exactly 7</b> → 5× payout"
    )


async def lucky7_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    args = context.args or []
    if not args:
        await update.message.reply_text(_usage(), parse_mode="HTML")
        return
    try:
        bet = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Enter a whole-number bet.\n\n" + _usage(), parse_mode="HTML")
        return
    if not MIN_BET <= bet <= MAX_BET:
        await update.message.reply_text(f"❌ Bet must be {MIN_BET}–{MAX_BET} coins.")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == uid).first()
        if not user:
            await update.message.reply_text("❌ Use /debut first.")
            return
        if user.total_coins < bet:
            await update.message.reply_text(
                f"❌ Not enough coins. Balance: <b>{user.total_coins:,}</b>.", parse_mode="HTML")
            return
        user.total_coins -= bet
        session.commit()
    finally:
        session.close()

    PENDING[uid] = {"bet": bet, "chat_id": update.effective_chat.id}
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⬇️ Below 7  (2×)", callback_data=f"l7_below_{uid}_{bet}"),
        InlineKeyboardButton("⬆️ Above 7  (2×)", callback_data=f"l7_above_{uid}_{bet}"),
    ], [
        InlineKeyboardButton("🎯 Exactly 7  (5×)", callback_data=f"l7_seven_{uid}_{bet}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"l7_cancel_{uid}_{bet}"),
    ]])
    await update.message.reply_text(
        f"🎲 <b>Lucky 7</b>\n\nBet: <b>{bet:,}</b> coins\nPick your outcome:",
        parse_mode="HTML", reply_markup=kb,
    )


async def lucky7_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("_")
    outcome, owner_id, bet = parts[1], int(parts[2]), int(parts[3])

    if query.from_user.id != owner_id:
        await query.answer("Not your game.", show_alert=True)
        return
    await query.answer()

    if outcome == "cancel":
        PENDING.pop(owner_id, None)
        session = get_session()
        try:
            user = session.query(User).filter(User.telegram_id == owner_id).first()
            if user:
                user.total_coins += bet
                session.commit()
        finally:
            session.close()
        await query.edit_message_text("❌ Lucky 7 cancelled. Bet refunded.")
        return

    PENDING.pop(owner_id, None)
    await query.edit_message_text("🎲 Rolling the dice…")

    d1 = await context.bot.send_dice(query.message.chat_id, emoji="🎲")
    d2 = await context.bot.send_dice(query.message.chat_id, emoji="🎲")
    await asyncio.sleep(3)

    total = d1.dice.value + d2.dice.value
    won = (
        (outcome == "below" and total < 7) or
        (outcome == "above" and total > 7) or
        (outcome == "seven" and total == 7)
    )
    multiplier = 5 if outcome == "seven" else 2
    label = {"below": "Below 7", "above": "Above 7", "seven": "Exactly 7"}[outcome]

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == owner_id).first()
        if not user:
            return
        if won:
            payout = bet * multiplier
            user.total_coins += payout
            net = payout - bet
            body = f"🎉 <b>WIN!</b>  Total: <b>{total}</b>  ({label})\n💰 +{net:,} coins"
        else:
            net = -bet
            body = f"💔 <b>LOSS!</b>  Total: <b>{total}</b>  (you picked {label})\n💸 −{bet:,} coins"
        log_activity(session, user.id, "lucky7", f"{label} bet:{bet} total:{total} net:{net:+d}")
        session.commit()
        await query.edit_message_text(
            f"🎲 <b>Lucky 7 Result</b>\n\n{body}\n💵 Balance: <b>{user.total_coins:,}</b>",
            parse_mode="HTML",
        )
    finally:
        session.close()

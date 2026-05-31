"""/powerplay <bet> <target> — Power Play crash game.
The multiplier crashes at a random point; if your target <= crash point you win.
"""
import logging
import random

from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import User
from services.activity_service import log_activity

logger = logging.getLogger(__name__)
MIN_BET, MAX_BET = 10, 10000
MIN_TARGET, MAX_TARGET = 1.1, 100.0


def _crash_point() -> float:
    """10 % instant crash, otherwise 99/(100-P)."""
    if random.random() < 0.10:
        return 1.0
    P = random.uniform(0, 99.99)
    crash = 99.0 / (100.0 - P)
    return max(1.0, round(crash, 2))


def _usage():
    return (
        "✈️ <b>Power Play</b> — Crash Betting\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "Usage: <code>/powerplay &lt;bet&gt; &lt;target&gt;</code>\n"
        f"  Bet: {MIN_BET}–{MAX_BET} coins\n"
        f"  Target: {MIN_TARGET}×–{MAX_TARGET}× (e.g. 2.5)\n\n"
        "If the plane stays airborne past your target multiplier you win.\n"
        "Win payout = bet × target\n\n"
        "<b>Rough odds:</b>\n"
        "• 1.5× → ~60 % win rate\n"
        "• 2×   → ~45 % win rate\n"
        "• 10×  → ~9 % win rate"
    )


async def powerplay_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(_usage(), parse_mode="HTML")
        return

    try:
        bet = int(args[0])
        target = round(float(args[1]), 2)
    except ValueError:
        await update.message.reply_text("❌ Invalid numbers.\n\n" + _usage(), parse_mode="HTML")
        return

    if not MIN_BET <= bet <= MAX_BET:
        await update.message.reply_text(f"❌ Bet must be {MIN_BET}–{MAX_BET} coins.")
        return
    if not MIN_TARGET <= target <= MAX_TARGET:
        await update.message.reply_text(f"❌ Target must be {MIN_TARGET}×–{MAX_TARGET}×.")
        return

    crash = _crash_point()
    won = crash >= target

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == update.effective_user.id).first()
        if not user:
            await update.message.reply_text("❌ Use /debut first.")
            return
        if user.total_coins < bet:
            await update.message.reply_text(
                f"❌ Not enough coins. Balance: <b>{user.total_coins:,}</b>.", parse_mode="HTML")
            return

        if won:
            payout = int(bet * target)
            net = payout - bet
            user.total_coins += net
            body = (
                f"✅ <b>SURVIVED!</b>\n\n"
                f"✈️ Plane crashed at <b>{crash}×</b> — past your target <b>{target}×</b>\n"
                f"💰 Payout: <b>{payout:,}</b> coins  (net +{net:,})"
            )
        else:
            user.total_coins -= bet
            body = (
                f"💥 <b>CRASHED!</b>\n\n"
                f"✈️ Crashed at <b>{crash}×</b> — before your target <b>{target}×</b>\n"
                f"💸 Lost: <b>{bet:,}</b> coins"
            )

        log_activity(session, user.id, "powerplay",
                     f"bet:{bet} target:{target} crash:{crash} {'win' if won else 'loss'}")
        session.commit()
        await update.message.reply_text(
            f"✈️ <b>Power Play</b>\n\n{body}\n\n💵 Balance: <b>{user.total_coins:,}</b>",
            parse_mode="HTML",
        )
    finally:
        session.close()

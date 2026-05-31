"""/catch <bet> <height> — catching game using the user's purse coins."""

import logging
import random

from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import User
from services.activity_service import log_activity

logger = logging.getLogger(__name__)
MIN_BET, MAX_BET, MIN_HEIGHT, MAX_HEIGHT, MAX_PAYOUT, BASE_WIN_CONST = 1, 500, 1.2, 20.0, 5000, 95.0

def _dynamic_max_bet(height): return int(min(MAX_BET, MAX_PAYOUT / height))
def _usage_text():
    return ("🏏 <b>CATCH — the catching game</b>\n━━━━━━━━━━━━━━━━━━━\nUsage: <code>/catch &lt;bet&gt; &lt;height&gt;</code>\n"
            "Example: <code>/catch 50 1.5</code>\n\n• Uses the same purse coins as /purse and player purchases\n"
            f"• Bet: {MIN_BET}–{MAX_BET} coins\n• Height: {MIN_HEIGHT}m–{MAX_HEIGHT:.0f}m (higher = harder)\n"
            "• Win chance = 95 ÷ height (%)\n• <b>CAUGHT</b> → payout bet × height\n• <b>DROPPED</b> → lose your bet\n"
            f"• Max payout: {MAX_PAYOUT} coins")

async def bal_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("ℹ️ /catch now uses your main purse coins. Check your balance with /purse.")

async def catch_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, args = update.effective_message, context.args or []
    if len(args) != 2: await msg.reply_text(_usage_text(), parse_mode="HTML"); return
    try: bet, height = int(args[0]), round(float(args[1]), 2)
    except ValueError: await msg.reply_text("❌ Use a whole-number bet and numeric height.\n\n" + _usage_text(), parse_mode="HTML"); return
    if not MIN_HEIGHT <= height <= MAX_HEIGHT: await msg.reply_text(f"❌ Height must be between {MIN_HEIGHT}m and {MAX_HEIGHT:.0f}m."); return
    if bet < MIN_BET or bet > _dynamic_max_bet(height): await msg.reply_text(f"❌ At {height}m, bet between {MIN_BET} and {_dynamic_max_bet(height)} coins."); return
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == update.effective_user.id).first()
        if not user: await msg.reply_text("❌ Use /debut first."); return
        if user.total_coins < bet: await msg.reply_text(f"❌ Not enough purse coins. You have <b>{user.total_coins:,}</b>.", parse_mode="HTML"); return
        chance = BASE_WIN_CONST / height; caught = random.uniform(0, 100) < chance; payout = min(MAX_PAYOUT, round(bet * height)); delta = payout - bet if caught else -bet
        user.total_coins += delta
        log_activity(session, user.id, "catch", f"{'CAUGHT' if caught else 'DROPPED'} @ {height}m · bet {bet} · net {delta:+d} coins")
        session.commit()
        if caught: body = f"🧤 <b>CAUGHT!</b> 🎉\n🏏 Height: <b>{height}m</b> ({chance:.1f}%)\n💰 Bet: <b>{bet:,}</b> coins\n🏆 Payout: <b>{payout:,}</b> coins (net {delta:+,})"
        else: body = f"🤲 <b>DROPPED!</b> 💔\n🏏 Height: <b>{height}m</b> ({chance:.1f}%)\n💰 Lost: <b>{bet:,}</b> coins"
        await msg.reply_text(body + f"\n💵 Purse: <b>{user.total_coins:,}</b> coins", parse_mode="HTML")
    except Exception:
        session.rollback(); logger.exception("catch failed"); await msg.reply_text("⚠️ Error. Try again.")
    finally: session.close()

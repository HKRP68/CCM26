"""/catch <bet> <height> — Telegram-storage-backed CRIC catching game."""

import html
import logging
import random

from telegram import Update
from telegram.ext import ContextTypes

from services import catch_wallet_service

logger = logging.getLogger(__name__)

MIN_BET = 1
MAX_BET = 500
MIN_HEIGHT = 1.2
MAX_HEIGHT = 20.0
MAX_PAYOUT = 5000
BASE_WIN_CONST = 95.0


def _dynamic_max_bet(height):
    """Largest bet allowed at this height so payout (bet×height) ≤ MAX_PAYOUT."""
    return int(min(MAX_BET, MAX_PAYOUT / height))


def _usage_text():
    return (
        "🏏 <b>CATCH — the catching game</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "Usage: <code>/catch &lt;bet&gt; &lt;height&gt;</code>\n"
        "Example: <code>/catch 50 1.5</code>\n\n"
        f"• Bet: {MIN_BET}–{MAX_BET} CRIC\n"
        f"• Height: {MIN_HEIGHT}m–{MAX_HEIGHT:.0f}m (higher = harder)\n"
        "• Win chance = 95 ÷ height (%)\n"
        "• <b>CAUGHT</b> → win bet × height\n"
        "• <b>DROPPED</b> → lose your bet\n"
        f"• Max payout: {MAX_PAYOUT} CRIC\n"
        "• Check this separate game wallet with <code>/bal cric</code>."
    )


def _storage_error_text():
    return (
        "⚠️ The CRIC wallet is unavailable. Configure a Telegram storage channel "
        "and give the bot permission to post and pin messages."
    )


async def bal_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the Telegram-storage-backed wallet requested by `/bal cric`."""
    args = context.args or []
    if len(args) != 1 or args[0].lower() != "cric":
        await update.effective_message.reply_text("Usage: <code>/bal cric</code>", parse_mode="HTML")
        return
    try:
        balance = await catch_wallet_service.get_balance(context.bot, update.effective_user.id)
        await update.effective_message.reply_text(
            f"🏏 <b>CRIC balance:</b> {balance:,} CRIC\n"
            "Use it in <code>/catch &lt;bet&gt; &lt;height&gt;</code>.",
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("bal cric: Telegram storage wallet failed")
        await update.effective_message.reply_text(_storage_error_text())


async def catch_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    args = context.args or []
    if len(args) != 2:
        await msg.reply_text(_usage_text(), parse_mode="HTML")
        return
    try:
        bet = int(args[0])
    except ValueError:
        await msg.reply_text("❌ Bet must be a whole number of CRIC.\n\n" + _usage_text(), parse_mode="HTML")
        return
    try:
        height = float(args[1])
    except ValueError:
        await msg.reply_text("❌ Height must be a number (metres), e.g. 1.5.\n\n" + _usage_text(), parse_mode="HTML")
        return
    if height < MIN_HEIGHT or height > MAX_HEIGHT:
        await msg.reply_text(f"❌ Height must be between {MIN_HEIGHT}m and {MAX_HEIGHT:.0f}m.")
        return
    height = round(height, 2)
    max_bet_here = _dynamic_max_bet(height)
    if bet < MIN_BET:
        await msg.reply_text(f"❌ Minimum bet is {MIN_BET} CRIC.")
        return
    if bet > max_bet_here:
        await msg.reply_text(
            f"❌ At {height}m the most you can bet is <b>{max_bet_here}</b> CRIC "
            f"(so the payout stays within the {MAX_PAYOUT}-CRIC cap).", parse_mode="HTML")
        return

    try:
        balance = await catch_wallet_service.get_balance(context.bot, update.effective_user.id)
        if balance < bet:
            await msg.reply_text(f"❌ Not enough CRIC. You have <b>{balance}</b>, bet was <b>{bet}</b>.", parse_mode="HTML")
            return
        win_chance = BASE_WIN_CONST / height
        caught = random.uniform(0, 100) < win_chance
        payout = min(MAX_PAYOUT, round(bet * height))
        delta = payout - bet if caught else -bet
        tg = update.effective_user
        display_name = html.escape(f"@{tg.username}" if tg.username else (tg.first_name or str(tg.id)))
        outcome = "CAUGHT" if caught else "DROPPED"
        audit = f"🏏 /catch — {display_name} · {outcome} @ {height}m · bet {bet} CRIC"
        new_balance = await catch_wallet_service.apply_catch_result(context.bot, tg.id, bet, delta, audit)

        chance_str = f"{win_chance:.1f}%"
        if caught:
            body = (
                "🧤 <b>CAUGHT!</b> 🎉\n━━━━━━━━━━━━━━━━━━━\n"
                f"🏏 Height: <b>{height}m</b>  (catch chance {chance_str})\n"
                f"💰 Bet: <b>{bet}</b> CRIC\n"
                f"🏆 Payout: <b>{payout}</b> CRIC  (net +{delta})\n"
                f"💵 Balance: <b>{new_balance}</b> CRIC"
            )
        else:
            body = (
                "🤲 <b>DROPPED!</b> 💔\n━━━━━━━━━━━━━━━━━━━\n"
                f"🏏 Height: <b>{height}m</b>  (catch chance {chance_str})\n"
                f"💰 Bet lost: <b>{bet}</b> CRIC\n"
                f"💵 Balance: <b>{new_balance}</b> CRIC"
            )
        await msg.reply_text(body, parse_mode="HTML")
    except ValueError:
        await msg.reply_text("❌ Not enough CRIC. Check your wallet with <code>/bal cric</code>.", parse_mode="HTML")
    except Exception:
        logger.exception("catch_handler: Telegram storage wallet failed")
        await msg.reply_text(_storage_error_text())

"""/catch <bet> <height> — a quick catching mini-game.

Bet coins to catch a cricket ball dropped from a chosen height. Higher = harder.

  win chance = 95 / height   (percent)
  CAUGHT  → credited  round(bet × height)   (capped so payout ≤ MAX_PAYOUT)
  DROPPED → lose the bet

No new database table is used (so nothing schedules Neon compute). The only DB
touch is the unavoidable coin-balance update on the user's row — coins live in
the DB, so a balance-aware bet must read/write it. Each result is also logged
to the Telegram storage channel (a text line), giving an audit trail without a
DB table.
"""

import logging
import random

from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import User
from services.activity_service import log_activity

logger = logging.getLogger(__name__)

# ── Tunable limits ───────────────────────────────────────────────────
MIN_BET = 1
MAX_BET = 500
MIN_HEIGHT = 1.2
MAX_HEIGHT = 20.0
MAX_PAYOUT = 5000
BASE_WIN_CONST = 95.0   # win% = BASE_WIN_CONST / height  → ~5% house edge


def _dynamic_max_bet(height):
    """Largest bet allowed at this height so payout (bet×height) ≤ MAX_PAYOUT."""
    return int(min(MAX_BET, MAX_PAYOUT / height))


def _usage_text():
    return (
        "🏏 <b>CATCH — the catching game</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "Usage: <code>/catch &lt;bet&gt; &lt;height&gt;</code>\n"
        "Example: <code>/catch 50 1.5</code>\n\n"
        f"• Bet: {MIN_BET}–{MAX_BET} coins\n"
        f"• Height: {MIN_HEIGHT}m–{MAX_HEIGHT:.0f}m (higher = harder)\n"
        "• Win chance = 95 ÷ height (%)\n"
        "• <b>CAUGHT</b> → win bet × height\n"
        "• <b>DROPPED</b> → lose your bet\n"
        f"• Max payout: {MAX_PAYOUT} coins\n"
        "• Max bet at a height auto-shrinks so the payout stays within the cap."
    )


def _log_to_storage_channel(context, text):
    """Best-effort: log a catch result line to the Telegram storage channel.
    No DB write; purely a message send. Silently no-ops if not configured."""
    try:
        from config import MEDIA_STORAGE_CHAT_ID
        if not MEDIA_STORAGE_CHAT_ID:
            return
        # Fire-and-forget; we don't await the result of the audit log.
        context.application.create_task(
            context.bot.send_message(
                chat_id=int(MEDIA_STORAGE_CHAT_ID),
                text=text,
                parse_mode="HTML",
                disable_notification=True,
            )
        )
    except Exception:
        logger.exception("catch: storage-channel log failed (non-fatal)")


async def catch_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    args = context.args or []

    if len(args) != 2:
        await msg.reply_text(_usage_text(), parse_mode="HTML")
        return

    # Parse bet + height
    try:
        bet = int(args[0])
    except ValueError:
        await msg.reply_text("❌ Bet must be a whole number of coins.\n\n" + _usage_text(),
                             parse_mode="HTML")
        return
    try:
        height = float(args[1])
    except ValueError:
        await msg.reply_text("❌ Height must be a number (metres), e.g. 1.5.\n\n" + _usage_text(),
                             parse_mode="HTML")
        return

    # Validate height first (it constrains the max bet)
    if height < MIN_HEIGHT or height > MAX_HEIGHT:
        await msg.reply_text(
            f"❌ Height must be between {MIN_HEIGHT}m and {MAX_HEIGHT:.0f}m.",
            parse_mode="HTML")
        return

    height = round(height, 2)
    max_bet_here = _dynamic_max_bet(height)

    if bet < MIN_BET:
        await msg.reply_text(f"❌ Minimum bet is {MIN_BET} coin.", parse_mode="HTML")
        return
    if bet > max_bet_here:
        await msg.reply_text(
            f"❌ At {height}m the most you can bet is <b>{max_bet_here}</b> coins "
            f"(so the payout stays within the {MAX_PAYOUT}-coin cap).",
            parse_mode="HTML")
        return

    tg = update.effective_user
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await msg.reply_text("❌ You need to /debut first.")
            return

        balance = user.total_coins or 0
        if balance < bet:
            await msg.reply_text(
                f"❌ Not enough coins. You have <b>{balance}</b>, "
                f"bet was <b>{bet}</b>.", parse_mode="HTML")
            return

        # ── Resolve the catch ──
        win_chance = BASE_WIN_CONST / height          # percent
        roll = random.uniform(0, 100)
        caught = roll < win_chance
        payout = min(MAX_PAYOUT, round(bet * height))  # credited if caught

        # Coin update (the only DB write — no new table)
        if caught:
            net = payout - bet
            user.total_coins = balance + net
            log_activity(session, user.id, "catch_win",
                         f"CAUGHT @ {height}m — bet {bet}, payout {payout}",
                         coins_change=net)
        else:
            user.total_coins = balance - bet
            log_activity(session, user.id, "catch_loss",
                         f"DROPPED @ {height}m — lost {bet}",
                         coins_change=-bet)
        new_balance = user.total_coins
        session.commit()

        # ── Reply ──
        chance_str = f"{win_chance:.1f}%"
        if caught:
            body = (
                "🧤 <b>CAUGHT!</b> 🎉\n"
                "━━━━━━━━━━━━━━━━━━━\n"
                f"🏏 Height: <b>{height}m</b>  (catch chance {chance_str})\n"
                f"💰 Bet: <b>{bet}</b>\n"
                f"🏆 Payout: <b>{payout}</b>  (net +{payout - bet})\n"
                f"💵 Balance: <b>{new_balance}</b>"
            )
        else:
            body = (
                "🤲 <b>DROPPED!</b> 💔\n"
                "━━━━━━━━━━━━━━━━━━━\n"
                f"🏏 Height: <b>{height}m</b>  (catch chance {chance_str})\n"
                f"💰 Bet lost: <b>{bet}</b>\n"
                f"💵 Balance: <b>{new_balance}</b>"
            )
        await msg.reply_text(body, parse_mode="HTML")

        # Audit to the storage channel (no DB)
        uname = f"@{user.username}" if user.username else (user.first_name or "Player")
        outcome = "CAUGHT" if caught else "DROPPED"
        _log_to_storage_channel(
            context,
            f"🏏 <b>/catch</b> — {uname}\n"
            f"{outcome} @ {height}m · bet {bet} · "
            f"{'payout ' + str(payout) if caught else 'lost ' + str(bet)} · "
            f"bal {new_balance}")
    except Exception:
        session.rollback()
        logger.exception("catch_handler error")
        await msg.reply_text("❌ Something went wrong. Your coins are safe.")
    finally:
        session.close()

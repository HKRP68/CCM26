"""Referral code redemption — `/redeem CODE` + free-text fallback.

The user can type their referral code:
  1. Via `/redeem ABCD23` command
  2. As a plain text message after /debut (when `awaiting_referral_code` flag set)

A "skip" inline button on the post-debut prompt clears the flag.
"""

import logging
import re
from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import User
from services.referral_service import redeem_code, find_inviter_by_code

logger = logging.getLogger(__name__)

# Code pattern: 6 chars from our alphabet (uppercase letters + digits, no 0,O,1,I)
_CODE_RE = re.compile(r"^[A-HJ-NP-Z2-9]{6}$")


def _looks_like_code(text):
    """Return uppercase code if text looks like a referral code, else None."""
    if not text:
        return None
    t = text.strip().upper()
    # Strip leading slash so "/ABCD23" still works
    t = t.lstrip("/")
    if _CODE_RE.match(t):
        return t
    return None


async def redeem_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/redeem CODE` — explicit redemption."""
    tg_user = update.effective_user
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: <code>/redeem CODE</code>\n\n"
            "Enter the 6-character referral code from your friend.",
            parse_mode="HTML",
        )
        return
    code = args[0].strip().upper()
    await _do_redeem(update, context, code, source="command")


async def text_code_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catch a bare 6-char code typed after /debut.

    ONLY processes the message if:
      - It's plain text matching the code pattern
      - The user's `awaiting_referral_code` flag is set (just debuted)

    Otherwise lets the message fall through to other handlers.
    """
    if not getattr(context, "user_data", None):
        return
    if not context.user_data.get("awaiting_referral_code"):
        return

    msg = update.message
    if not msg or not msg.text:
        return

    code = _looks_like_code(msg.text)
    if not code:
        return
    # Mark consumed so they can't trigger this again with random text
    context.user_data["awaiting_referral_code"] = False
    await _do_redeem(update, context, code, source="text_after_debut")


async def _do_redeem(update, context, code, source="command"):
    tg_user = update.effective_user
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text(
                "❌ Do /debut first to create your account.")
            return

        # Pre-flight: check the code shape so we give a clean error
        if not _looks_like_code(code):
            await update.message.reply_text(
                "⚠️ That doesn't look like a valid code.\n\n"
                "Codes are 6 characters — letters and digits, no spaces.\n"
                "Try: <code>/redeem ABCD23</code>",
                parse_mode="HTML")
            return

        result = redeem_code(session, code, user.id)
        if not result["ok"]:
            err = result.get("error")
            if err == "invalid_code":
                await update.message.reply_text(
                    f"❌ Code <code>{code}</code> doesn't match any user.\n"
                    "Double-check spelling and try again.",
                    parse_mode="HTML")
            elif err == "self_referral":
                await update.message.reply_text(
                    "❌ That's your own code — can't redeem it on yourself.")
            elif err == "already_redeemed":
                await update.message.reply_text(
                    "❌ You've already redeemed a referral code.")
            else:
                await update.message.reply_text(
                    f"❌ {result.get('message', 'Could not redeem code.')}")
            return

        session.commit()
        # Success — tell the user + thank them
        bits = []
        if result["paid_coins"] > 0: bits.append(f"+{result['paid_coins']:,} 🪙")
        if result["paid_gems"] > 0: bits.append(f"+{result['paid_gems']} 💎")
        reward = (" Your inviter just earned " + " ".join(bits) + "."
                  if bits else "")
        await update.message.reply_text(
            f"✅ <b>Code redeemed!</b>{reward}\n\n"
            "Thanks for joining via referral! 🎉",
            parse_mode="HTML")
    except Exception:
        session.rollback()
        logger.exception("Redeem failed")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


async def refcode_skip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inline button callback to dismiss the post-debut referral prompt."""
    q = update.callback_query
    try:
        await q.answer("OK — you can always /redeem later.")
        if getattr(context, "user_data", None):
            context.user_data["awaiting_referral_code"] = False
        try:
            await q.edit_message_text(
                "⏭️ Skipped. Use <code>/redeem CODE</code> any time later.",
                parse_mode="HTML")
        except Exception:
            pass
    except Exception:
        logger.exception("Skip callback failed")

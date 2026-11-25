"""/coins2gems — convert Coins into Gems at a fixed, irreversible rate.

Usage:
    /coins2gems 5000     → converts 5,000 coins into 5 gems

The rate is ``config.COINS_PER_GEM`` (1000 coins = 1 gem). The amount is the
number of COINS to spend; it is rounded DOWN to a whole multiple of the rate so
no fractional gems are ever created and no coins are lost silently beyond the
remainder. Because the swap can never be reversed, the command always asks for
an explicit Confirm before touching the balance, and re-validates the balance at
confirm time (guards against double-taps and stale button data).
"""

import logging
import re

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import COINS_PER_GEM
from database import get_session
from models import MiniappEconomyIntent, User
from services.economy_transactions import lock_user_for_update
from services.miniapp_economy import EconomyRejected, create_quote, settle

logger = logging.getLogger(__name__)


def _fmt(n: int) -> str:
    return f"{int(n):,}"


async def coins2gems_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    session = get_session()
    try:
        user = (session.query(User)
                .filter(User.telegram_id == tg_user.id).first())
        if not user:
            from services.message_service import get_msg
            await update.message.reply_text(get_msg("not_debuted"))
            return

        coins_balance = user.total_coins or 0
        gems_balance = user.total_gems or 0

        raw = (ctx.args[0].strip() if ctx.args else "").replace(",", "")
        if not raw:
            await update.message.reply_text(
                "💱 <b>/coins2gems</b> — convert Coins into Gems.\n\n"
                f"Rate: <b>{_fmt(COINS_PER_GEM)} coins = 1 gem</b> "
                "(one-way, cannot be undone).\n\n"
                "Usage: <code>/coins2gems &lt;coins&gt;</code>\n"
                "Example: <code>/coins2gems 5000</code> → 5 gems\n\n"
                f"💰 Your balance: <b>{_fmt(coins_balance)}</b> coins · "
                f"💎 <b>{_fmt(gems_balance)}</b> gems",
                parse_mode="HTML",
            )
            return

        try:
            coins = int(raw)
        except ValueError:
            await update.message.reply_text(
                "⚠️ Please give a whole number of coins, e.g. "
                "<code>/coins2gems 5000</code>.",
                parse_mode="HTML",
            )
            return

        if coins <= 0:
            await update.message.reply_text(
                "⚠️ Amount must be a positive number of coins.")
            return

        # Round DOWN to a whole multiple of the rate — no fractional gems.
        spend = (coins // COINS_PER_GEM) * COINS_PER_GEM
        gems = spend // COINS_PER_GEM

        if gems < 1:
            await update.message.reply_text(
                f"⚠️ You need at least <b>{_fmt(COINS_PER_GEM)}</b> coins to get "
                "1 gem.",
                parse_mode="HTML",
            )
            return

        if spend > coins_balance:
            await update.message.reply_text(
                f"⚠️ Not enough coins. You have <b>{_fmt(coins_balance)}</b> but "
                f"that conversion needs <b>{_fmt(spend)}</b>.",
                parse_mode="HTML",
            )
            return

        note = ""
        if spend != coins:
            note = (f"\n<i>(rounded down to {_fmt(spend)} — coins convert in "
                    f"steps of {_fmt(COINS_PER_GEM)})</i>")

        user = lock_user_for_update(session, user)
        try:
            quote = create_quote(session, user, "convert", spend)
        except EconomyRejected as error:
            session.rollback()
            await update.message.reply_text(str(error))
            return
        quote_id = quote["quote_id"]
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm", callback_data=f"c2g:{quote_id}"),
            InlineKeyboardButton("✖️ Cancel", callback_data=f"c2g:cancel:{quote_id}"),
        ]])
        await update.message.reply_text(
            f"💱 <b>Convert {_fmt(spend)} coins → {_fmt(gems)} 💎 gems?</b>{note}\n\n"
            f"Balance after: <b>{_fmt(quote['balance_after'])}</b> coins · "
            f"<b>{_fmt(quote['gems_after'])}</b> gems.\n"
            "This conversion cannot be undone. Quote expires in 5 minutes.",
            parse_mode="HTML", reply_markup=keyboard,
        )
    except Exception:
        logger.exception("coins2gems handler failed")
        await update.message.reply_text("⚠️ Something went wrong. Please try again.")
    finally:
        session.close()


async def coins2gems_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    payload = (query.data or "").removeprefix("c2g:")
    cancel = payload.startswith("cancel:")
    quote_id = payload.removeprefix("cancel:")
    if not re.fullmatch(r"[0-9a-f]{32}", quote_id):
        await query.answer("Request a new conversion quote.", show_alert=True)
        return

    session = get_session()
    try:
        user = session.query(User).filter_by(telegram_id=query.from_user.id).first()
        if not user:
            await query.answer("Account not found.", show_alert=True)
            return
        user = lock_user_for_update(session, user)
        intent = session.query(MiniappEconomyIntent).filter_by(
            id=quote_id, user_id=user.id, action="convert").first()
        if not intent:
            session.rollback()
            await query.answer("This conversion is unavailable for your account.", show_alert=True)
            return
        if cancel and not intent.result:
            session.delete(intent)
            session.commit()
            await query.answer("Cancelled")
            await query.edit_message_text("Conversion cancelled.")
            return
        try:
            receipt = settle(session, user, intent)
        except EconomyRejected as error:
            session.rollback()
            await query.answer(str(error), show_alert=True)
            return
        session.rollback()
        await query.answer("Converted!")
        balance = receipt["balance"]
        await query.edit_message_text(
            f"✅ <b>Received {_fmt(receipt['gems'])} gems.</b>\n\n"
            f"Coins: <b>{_fmt(balance['coins'])}</b> · Gems: <b>{_fmt(balance['gems'])}</b>\n"
            f"Receipt <code>{quote_id}</code>",
            parse_mode="HTML",
        )
    except Exception:
        session.rollback()
        logger.exception("coins2gems confirm failed")
        await query.answer("Check your Wallet for the result.", show_alert=True)
    finally:
        session.close()

"""/givepack — owner-only: drop packs into a user's unopened-pack inventory.

Usage:
    /givepack <pack_slot|pack_id> <telegram_id> [qty]

The pack is matched by its slot number first (the number shown in /buypack),
then by its database id. ``qty`` defaults to 1 and is capped at
``MAX_QTY``. Packs land unopened, exactly like an admin grant from the website
(``source="admin"``); the user is DMed and opens them with /openpack.
"""

import html
import logging

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from database import get_session
from models import Pack, User
from services.admin_ids import is_owner

logger = logging.getLogger(__name__)

MAX_QTY = 20


def _usage() -> str:
    return ("📦 <b>/givepack</b> — give packs to a user (owner only)\n\n"
            "<code>/givepack &lt;pack_slot|pack_id&gt; &lt;telegram_id&gt; [qty]</code>\n"
            f"qty defaults to 1, max {MAX_QTY}.")


def _find_pack(session, key: int):
    pack = session.query(Pack).filter(Pack.slot_number == key).first()
    return pack or session.get(Pack, key)


async def givepack_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return  # Silent for non-owners.

    args = list(ctx.args or [])
    if len(args) < 2:
        await update.message.reply_text(_usage(), parse_mode="HTML")
        return
    try:
        pack_key = int(args[0])
        target_tg_id = int(args[1])
        qty = int(args[2]) if len(args) > 2 else 1
    except ValueError:
        await update.message.reply_text("⚠️ Pack, Telegram ID and qty must be numbers.\n\n"
                                        + _usage(), parse_mode="HTML")
        return
    if not 1 <= qty <= MAX_QTY:
        await update.message.reply_text(f"⚠️ qty must be between 1 and {MAX_QTY}.")
        return

    session = get_session()
    try:
        pack = _find_pack(session, pack_key)
        if not pack:
            await update.message.reply_text(f"⚠️ No pack with slot or id {pack_key}.")
            return
        user = session.query(User).filter(User.telegram_id == target_tg_id).first()
        if not user:
            await update.message.reply_text(
                f"⚠️ No user found with Telegram ID <code>{target_tg_id}</code>. "
                "They must /debut first.", parse_mode="HTML")
            return

        from services.pack_service import grant_pack
        from services.activity_service import log_activity
        for _ in range(qty):
            grant_pack(session, user.id, pack.id, source="admin")
        pack_label = f"{pack.emoji or '📦'} {pack.name}"
        log_activity(session, user.id, "admin_grant_pack",
                     f"Owner granted {qty}× {pack.name}")
        session.commit()

        name = html.escape(user.username or user.first_name or f"#{user.id}")
        dm_ok = True
        try:
            await ctx.bot.send_message(
                chat_id=user.telegram_id,
                text=(f"🎁 You received <b>{qty}× {html.escape(pack_label)}</b>!\n"
                      "Open it with /openpack."),
                parse_mode="HTML")
        except TelegramError:
            dm_ok = False
        await update.message.reply_text(
            f"✅ Gave {qty}× {html.escape(pack_label)} to {name}."
            + ("\n📬 User notified." if dm_ok else "\n⚠️ Could not DM the user."),
            parse_mode="HTML")
        logger.info("Owner %s gave %sx pack %s to user %s",
                    update.effective_user.id, qty, pack.id, user.id)
    except Exception:
        session.rollback()
        logger.exception("givepack failed")
        await update.message.reply_text("⚠️ Could not give the pack. Try again.")
    finally:
        session.close()

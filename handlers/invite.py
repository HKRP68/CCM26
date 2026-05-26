"""`/invite` bot command — shows the user's referral link and stats."""

import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User
from services.referral_service import (
    get_active_competition, get_user_stats, get_invite_link,
)

logger = logging.getLogger(__name__)


async def invite_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text(
                "❌ Do /debut first to create your account.")
            return

        bot_username = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
        if not bot_username:
            await update.message.reply_text(
                "⚠️ <b>Invite system not configured.</b>\n\n"
                "<i>Admin: set the <code>BOT_USERNAME</code> env var.</i>",
                parse_mode="HTML",
            )
            return

        link = get_invite_link(bot_username, tg_user.id)
        comp = get_active_competition(session)
        stats = get_user_stats(session, user.id,
                               competition_id=comp.id if comp else None)

        # Build the message
        lines = ["🎟️ <b>Invite Friends</b>\n"]

        if comp:
            lines.append(f"📅 <b>{escape_html(comp.name)}</b>")
            ends_str = comp.end_date.strftime("%b %d, %Y")
            lines.append(f"<i>Ends: {ends_str} UTC</i>\n")

            if comp.prize_per_invite > 0 or comp.prize_per_invite_gems > 0:
                pi_parts = []
                if comp.prize_per_invite > 0: pi_parts.append(f"{comp.prize_per_invite:,} 🪙")
                if comp.prize_per_invite_gems > 0: pi_parts.append(f"{comp.prize_per_invite_gems} 💎")
                lines.append(f"🎁 Per invite: <b>{' + '.join(pi_parts)}</b>")

            if comp.prize_top1 > 0 or comp.prize_top2 > 0 or comp.prize_top3 > 0:
                lines.append("\n🏆 <b>Top prizes:</b>")
                if comp.prize_top1 > 0: lines.append(f"  🥇 1st: {comp.prize_top1:,} 🪙")
                if comp.prize_top2 > 0: lines.append(f"  🥈 2nd: {comp.prize_top2:,} 🪙")
                if comp.prize_top3 > 0: lines.append(f"  🥉 3rd: {comp.prize_top3:,} 🪙")

            lines.append(f"\n<b>Your stats:</b>")
            lines.append(f"✅ Joined: <b>{stats['completed']}</b>")
            if stats['pending'] > 0:
                lines.append(f"⏳ Pending (clicked, not signed up): {stats['pending']}")
            if stats['rank']:
                lines.append(f"🏆 Rank: <b>#{stats['rank']}</b> of {stats['total_competitors']}")
        else:
            lines.append("<i>No active competition right now — invites still recorded.</i>\n")
            lines.append(f"<b>Lifetime invites:</b> {stats['lifetime_completed']}")

        lines.append("\n📋 <b>Your invite link:</b>")
        lines.append(f"<code>{link}</code>")
        lines.append("\nShare with friends. When they do /debut, you get credit.")

        # Telegram has a "share" deep-link that opens a chat picker
        share_url = (
            f"https://t.me/share/url?url={link}"
            f"&text=Join%20me%20in%20Cricket%20Simulator%20Bot%21"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Share invite", url=share_url)],
        ])
        await update.message.reply_text(
            "\n".join(lines), parse_mode="HTML", reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception:
        logger.exception("Invite handler failed")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


def escape_html(s):
    return (str(s or "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))

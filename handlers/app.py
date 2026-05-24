"""/app command — opens the Telegram Mini App for the user.

The Mini App URL is set via the WEBAPP_URL environment variable. If not
set, the command tells the admin to configure it.

Telegram only opens https URLs as Mini Apps. Render.com gives you HTTPS
out of the box; for local dev use a tunnel like ngrok.
"""

import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def app_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    webapp_url = os.getenv("WEBAPP_URL", "").strip()
    if not webapp_url:
        await update.message.reply_text(
            "⚠️ <b>Mini App not configured.</b>\n\n"
            "<i>Admin: set the <code>WEBAPP_URL</code> env var to your "
            "deployed URL ending in <code>/webapp</code>.</i>",
            parse_mode="HTML",
        )
        return
    if not webapp_url.startswith("https://"):
        await update.message.reply_text(
            "⚠️ Mini App URL must be HTTPS. Current: " + webapp_url[:80],
            parse_mode="HTML",
        )
        return

    # Telegram only renders WebApp buttons in PRIVATE chats reliably.
    # In groups it shows a link instead.
    button = InlineKeyboardButton(
        "🏏 Open CricMaster",
        web_app=WebAppInfo(url=webapp_url),
    )
    kb = InlineKeyboardMarkup([[button]])

    await update.message.reply_text(
        "🎮 <b>CricMaster Mini App</b>\n\n"
        "Tap the button below to open the app. Spin the wheel, search players, "
        "manage your roster — all in one place.\n\n"
        "<i>📺 Watch a quick ad to unlock each spin.</i>",
        parse_mode="HTML",
        reply_markup=kb,
    )

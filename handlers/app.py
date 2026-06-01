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

    # WebApp buttons only work in PRIVATE chats. In groups, fall back to a
    # url= deep link.
    chat_type = (update.effective_chat.type
                 if update.effective_chat else "private")
    is_private = (chat_type == "private")

    text = ("🎮 <b>CricMaster Mini App</b>\n\n"
            "Tap the button below to open the app. Spin the wheel, search "
            "players, manage your roster — all in one place.\n\n"
            "<i>📺 Watch a quick ad to unlock each spin.</i>")

    if is_private:
        button = InlineKeyboardButton(
            "🏏 Open CricMaster",
            web_app=WebAppInfo(url=webapp_url),
        )
    else:
        # Group chat — must use url= button. Both deep-link forms below open
        # the Mini App directly (no DM bounce); `/app` just opens it on home.
        bot_username = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
        miniapp_name = os.getenv("MINIAPP_NAME", "").strip()
        if bot_username and miniapp_name:
            # Named Mini App — t.me/<bot>/<app> launches it straight away
            deep_link = f"https://t.me/{bot_username}/{miniapp_name}"
        elif bot_username:
            # Bot's main Mini App (BotFather) — `?startapp` launches the app
            # directly instead of opening a DM chat
            deep_link = f"https://t.me/{bot_username}?startapp=home"
        else:
            await update.message.reply_text(
                "⚠️ Mini App buttons don't work in groups. Open this in a "
                "private chat with the bot.\n\n"
                "<i>Admin: set BOT_USERNAME env var for proper group support.</i>",
                parse_mode="HTML",
            )
            return
        button = InlineKeyboardButton("🏏 Open CricMaster", url=deep_link)

    kb = InlineKeyboardMarkup([[button]])
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

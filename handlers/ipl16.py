"""/ipl160 command — opens the "16-0" IPL season simulator Mini App.

16-0 is a self-contained, client-side game (draft an IPL XI, simulate a
season). It is served as static files at <WEBAPP_URL>/ipl16/ by the Flask
admin app (see admin.py) and opened here as a Telegram Web App.

The Mini App URL is derived from the WEBAPP_URL environment variable. Telegram
only opens https URLs as Mini Apps. Render.com gives you HTTPS out of the box;
for local dev use a tunnel like ngrok.

This command is also reachable by typing the literal "/16-0IPL" — that string
is not a valid Telegram command (hyphen + uppercase), so bot.py routes it here
via a text matcher instead of a CommandHandler.
"""

import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def ipl160_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Reuse the cricket Mini App host helper: WEBAPP_URL often points at
    # /webapp, but the 16-0 game is served at the site root (/ipl16/), so we
    # reduce WEBAPP_URL to scheme+host before appending the game path.
    from services.match_broadcast import _webapp_host
    host = _webapp_host()
    if not host:
        await update.message.reply_text(
            "⚠️ <b>16-0 Mini App not configured.</b>\n\n"
            "<i>Admin: set the <code>WEBAPP_URL</code> env var to your "
            "deployed HTTPS URL. The game is served at "
            "<code>https://&lt;host&gt;/ipl16/</code>.</i>",
            parse_mode="HTML",
        )
        return

    game_url = f"{host}/ipl16/"

    # WebApp buttons only work in PRIVATE chats. In groups, fall back to a
    # url= deep link that launches the bot's Mini App.
    chat_type = (update.effective_chat.type
                 if update.effective_chat else "private")
    is_private = (chat_type == "private")

    text = ("🏏 <b>16-0 — Can your team go undefeated?</b>\n\n"
            "Draft your franchise, build an XI from 800+ players across "
            "2008–2026, and simulate a full IPL season.\n\n"
            "Tap below to open the <b>16-0 IPL Simulator</b>.")

    if is_private:
        button = InlineKeyboardButton(
            "🏏 Open 16-0 IPL Sim",
            web_app=WebAppInfo(url=game_url),
        )
    else:
        # Group chat — must use a url= button. Deep-link into the bot's Mini
        # App (named app if MINIAPP_NAME is set), tagging this app with
        # startapp=ipl16 so it can route to the 16-0 game.
        bot_username = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
        miniapp_name = os.getenv("MINIAPP_NAME", "").strip()
        if bot_username and miniapp_name:
            deep_link = f"https://t.me/{bot_username}/{miniapp_name}?startapp=ipl16"
        elif bot_username:
            deep_link = f"https://t.me/{bot_username}?startapp=ipl16"
        else:
            await update.message.reply_text(
                "⚠️ Mini App buttons don't work in groups. Open this in a "
                "private chat with the bot.\n\n"
                "<i>Admin: set BOT_USERNAME env var for proper group support.</i>",
                parse_mode="HTML",
            )
            return
        button = InlineKeyboardButton("🏏 Open 16-0 IPL Sim", url=deep_link)

    kb = InlineKeyboardMarkup([[button]])
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

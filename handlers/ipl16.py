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
        # Private chat — open as a proper Telegram Mini App (overlay + SDK).
        button = InlineKeyboardButton(
            "🏏 Open 16-0 IPL Sim",
            web_app=WebAppInfo(url=game_url),
        )
    else:
        # Group chat — web_app buttons are rejected by Telegram here. The game
        # is fully standalone (no initData needed), so point a plain url= button
        # straight at it: Telegram opens the game in its in-app browser. This
        # opens the GAME directly, not the bot's main Mini App dashboard.
        button = InlineKeyboardButton("🏏 Open 16-0 IPL Sim", url=game_url)

    kb = InlineKeyboardMarkup([[button]])
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

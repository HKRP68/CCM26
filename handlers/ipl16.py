"""/ipl160 command — opens the "CMU 16-0" IPL season simulator Mini App.

CMU 16-0 is a self-contained, client-side game (draft an IPL XI, simulate a
season). It is served as static files at <WEBAPP_URL host>/ipl16/ by the Flask
admin app (see admin.py) and opened here as a Telegram Web App.

The Mini App URL is derived from the WEBAPP_URL environment variable. Telegram
only opens https URLs as Mini Apps. Render.com gives you HTTPS out of the box;
for local dev use a tunnel like ngrok.

This command is also reachable by typing the literal "/16-0IPL" — that string
is not a valid Telegram command (hyphen + uppercase), so bot.py routes it here
via a text matcher instead of a CommandHandler. It is also reached via the
"/start ipl160" deep link (handlers/bot.py) used by the group button and the
result-share "Play" button.
"""

import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# Deep-link payload that opens the game in a private chat (via /start).
IPL160_START_PAYLOAD = "ipl160"


def _game_host():
    """Scheme+host for the Mini App, or None if WEBAPP_URL isn't a valid https URL."""
    from services.match_broadcast import _webapp_host
    return _webapp_host()


async def send_ipl160_launch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Send the message + button that opens CMU 16-0.

    Shared by the /ipl160 command, the /16-0IPL text matcher and the
    "/start ipl160" deep link, so every entry point opens the same game.
    """
    host = _game_host()
    if not host:
        await update.message.reply_text(
            "⚠️ <b>CMU 16-0 Mini App not configured.</b>\n\n"
            "<i>Admin: set the <code>WEBAPP_URL</code> env var to your "
            "deployed HTTPS URL. The game is served at "
            "<code>https://&lt;host&gt;/ipl16/</code>.</i>",
            parse_mode="HTML",
        )
        return

    game_url = f"{host}/ipl16/"

    chat_type = (update.effective_chat.type
                 if update.effective_chat else "private")
    is_private = (chat_type == "private")

    text = ("🏏 <b>CMU 16-0 — Can your team go undefeated?</b>\n\n"
            "Draft your franchise, build an XI from 800+ players across "
            "2008–2026, and simulate a full IPL season.\n\n"
            "Tap below to open <b>CMU 16-0</b>.")

    if is_private:
        # Private chat — open as a proper Telegram Mini App (overlay + SDK).
        # initData is available here, so in-app image sharing works.
        button = InlineKeyboardButton(
            "🏏 Open CMU 16-0",
            web_app=WebAppInfo(url=game_url),
        )
    else:
        # Group chat — web_app buttons are rejected by Telegram. Deep-link into
        # the bot's DM with the ipl160 payload so /start opens the game there as
        # a real Mini App (so initData → image sharing works). This opens the
        # GAME, not the bot's main dashboard. Falls back to the raw game URL
        # (in-app browser) when BOT_USERNAME isn't configured.
        bot_username = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
        if bot_username:
            deep_link = f"https://t.me/{bot_username}?start={IPL160_START_PAYLOAD}"
        else:
            deep_link = game_url
        button = InlineKeyboardButton("🏏 Open CMU 16-0", url=deep_link)

    kb = InlineKeyboardMarkup([[button]])
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


# Backwards-compatible command/text-matcher entry point.
async def ipl160_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await send_ipl160_launch(update, ctx)

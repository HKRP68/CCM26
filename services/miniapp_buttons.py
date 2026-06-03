"""Helpers for building Telegram Mini App buttons and deep links."""

import os
from telegram import InlineKeyboardButton, WebAppInfo


def miniapp_button(label, tab, *, is_private=True):
    """Return an InlineKeyboardButton that opens the Mini App on ``tab``.

    Private chats can use Telegram's native WebApp button. Groups must use a
    t.me deep link because Telegram rejects WebApp buttons outside DMs.
    Returns ``None`` when the required Mini App configuration is missing.
    """
    webapp_url = os.getenv("WEBAPP_URL", "").strip()
    if is_private and webapp_url.startswith("https://"):
        return InlineKeyboardButton(label, web_app=WebAppInfo(url=f"{webapp_url}#{tab}"))

    bot_username = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
    if not bot_username:
        return None

    miniapp_name = os.getenv("MINIAPP_NAME", "").strip()
    if miniapp_name:
        deep_link = f"https://t.me/{bot_username}/{miniapp_name}?startapp={tab}"
    else:
        deep_link = f"https://t.me/{bot_username}?startapp={tab}"
    return InlineKeyboardButton(label, url=deep_link)


def has_miniapp_url():
    return os.getenv("WEBAPP_URL", "").strip().startswith("https://")

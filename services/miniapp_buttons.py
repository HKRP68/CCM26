"""Helpers for building Telegram Mini App buttons and deep links."""

import os
from telegram import InlineKeyboardButton, WebAppInfo


def miniapp_button(label, tab, *, is_private=True, origin_chat_id=None):
    """Return an InlineKeyboardButton that opens the Mini App on ``tab``.

    Private chats can use Telegram's native WebApp button. Groups must use a
    t.me deep link because Telegram rejects WebApp buttons outside DMs.

    When ``origin_chat_id`` is a group/supergroup id (negative), it is encoded
    into the group deep link's start_param as ``<tab>_c<chatId>`` so the Mini App
    knows which chat it was launched from and can echo activity back there.
    Returns ``None`` when the required Mini App configuration is missing.
    """
    webapp_url = os.getenv("WEBAPP_URL", "").strip()
    if is_private and webapp_url.startswith("https://"):
        return InlineKeyboardButton(label, web_app=WebAppInfo(url=f"{webapp_url}#{tab}"))

    deep_link = miniapp_deep_link(tab, origin_chat_id=origin_chat_id)
    if not deep_link:
        return None
    return InlineKeyboardButton(label, url=deep_link)


def miniapp_deep_link(tab, *, origin_chat_id=None):
    """Return a t.me deep link (string) that opens the Mini App on ``tab``.

    Unlike :func:`miniapp_button`, this returns a plain URL suitable for an
    HTML ``<a href>`` text link inside a message. A named Mini App is required:
    the ``t.me/<bot>?startapp=...`` fallback opens the bot's DM when the bot
    does not have a Main Mini App configured, rather than reliably opening the
    requested Mini App.
    """
    bot_username = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
    miniapp_name = os.getenv("MINIAPP_NAME", "").strip()
    if not bot_username or not miniapp_name:
        return None

    start_param = tab
    try:
        if origin_chat_id is not None and int(origin_chat_id) < 0:
            start_param = f"{tab}_c{int(origin_chat_id)}"
    except (ValueError, TypeError):
        pass

    return f"https://t.me/{bot_username}/{miniapp_name}?startapp={start_param}"


def has_miniapp_url():
    return os.getenv("WEBAPP_URL", "").strip().startswith("https://")

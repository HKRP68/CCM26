"""Helpers for building Telegram Mini App buttons and deep links."""

import os
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
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


def auction_launch(value):
    """Parse an auction route hint; the API separately checks auction access."""
    match = re.fullmatch(r'auction(?:_(\d+))?(?:_c(-\d+))?', (value or '').strip())
    if not match:
        return None
    return dict(season_id=int(match[1]) if match[1] else None,
                chat_id=int(match[2]) if match[2] else None)


def franchise_website_url(season):
    """Build an optional standalone-site link; route hints grant no access.

    Reject unsafe or incomplete configuration and leave the established Mini
    App available. The website authenticates Telegram users and independently
    verifies their access to the hinted season/group.
    """
    value = os.getenv("FRANCHISE_WEBSITE_URL", "").strip()
    if not value or any(char.isspace() or ord(char) < 32 for char in value):
        return None
    try:
        parts = urlsplit(value)
        if (parts.scheme != "https" or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.fragment):
            return None
        # Accessing port also rejects malformed values before building a button.
        parts.port
        season_id = int(season.id)
        if season_id <= 0:
            return None
        query = [(key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True)
                 if key not in ("season_id", "chat_id")]
        query.append(("season_id", str(season_id)))
        try:
            chat_id = int(season.chat_id)
            if chat_id < 0:
                query.append(("chat_id", str(chat_id)))
        except (AttributeError, TypeError, ValueError):
            pass
        return urlunsplit((parts.scheme, parts.netloc, parts.path or "/",
                           urlencode(query), ""))
    except (AttributeError, TypeError, ValueError):
        return None


def auction_button(season, *, is_private=False):
    website_url = franchise_website_url(season)
    if website_url:
        return InlineKeyboardButton('🌐 Open auction website', url=website_url)
    return miniapp_button('📱 Switch to Miniapp', f'auction_{season.id}',
                          is_private=is_private, origin_chat_id=season.chat_id)

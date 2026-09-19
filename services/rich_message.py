"""Bot API 10.1 rich messages (``sendRichMessage``) with an HTML fallback.

A rich message is not a ``parse_mode``. The text is a tree of typed blocks —
headings, native tables, collapsible ``details`` — posted to its own endpoint,
so a surface that wants one needs a second renderer alongside its HTML one.
python-telegram-bot is still on Bot API 10.0 and ships no ``send_rich_message``,
so the payload goes out through ``Bot._post``: the same hop every PTB method
uses, which keeps the rate limiter, the base URL and the error parsing.

Every sender here takes the surface's existing HTML as ``fallback_text`` and
falls back to a plain ``sendMessage`` whenever the rich send is refused — a
self-hosted Bot API server below 10.1, or a payload Telegram rejects. Nothing
in this module is allowed to lose the message.

The block and text shapes below follow the Bot API 10.1 schema: exactly one of
``html``/``markdown``/``blocks`` per message, ``align`` and ``valign`` required
on every table cell, heading ``size`` 1-6 with 1 the largest.
"""

import logging

from telegram import Message
from telegram.error import InvalidToken, TelegramError

import config

logger = logging.getLogger(__name__)

# Latched once the server answers that it has no such method, so a bot pointed
# at an older Bot API server pays for exactly one refused call per process
# instead of one per rendered XI.
_unsupported = False

# Descriptions Telegram uses when the endpoint itself is missing, as opposed to
# a payload it did not like.
_MISSING_METHOD_HINTS = ("method not found", "method is not available",
                         "unknown method", "not supported")


def rich_text_enabled() -> bool:
    """True when rich messages should be attempted at all."""
    return bool(getattr(config, "RICH_TEXT_ENABLED", False)) and not _unsupported


def _note_failure(endpoint: str, exc: BaseException) -> None:
    """Log a refused rich send, latching off when the method is missing."""
    global _unsupported
    description = str(exc).lower()
    missing = isinstance(exc, InvalidToken) or any(
        hint in description for hint in _MISSING_METHOD_HINTS)
    if missing:
        _unsupported = True
        logger.warning(
            "%s unavailable (%s) — falling back to HTML for this process",
            endpoint, exc)
    else:
        logger.warning("%s refused (%s) — falling back to HTML", endpoint, exc)


def reset_support_latch() -> None:
    """Re-arm rich sends after a ``_unsupported`` latch. For tests."""
    global _unsupported
    _unsupported = False


# ── Rich text nodes ──────────────────────────────────────────────────
# A RichText is a plain string, a list of RichText, or one of these nodes —
# each of which nests another RichText in ``text``.

def bold(text):
    return {"type": "bold", "text": text}


def italic(text):
    return {"type": "italic", "text": text}


def underline(text):
    return {"type": "underline", "text": text}


def code(text):
    return {"type": "code", "text": text}


def spoiler(text):
    return {"type": "spoiler", "text": text}


def link(text, url: str):
    return {"type": "url", "text": text, "url": url}


# ── Blocks ───────────────────────────────────────────────────────────

def heading(text, size: int = 3):
    """A section heading. ``size`` is 1-6, 1 being the largest."""
    return {"type": "heading", "text": text, "size": size}


def paragraph(text):
    return {"type": "paragraph", "text": text}


def footer(text):
    return {"type": "footer", "text": text}


def divider():
    return {"type": "divider"}


def cell(text=None, *, header: bool = False, align: str = "left",
         valign: str = "middle", colspan: int = None, rowspan: int = None):
    """One table cell. ``text=None`` leaves the cell invisible.

    ``align`` and ``valign`` are required by the API, so they are always sent.
    """
    block = {"align": align, "valign": valign}
    if text is not None:
        block["text"] = text
    if header:
        block["is_header"] = True
    if colspan:
        block["colspan"] = colspan
    if rowspan:
        block["rowspan"] = rowspan
    return block


def table(rows, *, bordered: bool = False, striped: bool = False,
          compact: bool = False, caption=None):
    """A table from a list of rows, each a list of :func:`cell` dicts."""
    block = {"type": "table", "cells": rows}
    if bordered:
        block["is_bordered"] = True
    if striped:
        block["is_striped"] = True
    if compact:
        block["is_compact"] = True
    if caption is not None:
        block["caption"] = caption
    return block


def details(summary, blocks, *, is_open: bool = False):
    """A collapsible block: ``summary`` always shown, ``blocks`` behind it."""
    block = {"type": "details", "summary": summary, "blocks": blocks}
    if is_open:
        block["is_open"] = True
    return block


# ── Senders ──────────────────────────────────────────────────────────

async def send_rich_message(bot, chat_id, blocks, fallback_text, *,
                            reply_markup=None, reply_to_message_id=None,
                            **kwargs):
    """Send ``blocks`` as a rich message, or ``fallback_text`` as HTML.

    Returns the sent :class:`telegram.Message`. ``kwargs`` are passed to the
    HTML fallback only, since the two endpoints do not take the same options.
    """
    if rich_text_enabled() and blocks:
        payload = {"chat_id": chat_id, "rich_message": {"blocks": blocks}}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if reply_to_message_id is not None:
            payload["reply_parameters"] = {"message_id": reply_to_message_id}
        try:
            result = await bot._post("sendRichMessage", payload)
            return Message.de_json(result, bot)
        except TelegramError as exc:
            _note_failure("sendRichMessage", exc)

    return await bot.send_message(
        chat_id, fallback_text, parse_mode="HTML", reply_markup=reply_markup,
        reply_to_message_id=reply_to_message_id,
        disable_web_page_preview=True, **kwargs)


async def edit_rich_message(bot, chat_id, message_id, blocks, *,
                            reply_markup=None) -> bool:
    """Replace a message's contents with ``blocks``.

    Returns True on success. On refusal the caller still holds its HTML path,
    so this reports failure instead of raising.
    """
    if not (rich_text_enabled() and blocks):
        return False
    payload = {"chat_id": chat_id, "message_id": message_id,
               "rich_message": {"blocks": blocks}}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        await bot._post("editMessageText", payload)
        return True
    except TelegramError as exc:
        _note_failure("editMessageText", exc)
        return False

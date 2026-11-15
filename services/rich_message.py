"""Bot API 10.4 rich messages with a formatted compatibility fallback.

A rich message is not a ``parse_mode``. The text is a tree of typed blocks —
headings, native tables, collapsible ``details`` — posted to its own endpoint,
and existing HTML can also be sent natively. Specialized surfaces add block
renderers; rich_message_defaults covers ordinary text sends across the bot.
The installed SDK predates these methods, so their payloads go through
``Bot._post``: the same authenticated transport every PTB method
uses, which keeps the rate limiter, the base URL and the error parsing.

Every sender accepts existing HTML as ``fallback_text``. A rejected block tree
is retried using native rich HTML, preserving tables and sections. An unavailable
endpoint or transport finally uses formatted HTML through ``sendMessage``.

The block and text shapes below follow the Bot API 10.4 schema: exactly one of
``html``/``markdown``/``blocks`` per message, ``align`` and ``valign`` required
on every table cell, heading ``size`` 1-6 with 1 the largest.

Two conveniences sit on top of the raw senders, because most surfaces in this
bot already hold a ``Message`` or a ``CallbackQuery`` rather than a bot and a
chat id: :func:`reply_rich` answers a command, :func:`edit_rich` redraws a card
behind a button. Both take the surface's existing HTML as the fallback, and
both split an over-long fallback rather than letting Telegram refuse it.
"""

import asyncio
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import logging
import re
import time

from telegram import Message
from telegram.error import (
    BadRequest,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
)

import config

logger = logging.getLogger(__name__)

# Explicit compatibility override; automatic failures are cached per bot and
# operation for a limited time, so one old server cannot disable another bot.
_unsupported = False
_capabilities = OrderedDict()
_legacy_delivery = ContextVar("cmu_rich_legacy_delivery", default=False)
NATIVE_TIMEOUT_SECONDS = 8.0
NATIVE_COOLDOWN_SECONDS = 5.0
UNSUPPORTED_RETRY_SECONDS = 300.0

# Descriptions Telegram uses when the endpoint itself is missing, as opposed to
# a payload it did not like.
_MISSING_METHOD_HINTS = (
    "method not found",
    "method is not available",
    "unknown method",
)

# Telegram refuses a text message over 4,096 characters outright. The HTML
# fallbacks are cut under that rather than lost.
CHUNK_LIMIT = 3800


def legacy_delivery_active():
    return _legacy_delivery.get()


@contextmanager
def legacy_delivery():
    """Avoid promoting an already attempted rich helper's fallback a second time."""
    token = _legacy_delivery.set(True)
    try:
        yield
    finally:
        _legacy_delivery.reset(token)


def _state(bot, endpoint):
    key = (id(bot), endpoint)
    if key not in _capabilities:
        if len(_capabilities) >= 64:
            _capabilities.popitem(last=False)
        _capabilities[key] = dict(bot=bot, retry_after=0.0, unsupported_until=0.0)
    return _capabilities[key]


def rich_text_enabled(bot=None, endpoint="sendRichMessage") -> bool:
    """True when rich messages should be attempted at all."""
    enabled = bool(getattr(config, "RICH_TEXT_ENABLED", False)) and not _unsupported
    if not enabled or bot is None:
        return enabled
    state = _state(bot, endpoint)
    return time.monotonic() >= max(state["retry_after"], state["unsupported_until"])


def _note_failure(endpoint: str, exc: BaseException, bot=None) -> None:
    """Record an unavailable method without globally disabling rich messages."""
    description = str(exc).lower()
    missing = isinstance(exc, InvalidToken) or any(
        hint in description for hint in _MISSING_METHOD_HINTS
    )
    if missing:
        if bot is not None:
            _state(bot, endpoint)["unsupported_until"] = (
                time.monotonic() + UNSUPPORTED_RETRY_SECONDS
            )
        logger.warning(
            "%s unavailable (%s) — using formatted HTML until the next capability check",
            endpoint,
            exc,
        )
    else:
        logger.warning("%s refused (%s) — falling back to HTML", endpoint, exc)


def reset_support_latch(bot=None) -> None:
    """Clear capability/backoff state for one bot, or all bots."""
    global _unsupported
    _unsupported = False
    for key in list(_capabilities):
        if bot is None or key[0] == id(bot):
            del _capabilities[key]


async def _native_post(post, endpoint, payload):
    """Keep optional native formatting from holding up the HTML response."""
    return await asyncio.wait_for(post(endpoint, payload), NATIVE_TIMEOUT_SECONDS)


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


def callback_button(text, callback_data, *, style="primary"):
    """Bot API 10.3 button embedded in a paragraph's RichText."""
    if (
        not isinstance(callback_data, str)
        or not 1 <= len(callback_data.encode("utf-8")) <= 64
    ):
        raise ValueError("Callback data must be 1–64 UTF-8 bytes.")
    if style not in ("primary", "success", "danger", "link"):
        raise ValueError("Unsupported rich button style.")
    return {
        "type": "button",
        "button": {"text": text, "callback_data": callback_data, "style": style},
    }


def spoiler(text):
    return {"type": "spoiler", "text": text}


def link(text, url: str):
    return {"type": "url", "text": text, "url": url}


def strikethrough(text):
    return {"type": "strikethrough", "text": text}


def mention(text, tg_id):
    """A user mention: ``text`` linked to the Telegram account ``tg_id``.

    Rides on the ``url`` node with a ``tg://user`` link, the same link the HTML
    renderers use for ``<a href="tg://user?id=…">``.
    """
    try:
        tg_id = int(tg_id)
    except (TypeError, ValueError):
        return text
    if tg_id <= 0:
        return text
    return link(text, f"tg://user?id={tg_id}")


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


def cell(
    text=None,
    *,
    header: bool = False,
    align: str = None,
    valign: str = "middle",
    colspan: int = None,
    rowspan: int = None,
):
    """One table cell. ``text=None`` leaves the cell invisible.

    ``align`` and ``valign`` are required by the API, so they are always sent.
    """
    if align is None:

        def plain(value):
            if isinstance(value, dict):
                return plain(value.get("text", ""))
            if isinstance(value, list):
                return "".join(plain(item) for item in value)
            return str("" if value is None else value)

        numeric = re.fullmatch(
            r"[₹$€£+−-]?\s*\d[\d,./:%\s]*(?:Cr|cr|OVR|coins|lakh|%|[kKmM])?",
            plain(text).strip(),
        )
        align = "right" if numeric and not header else "left"
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


def table(
    rows,
    *,
    bordered: bool = False,
    striped: bool = False,
    compact: bool = False,
    caption=None,
):
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


# Shared quotation, code and list blocks follow the current documented schema.
# Keep the optional caption argument for callers, but send it as API "credit".


def pre(text: str, language: str = None):
    """A preformatted code block, optionally tagged with a ``language``."""
    block = {"type": "pre", "text": text}
    if language:
        block["language"] = language
    return block


def pullquote(text, caption=None):
    """A large quotation — a headline result, set apart from the body."""
    block = {"type": "pullquote", "text": text}
    if caption is not None:
        block["credit"] = caption
    return block


def list_block(items, *, ordered: bool = False):
    """A bulleted (or, with ``ordered``, numbered) list of RichText items.

    Each item is its own list of blocks, so a plain RichText item is wrapped
    in a paragraph; a numbered list carries its number as each item's value.
    """
    wrapped = []
    for number, item in enumerate(items, start=1):
        entry = {"blocks": [paragraph(item)]}
        if ordered:
            entry.update(value=number, type="1")
        wrapped.append(entry)
    return {"type": "list", "items": wrapped}


def checklist(items):
    """A checkbox list from ``(checked, RichText)`` pairs.

    The Bot API's list items carry their own tick state, so a rule sheet — the
    Challenge League's XI conditions, say — is a real checklist rather than a
    line of ✅/⬜ characters that the HTML rendering has to fake.
    """
    wrapped = []
    for checked, item in items:
        entry = {"blocks": [paragraph(item)], "has_checkbox": True}
        if checked:
            entry["is_checked"] = True
        wrapped.append(entry)
    return {"type": "list", "items": wrapped}


def blockquote(blocks, *, expandable: bool = False):
    """A quoted passage. ``expandable`` collapses a long one behind a tap.

    The block twin of ``<blockquote>`` / ``<blockquote expandable>``, which is
    how every HTML renderer in the bot sets a passage apart.
    """
    if expandable:
        return {"type": "expandable_blockquote", "text": _block_text(blocks)}
    return {"type": "blockquote", "blocks": blocks}


def footnote(text, reference):
    """Body ``text`` carrying a ``reference`` note readers can open.

    Used for the asterisks a scoreboard grows — an adjusted points total, a
    not-out score — so the explanation rides on the number it belongs to
    instead of a line at the bottom nobody connects back.
    """
    name = "cmu-" + hashlib.sha256(repr((text, reference)).encode()).hexdigest()[:16]
    return [
        {"type": "reference_link", "text": text, "reference_name": name},
        {"type": "reference", "text": reference, "name": name},
    ]


def _block_text(blocks):
    content = []
    for block in blocks:
        if content:
            content.append("\n")
        if isinstance(block, dict):
            content.append(block.get("text", _block_text(block.get("blocks", []))))
        else:
            content.append(str(block))
    return content


def normalize_blocks(blocks):
    """Upgrade stored event snapshots as well as newly rendered cards."""

    def visit(node):
        if isinstance(node, list):
            return [visit(item) for item in node]
        if not isinstance(node, dict):
            return node
        row = {key: visit(value) for key, value in node.items()}
        kind = row.get("type")
        if kind == "pullquote" and "caption" in row:
            row.setdefault("credit", row.pop("caption"))
        if kind == "blockquote" and row.pop("is_expandable", False):
            row["type"] = "expandable_blockquote"
            row["text"] = _block_text(row.pop("blocks", []))
        if kind == "list":
            checkbox = row.pop("is_checkbox", False)
            for item in row.get("items", []):
                label = item.pop("label", None)
                if label and str(label).rstrip(".").isdigit():
                    item.update(value=int(str(label).rstrip(".")), type="1")
                if checkbox:
                    item["has_checkbox"] = True
                if item.get("is_checked") is False:
                    item.pop("is_checked")
        if kind == "footnote":
            return footnote(row.get("text", ""), row.get("reference", ""))
        return row

    return visit(blocks)


def button_rows(rows, *, align="center"):
    if align not in ("left", "center", "right"):
        raise ValueError("Invalid rich button alignment.")
    if any(len(row) > 8 for row in rows):
        raise ValueError("A rich button row supports at most eight buttons.")
    return [
        {"type": "buttons", "buttons": list(row), "align": align} for row in rows if row
    ]


def date_time(text, unix_time, date_time_format="DT"):
    return {
        "type": "date_time",
        "text": text,
        "unix_time": int(unix_time),
        "date_time_format": date_time_format,
    }


def _native_keyboard(blocks, markup):
    """Reuse the exact authorized callbacks inside Bot API 10.3+ button rows."""
    rows = getattr(markup, "inline_keyboard", None)
    if not rows:
        return blocks, markup
    native = []
    for row in rows:
        converted = []
        for button in row:
            # Keep unsupported actions and future keyboard properties intact.
            # Never turn a disabled or specialized button into an active one.
            if set(button.to_dict()) - {"text", "callback_data", "url"}:
                return blocks, markup
            if getattr(button, "callback_data", None) is not None:
                label = button.text
                style = (
                    "danger"
                    if label.startswith(("❌", "🚫", "Cancel", "Delete"))
                    else "success" if label.startswith(("✅", "Confirm")) else "primary"
                )
                converted.append(
                    callback_button(label, button.callback_data, style=style)["button"]
                )
            elif getattr(button, "url", None):
                converted.append({"text": button.text, "url": button.url})
            else:
                return blocks, markup
        native.extend(converted[i : i + 8] for i in range(0, len(converted), 8))
    return list(blocks) + button_rows(native), None


# ── Senders ──────────────────────────────────────────────────────────


async def _request_rich(
    bot,
    endpoint,
    payload,
    *,
    fallback_text=None,
    fallback_markup=None,
    post=None,
    retry_html=True,
):
    post = post or getattr(bot, "_post", None)
    if not post or not rich_text_enabled(bot, endpoint):
        return None
    try:
        result = await _native_post(post, endpoint, payload)
    except BadRequest as exc:
        if "not modified" in str(exc).lower():
            return True
        if any(hint in str(exc).lower() for hint in _MISSING_METHOD_HINTS):
            _note_failure(endpoint, exc, bot)
            return None
        if not retry_html or "blocks" not in payload["rich_message"]:
            _note_failure(endpoint, exc, bot)
            return None
        # A rejected block tree is not evidence that rich messages are missing.
        # Try the documented rich HTML representation before flattening a card.
        retry = dict(payload)
        try:
            from services.rich_message_html import render

            body = render(payload["rich_message"]["blocks"])
        except (KeyError, TypeError, ValueError):
            body = fallback_text
            if fallback_markup is not None:
                retry["reply_markup"] = fallback_markup
        if not body:
            _note_failure(endpoint, exc, bot)
            return None
        retry["rich_message"] = {"html": body}
        try:
            result = await _native_post(post, endpoint, retry)
        except Exception as retry_exc:
            if "not modified" in str(retry_exc).lower():
                return True
            _transport_failure(bot, endpoint, retry_exc)
            return None
    except Exception as exc:
        _transport_failure(bot, endpoint, exc)
        return None
    state = _state(bot, endpoint)
    state["retry_after"] = state["unsupported_until"] = 0.0
    if endpoint == "sendRichMessage" and isinstance(result, dict):
        from services.button_access import register_rich_message_owner

        register_rich_message_owner(result, payload)
    return result


def _transport_failure(bot, endpoint, exc):
    if isinstance(exc, (asyncio.TimeoutError, NetworkError)) and not isinstance(
        exc, BadRequest
    ):
        _state(bot, endpoint)["retry_after"] = (
            time.monotonic() + NATIVE_COOLDOWN_SECONDS
        )
    if isinstance(exc, RetryAfter):
        delay = exc.retry_after
        if hasattr(delay, "total_seconds"):
            delay = delay.total_seconds()
        _state(bot, endpoint)["retry_after"] = time.monotonic() + float(delay)
    _note_failure(endpoint, exc, bot)


def _rich_content(blocks, reply_markup, fallback_text, *, native_buttons=False):
    native_blocks, native_markup = normalize_blocks(blocks or []), reply_markup
    if native_buttons:
        native_blocks, native_markup = _native_keyboard(native_blocks, reply_markup)
    if blocks:
        return {"blocks": native_blocks}, native_markup
    from services.rich_layout import aligned_blocks

    aligned = aligned_blocks(str(fallback_text or ""))
    if aligned:
        return {"blocks": aligned + native_blocks}, native_markup
    from services.rich_message_html import render

    return {"html": str(fallback_text or "") + render(native_blocks)}, native_markup


async def _post_rich(
    bot,
    chat_id,
    blocks,
    reply_markup,
    reply_to_message_id,
    *,
    fallback_text=None,
    native_buttons=False,
    **options,
):
    if not (blocks or fallback_text) or not rich_text_enabled(bot):
        return None
    try:
        content, native_markup = _rich_content(
            blocks, reply_markup, fallback_text, native_buttons=native_buttons
        )
    except Exception:
        logger.warning(
            "Unable to prepare native rich message; preserving formatted HTML",
            exc_info=True,
        )
        return None
    payload = {"chat_id": chat_id, "rich_message": content, **options}
    if native_markup is not None:
        payload["reply_markup"] = native_markup
    if reply_to_message_id is not None and "reply_parameters" not in payload:
        payload["reply_parameters"] = {"message_id": reply_to_message_id}
    result = await _request_rich(
        bot,
        "sendRichMessage",
        payload,
        fallback_text=fallback_text,
        fallback_markup=reply_markup,
    )
    if result is None:
        return None
    try:
        return Message.de_json(result, bot)
    except Exception:
        logger.warning("Unable to decode rich-message response", exc_info=True)
        return None


async def send_rich_message(
    bot,
    chat_id,
    blocks,
    fallback_text,
    *,
    reply_markup=None,
    reply_to_message_id=None,
    native_buttons=False,
    **kwargs,
):
    """Send ``blocks`` as a rich message, or ``fallback_text`` as HTML.

    Returns the sent :class:`telegram.Message`. Supported delivery options
    (topics, reply context, protection) also go to the native endpoint.
    Private ephemeral replies never fall back to a public message.
    """
    native_options = {
        key: value
        for key, value in kwargs.items()
        if key
        in (
            "message_thread_id",
            "direct_messages_topic_id",
            "ephemeral_message_parameters",
            "business_connection_id",
            "disable_notification",
            "protect_content",
            "allow_paid_broadcast",
            "message_effect_id",
            "reply_parameters",
            "suggested_post_parameters",
        )
    }
    sent = await _post_rich(
        bot,
        chat_id,
        blocks,
        reply_markup,
        reply_to_message_id,
        fallback_text=fallback_text,
        native_buttons=native_buttons,
        **native_options,
    )
    if sent is not None:
        return sent

    if kwargs.get("ephemeral_message_parameters"):
        raise TelegramError(
            "Private rich reply unavailable; no public message was sent."
        )
    parts = html_parts(fallback_text)
    sent = None
    for index, part in enumerate(parts):
        options = dict(kwargs)
        if index:
            options.pop("reply_parameters", None)
        options.update(
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=reply_markup if index == len(parts) - 1 else None,
            reply_to_message_id=reply_to_message_id if index == 0 else None,
        )
        with legacy_delivery():
            sent = await bot.send_message(chat_id, part, **options)
    return sent


async def edit_rich_message(
    bot, chat_id, message_id, blocks, *, reply_markup=None, fallback_text=None
) -> bool:
    if not (blocks or fallback_text) or not rich_text_enabled(bot, "editMessageText"):
        return False
    try:
        content, native_markup = _rich_content(blocks, reply_markup, fallback_text)
    except Exception:
        logger.warning(
            "Unable to prepare native rich edit; preserving formatted HTML",
            exc_info=True,
        )
        return False
    payload = {"chat_id": chat_id, "message_id": message_id, "rich_message": content}
    if native_markup is not None:
        payload["reply_markup"] = native_markup
    result = await _request_rich(
        bot,
        "editMessageText",
        payload,
        fallback_text=fallback_text,
        fallback_markup=reply_markup,
        retry_html=not any(
            block.get("type") in ("photo", "animation", "video", "document", "audio")
            for block in (blocks or [])
        ),
    )
    return result is not None


async def answer_callback_rich(query, blocks, fallback_text):
    """Bot API 10.4 private rich response, visible only to the button clicker."""
    getter = getattr(query, "get_bot", None)
    query_id = getattr(query, "id", None)
    if not getter or not query_id:
        return False
    bot = getter()
    message = getattr(query, "message", None)
    payload = {
        "rich_message": {"blocks": normalize_blocks(blocks)},
        "ephemeral_message_parameters": {"callback_query_id": query_id},
    }
    if message is not None:
        payload["chat_id"] = message.chat_id
    result = await _request_rich(
        bot, "sendRichMessage", payload, fallback_text=fallback_text
    )
    return result is not None


# ── Conveniences for handlers ────────────────────────────────────────
# A command handler holds a ``Message``; a button handler holds a
# ``CallbackQuery``. Neither holds the (bot, chat_id) pair the senders above
# take, and both already know how to render their own HTML — so these two wrap
# the plumbing that would otherwise be copied into every surface.


def html_parts(html_text, limit=CHUNK_LIMIT):
    """``html_text`` cut into sends Telegram will accept.

    Cuts at blank lines first — where every renderer in this bot puts its
    section breaks, and never inside a ``<blockquote>`` — and only falls back to
    single line breaks for one section longer than a whole message.
    """
    from utils.message_chunks import chunk_blocks

    if len(html_text) <= limit:
        return [html_text]
    parts = []
    for chunk in chunk_blocks(html_text.split("\n\n"), limit=limit):
        chunk = chunk.strip("\n")
        if len(chunk) <= limit:
            parts.append(chunk)
        else:
            parts.extend(
                p.strip("\n") for p in chunk_blocks(chunk.split("\n"), limit=limit)
            )
    return [p for p in parts if p]


async def reply_rich(message, blocks, fallback_text, *, reply_markup=None):
    """Answer ``message`` with ``blocks``, or with its HTML twin.

    Returns the last message sent, or None when there was nothing to answer.
    The fallback goes out through ``message.reply_text`` — the same call the
    surfaces here made before they grew a block rendering, so the threading and
    quoting behaviour is unchanged — and an HTML body too long for one send goes
    out in parts, with the buttons on the last.
    """
    if message is None:
        return None
    get_bot = getattr(message, "get_bot", None)
    if get_bot is not None:
        options = {}
        if getattr(message, "message_thread_id", None) is not None:
            options["message_thread_id"] = message.message_thread_id
        if getattr(message, "business_connection_id", None):
            options["business_connection_id"] = message.business_connection_id
        sent = await _post_rich(
            get_bot(),
            message.chat_id,
            blocks,
            reply_markup,
            None,
            fallback_text=fallback_text,
            **options,
        )
        if sent is not None:
            return sent
    parts = html_parts(fallback_text)
    sent = None
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        with legacy_delivery():
            sent = await message.reply_text(
                part,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=reply_markup if last else None,
            )
    return sent


async def edit_rich(query, blocks, fallback_text, *, reply_markup=None):
    """Redraw a callback card as ``blocks``, falling back to its HTML twin.

    Returns True when something was drawn. An edit that Telegram refuses as
    "not modified" counts as drawn — the card already shows what was asked for.
    """
    if query is None:
        return False
    message = getattr(query, "message", None)
    get_bot = getattr(query, "get_bot", None)
    if message is not None and get_bot is not None:
        if await edit_rich_message(
            get_bot(),
            message.chat_id,
            message.message_id,
            blocks,
            reply_markup=reply_markup,
            fallback_text=fallback_text,
        ):
            return True
    # Old servers cannot edit long rich cards. Reply with every fallback part
    # when there is a chat message, preserving all rows and the final keyboard.
    parts = html_parts(fallback_text)
    if len(parts) > 1:
        if message is not None and hasattr(message, "reply_text"):
            await reply_rich(message, blocks, fallback_text, reply_markup=reply_markup)
            return True
    text = parts[0]
    try:
        with legacy_delivery():
            await query.edit_message_text(
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )
        return True
    except Exception as exc:
        if "not modified" in str(exc).lower():
            return True
        logger.debug("rich edit fallback failed: %s", exc)
        return False

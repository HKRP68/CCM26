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

Two conveniences sit on top of the raw senders, because most surfaces in this
bot already hold a ``Message`` or a ``CallbackQuery`` rather than a bot and a
chat id: :func:`reply_rich` answers a command, :func:`edit_rich` redraws a card
behind a button. Both take the surface's existing HTML as the fallback, and
both split an over-long fallback rather than letting Telegram refuse it.
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

# Telegram refuses a text message over 4,096 characters outright. The HTML
# fallbacks are cut under that rather than lost.
CHUNK_LIMIT = 3800


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


# The three blocks below are named in the Bot API 10.1 block list (see
# docs/rich-text-messages.md) but no surface had used them before the Franchise
# Auction. Their field names follow the conventions of the blocks above; a
# payload Telegram refuses falls back to the HTML rendering like any other, and
# does not latch rich sends off.

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
        block["caption"] = caption
    return block


def list_block(items, *, ordered: bool = False):
    """A bulleted (or, with ``ordered``, numbered) list of RichText items.

    Each item is its own list of blocks, so a plain RichText item is wrapped
    in a paragraph; a numbered list carries its number as each item's label.
    """
    wrapped = []
    for number, item in enumerate(items, start=1):
        entry = {"blocks": [paragraph(item)]}
        if ordered:
            entry["label"] = f"{number}."
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
        wrapped.append({"blocks": [paragraph(item)],
                        "is_checked": bool(checked)})
    return {"type": "list", "items": wrapped, "is_checkbox": True}


def blockquote(blocks, *, expandable: bool = False):
    """A quoted passage. ``expandable`` collapses a long one behind a tap.

    The block twin of ``<blockquote>`` / ``<blockquote expandable>``, which is
    how every HTML renderer in the bot sets a passage apart.
    """
    block = {"type": "blockquote", "blocks": blocks}
    if expandable:
        block["is_expandable"] = True
    return block


def footnote(text, reference):
    """Body ``text`` carrying a ``reference`` note readers can open.

    Used for the asterisks a scoreboard grows — an adjusted points total, a
    not-out score — so the explanation rides on the number it belongs to
    instead of a line at the bottom nobody connects back.
    """
    return {"type": "footnote", "text": text, "reference": reference}


# ── Senders ──────────────────────────────────────────────────────────

async def _post_rich(bot, chat_id, blocks, reply_markup, reply_to_message_id):
    """One ``sendRichMessage`` attempt: the Message, or None when refused.

    Shared by every sender here so the decision of *whether* to try rich, and
    the accounting when Telegram says no, live in exactly one place.
    """
    post = getattr(bot, "_post", None)
    if not (rich_text_enabled() and blocks and post):
        return None
    payload = {"chat_id": chat_id, "rich_message": {"blocks": blocks}}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    if reply_to_message_id is not None:
        payload["reply_parameters"] = {"message_id": reply_to_message_id}
    try:
        result = await post("sendRichMessage", payload)
        return Message.de_json(result, bot)
    except TelegramError as exc:
        _note_failure("sendRichMessage", exc)
        return None
    except Exception:
        # Not a refusal — a bug on this side of the wire (a block builder that
        # produced something unserialisable, say). It must still fall through to
        # the HTML, because nothing in this module is allowed to lose the
        # message; it is logged with its traceback rather than counted as a
        # server refusal, so it keeps showing up until somebody fixes it.
        logger.warning("sendRichMessage failed locally — falling back to HTML",
                       exc_info=True)
        return None


async def send_rich_message(bot, chat_id, blocks, fallback_text, *,
                            reply_markup=None, reply_to_message_id=None,
                            **kwargs):
    """Send ``blocks`` as a rich message, or ``fallback_text`` as HTML.

    Returns the sent :class:`telegram.Message`. ``kwargs`` are passed to the
    HTML fallback only, since the two endpoints do not take the same options.
    """
    sent = await _post_rich(bot, chat_id, blocks, reply_markup,
                            reply_to_message_id)
    if sent is not None:
        return sent

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
    post = getattr(bot, "_post", None)
    if not (rich_text_enabled() and blocks and post):
        return False
    payload = {"chat_id": chat_id, "message_id": message_id,
               "rich_message": {"blocks": blocks}}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        await post("editMessageText", payload)
        return True
    except Exception as exc:
        if "not modified" in str(exc).lower():
            # Two ticks can render the same card. Nothing changed, so nothing
            # failed — reporting this as a refusal would send the HTML twin
            # over a message that already shows the right thing.
            return True
        if isinstance(exc, TelegramError):
            _note_failure("editMessageText", exc)
        else:
            logger.warning("editMessageText failed locally — falling back to "
                           "HTML", exc_info=True)
        return False


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
            parts.extend(p.strip("\n")
                         for p in chunk_blocks(chunk.split("\n"), limit=limit))
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
        sent = await _post_rich(get_bot(), message.chat_id, blocks,
                                reply_markup, None)
        if sent is not None:
            return sent
    parts = html_parts(fallback_text)
    sent = None
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        sent = await message.reply_text(
            part, parse_mode="HTML", disable_web_page_preview=True,
            reply_markup=reply_markup if last else None)
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
        if await edit_rich_message(get_bot(), message.chat_id,
                                   message.message_id, blocks,
                                   reply_markup=reply_markup):
            return True
    # An edit is one message by definition, so an over-long fallback cannot go
    # out in parts the way a reply can. Showing the first part and saying it was
    # cut beats a refused edit that leaves the old card on screen.
    parts = html_parts(fallback_text)
    text = parts[0]
    if len(parts) > 1:
        text += "\n\n<i>… trimmed to fit — run the command again for the "\
                "full card.</i>"
    try:
        await query.edit_message_text(
            text, parse_mode="HTML",
            disable_web_page_preview=True, reply_markup=reply_markup)
        return True
    except Exception as exc:
        if "not modified" in str(exc).lower():
            return True
        logger.debug("rich edit fallback failed: %s", exc)
        return False

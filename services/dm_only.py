"""Commands that answer in DM instead of flooding the group.

A stat lookup is a private question with a long answer. Ten managers running
/stats, /gstats and /traits in the same group buries every match, lobby and
trade under walls of text nobody else asked for.

These commands therefore run one-to-one with the bot. Used in a group they
don't refuse — they post one short notice with a deep-link button that opens
the DM *and runs the same command with the same arguments there*, so the
redirect costs the player one tap rather than a retype.

Two things keep the notice itself from becoming the new spam:

* it is throttled per (chat, user, command), so holding down /stats posts one
  notice, not twelve;
* it deletes itself after a minute.

The deep link carries the arguments as base64url in the ``/start`` payload —
see ``build_start_payload`` / ``parse_start_payload``, which ``bot.start_handler``
uses to replay the command in the DM.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import time
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

logger = logging.getLogger(__name__)

# Telegram's cap on a /start payload, and its charset: A-Za-z0-9_- only.
START_PAYLOAD_LIMIT = 64
PAYLOAD_PREFIX = "cmd_"
_ARGS_SEP = "-"

GROUP_CHAT_TYPES = ("group", "supergroup")

# How long a redirect notice lives in the group, and how long the same person
# has to wait before another one is posted for the same command.
NOTICE_TTL_SECONDS = 60
THROTTLE_SECONDS = 60

# The commands that have moved. Keyed by the canonical command name (the one
# published in the slash menu); the value is what the notice calls it.
DM_ONLY_COMMANDS = {
    "stats": "Player stats",
    "gstats": "Global player stats",
    "statscl": "Challenge League stats",
    "setbo": "Batting order",
    "recentmatches": "Recent matches",
    "traits": "Traits",
    "traitshop": "Trait shop",
    "traitapply": "Apply a trait",
    "traitupgrade": "Upgrade a trait",
    "traitreplace": "Replace a trait",
    "removetrait": "Remove a trait",
    "selltrait": "Sell a trait",
}

# (chat_id, user_id, command) -> monotonic time the last notice went out.
_last_notice: dict[tuple[int, int, str], float] = {}


def _bot_username() -> str:
    return (os.getenv("BOT_USERNAME", "") or "").strip().lstrip("@")


def _encode_args(args) -> str:
    raw = " ".join(str(a) for a in (args or [])).strip()
    if not raw:
        return ""
    packed = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")
    return packed.rstrip("=")


def _decode_args(blob: str) -> list[str]:
    if not blob:
        return []
    padded = blob + "=" * (-len(blob) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return []
    return raw.split()


def build_start_payload(command: str, args=None) -> str:
    """``cmd_gstats-VmlyYXQgS29obGk`` — the command, plus its arguments.

    Falls back to the bare ``cmd_<command>`` when the arguments would push the
    payload past Telegram's 64-character ceiling: opening the command with no
    argument is a far better outcome than a deep link Telegram rejects.
    """
    base = f"{PAYLOAD_PREFIX}{command}"
    blob = _encode_args(args)
    if not blob:
        return base
    full = f"{base}{_ARGS_SEP}{blob}"
    return full if len(full) <= START_PAYLOAD_LIMIT else base


def parse_start_payload(payload: str):
    """(command, args) from a ``cmd_…`` payload, or (None, []) if it isn't one."""
    if not payload or not payload.startswith(PAYLOAD_PREFIX):
        return None, []
    body = payload[len(PAYLOAD_PREFIX):]
    command, _, blob = body.partition(_ARGS_SEP)
    if command not in DM_ONLY_COMMANDS:
        return None, []
    return command, _decode_args(blob)


def dm_link(command: str, args=None) -> str | None:
    """The ``https://t.me/bot?start=…`` link that replays this command in DM."""
    username = _bot_username()
    if not username:
        return None
    return f"https://t.me/{username}?start={build_start_payload(command, args)}"


def _throttled(chat_id: int, user_id: int, command: str) -> bool:
    """True when a notice for this trio is already on screen.

    Also prunes expired entries, so a long-running process doesn't accumulate
    one dictionary key per (chat, user, command) it has ever seen.
    """
    now = time.monotonic()
    for key, stamp in list(_last_notice.items()):
        if now - stamp >= THROTTLE_SECONDS:
            _last_notice.pop(key, None)
    key = (chat_id, user_id, command)
    if key in _last_notice:
        return True
    _last_notice[key] = now
    return False


def _notice_text(command: str, args) -> str:
    label = DM_ONLY_COMMANDS.get(command, f"/{command}")
    typed = " ".join(str(a) for a in (args or [])).strip()
    shown = f"/{command} {typed}".strip()
    return (
        f"📩 <b>{escape(label)} now answers in DM</b>\n"
        f"<code>/{escape(command)}</code> was filling groups with stat walls, "
        f"so it runs one-to-one with me instead.\n\n"
        f"Tap below — I'll run <code>{escape(shown)}</code> there for you.")


async def _delete_later(context, chat_id: int, message_id: int, delay: int):
    try:
        await asyncio.sleep(delay)
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except asyncio.CancelledError:
        raise
    except (TelegramError, Exception):
        # No delete rights, or the message is already gone. Either way the
        # notice staying put is not worth an error.
        logger.debug("dm-only notice cleanup skipped", exc_info=True)


async def require_dm(update, context, command: str) -> bool:
    """Gate a DM-only command. True to run it, False once redirected.

    Anything that isn't a group — a DM, a channel post, an update with no chat
    at all — runs normally, so this can never lock a player out of a command by
    mistake.
    """
    chat = getattr(update, "effective_chat", None)
    chat_type = getattr(chat, "type", None) if chat else None
    if chat_type not in GROUP_CHAT_TYPES:
        return True

    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    tg_user = getattr(update, "effective_user", None)
    if message is None or tg_user is None:
        return True

    args = list(getattr(context, "args", None) or [])
    if _throttled(chat.id, tg_user.id, command):
        return False

    link = dm_link(command, args)
    if link:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📩 Open in DM", url=link)]])
        text = _notice_text(command, args)
    else:
        # No BOT_USERNAME configured — there is no link to offer, so say where
        # to go rather than showing a button that goes nowhere.
        keyboard = None
        text = (f"📩 <code>/{escape(command)}</code> now answers in my DM — "
                f"start a chat with me and run it there.")

    try:
        sent = await message.reply_text(text, parse_mode="HTML",
                                        reply_markup=keyboard,
                                        disable_web_page_preview=True)
    except Exception:
        logger.exception("dm-only notice failed for /%s", command)
        return False

    try:
        asyncio.create_task(_delete_later(context, sent.chat_id, sent.message_id,
                                          NOTICE_TTL_SECONDS))
    except RuntimeError:
        pass  # no running loop (tests) — the notice simply stays put
    return False


def dm_only(command: str, handler):
    """Wrap a command handler so groups get the redirect and DMs get the answer.

    Applied at registration in bot.py, which keeps the rule in one readable
    list instead of a guard pasted into a dozen handler modules.
    """
    async def _guarded(update, context):
        if not await require_dm(update, context, command):
            return None
        return await handler(update, context)

    _guarded.__name__ = getattr(handler, "__name__", command)
    _guarded.__doc__ = getattr(handler, "__doc__", None)
    return _guarded

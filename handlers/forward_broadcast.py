"""Admin-only reply-forward commands for Telegram broadcasts.

Usage in the bot's private chat:
    1. Send the message/media to the bot.
    2. Reply to that message with /frwd_grp or /frwd_prvt.

/frwd_grp forwards the replied message to active groups/supergroups where the
bot is present. /frwd_prvt forwards it to active private chats that have talked
to the bot.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

from telegram import Update
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import ContextTypes

from database import get_session
from models import BotChat
# Single source of truth for "who is a bot admin" — shared with the rest of the
# codebase so authorization behavior can't drift between modules.
from services.admin_ids import (
    ADMIN_ID_ENV_VARS,  # re-exported for callers/tests that reference it here
    configured_admin_ids,
    is_admin as is_forward_admin,
)

logger = logging.getLogger(__name__)

GROUP_CHAT_TYPES = ("group", "supergroup")
PRIVATE_CHAT_TYPES = ("private",)
DEFAULT_DELAY_SECONDS = 0.05

# ── Album (media-group) buffering ────────────────────────────────────
# Telegram delivers each photo of an album as a separate message that merely
# shares a ``media_group_id``; there is no API to fetch "all messages in a
# group". Replying to one of them therefore only knows that single message_id,
# which is why /frwd_* used to forward just the first image. We remember the
# message_ids of albums admins send to the bot's DM so the forward commands can
# copy the whole group. Entries expire so the dict can't grow unbounded.
ALBUM_CACHE_TTL_SECONDS = 1800
_album_cache: dict[tuple[int, str], dict] = {}


def remember_album_message(chat_id: int, media_group_id, message_id: int) -> None:
    """Record one message of an album keyed by (chat_id, media_group_id)."""
    if not media_group_id:
        return
    now = time.time()
    # Prune stale groups opportunistically.
    for key in [k for k, v in _album_cache.items()
                if now - v["ts"] > ALBUM_CACHE_TTL_SECONDS]:
        _album_cache.pop(key, None)
    key = (int(chat_id), str(media_group_id))
    entry = _album_cache.get(key)
    if entry is None:
        entry = {"ids": [], "ts": now}
        _album_cache[key] = entry
    mid = int(message_id)
    if mid not in entry["ids"]:
        entry["ids"].append(mid)
    entry["ts"] = now


def get_album_message_ids(chat_id: int, media_group_id) -> list[int]:
    """Return the buffered, sorted message_ids for an album (empty if unknown)."""
    if not media_group_id:
        return []
    entry = _album_cache.get((int(chat_id), str(media_group_id)))
    return sorted(entry["ids"]) if entry else []


async def track_album_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Message handler: buffer album parts admins send to the bot's DM so the
    forward commands can later copy every image, not just the replied one."""
    message = update.effective_message
    user = update.effective_user
    if not message or not message.media_group_id or not user:
        return
    if not is_forward_admin(user.id):
        return
    remember_album_message(message.chat_id, message.media_group_id, message.message_id)


@dataclass(frozen=True)
class ForwardResult:
    """Counters from a forward broadcast run."""

    total: int
    sent: int
    failed: int


def _target_chat_ids(chat_types: tuple[str, ...]) -> list[int]:
    """Load active target chat IDs for a broadcast destination."""
    session = get_session()
    try:
        rows = (
            session.query(BotChat)
            .filter(BotChat.is_active == True, BotChat.chat_type.in_(chat_types))
            .order_by(BotChat.last_seen_at.desc())
            .all()
        )
        return [int(row.chat_id) for row in rows]
    finally:
        session.close()


def _forward_delay_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("FORWARD_BROADCAST_DELAY_SECONDS", DEFAULT_DELAY_SECONDS)))
    except ValueError:
        return DEFAULT_DELAY_SECONDS


async def _mark_chat_inactive(chat_id: int) -> None:
    """Deactivate chats that Telegram says the bot can no longer message."""
    session = get_session()
    try:
        row = session.query(BotChat).filter(BotChat.chat_id == chat_id).first()
        if row:
            row.is_active = False
            session.commit()
    except Exception:
        session.rollback()
        logger.exception("Failed to mark chat %s inactive after forward failure", chat_id)
    finally:
        session.close()


async def forward_replied_message(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_ids: list[int],
    from_chat_id: int,
    message_id: int,
    message_ids: list[int] | None = None,
) -> ForwardResult:
    """Copy one source message (or a whole album) to each target chat.

    Uses ``copy_message`` / ``copy_messages`` rather than ``forward_message`` so
    the broadcast carries **no** "Forwarded from …" sender attribution. When
    ``message_ids`` holds more than one id (an album / media group), all of them
    are copied as a group so multi-image posts keep every image and the caption.
    """
    sent = 0
    failed = 0
    delay = _forward_delay_seconds()
    album_ids = [int(m) for m in message_ids] if message_ids else []

    for chat_id in chat_ids:
        try:
            if len(album_ids) > 1:
                copied = await context.bot.copy_messages(
                    chat_id=chat_id,
                    from_chat_id=from_chat_id,
                    message_ids=album_ids,
                )
                # copy_messages returns a sequence of MessageId; pin the first
                # item of the album so the whole post is highlighted.
                pinned_id = copied[0].message_id if copied else None
            else:
                copied = await context.bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=from_chat_id,
                    message_id=message_id,
                )
                pinned_id = copied.message_id if copied else None
            sent += 1
            # Pin the freshly-forwarded message in the target chat. Best-effort:
            # a missing pin permission must never fail the broadcast itself.
            if pinned_id is not None:
                try:
                    await context.bot.pin_chat_message(
                        chat_id=chat_id,
                        message_id=pinned_id,
                        disable_notification=True,
                    )
                except TelegramError as exc:
                    logger.info("Could not pin forwarded message in %s: %s",
                                chat_id, exc)
        except Forbidden as exc:
            failed += 1
            logger.warning("Forward target %s is unavailable: %s", chat_id, exc)
            await _mark_chat_inactive(chat_id)
        except BadRequest as exc:
            failed += 1
            logger.warning("Forward to %s rejected: %s", chat_id, exc)
        except TelegramError as exc:
            failed += 1
            logger.warning("Forward to %s failed: %s", chat_id, exc)

        if delay:
            await asyncio.sleep(delay)

    return ForwardResult(total=len(chat_ids), sent=sent, failed=failed)


async def _handle_forward_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    command_name: str,
    target_label: str,
    chat_types: tuple[str, ...],
) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not message or not user or not chat:
        return

    if not is_forward_admin(user.id):
        await message.reply_text("⛔ Only bot admins can use this command.")
        return

    if chat.type != "private":
        await message.reply_text(
            f"⚠️ Use /{command_name} in the bot's DM by replying to the message you want to forward."
        )
        return

    source = message.reply_to_message
    if not source:
        await message.reply_text(
            f"📌 Send a message to this DM, then reply to it with /{command_name}."
        )
        return

    chat_ids = _target_chat_ids(chat_types)
    if not chat_ids:
        await message.reply_text(f"ℹ️ No active {target_label} chats found.")
        return

    # If the replied message is part of an album, copy every buffered image of
    # that media group — not just the one the admin happened to reply to.
    message_ids = None
    if getattr(source, "media_group_id", None):
        message_ids = get_album_message_ids(source.chat_id, source.media_group_id) \
            or [source.message_id]

    album_note = f" (album of {len(message_ids)} items)" if message_ids and len(message_ids) > 1 else ""
    status = await message.reply_text(
        f"🚀 Forwarding replied message{album_note} to {len(chat_ids)} {target_label} chat(s)…"
    )
    result = await forward_replied_message(
        context,
        chat_ids=chat_ids,
        from_chat_id=source.chat_id,
        message_id=source.message_id,
        message_ids=message_ids,
    )

    summary = (
        f"✅ Forward complete for {target_label} chats.\n"
        f"Sent: {result.sent}/{result.total}\n"
        f"Failed: {result.failed}"
    )
    try:
        await status.edit_text(summary)
    except TelegramError:
        await message.reply_text(summary)


async def frwd_grp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Forward the replied DM message to all active group/supergroup chats."""
    await _handle_forward_command(
        update,
        context,
        command_name="frwd_grp",
        target_label="group",
        chat_types=GROUP_CHAT_TYPES,
    )


async def frwd_prvt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Forward the replied DM message to all active private chats."""
    await _handle_forward_command(
        update,
        context,
        command_name="frwd_prvt",
        target_label="private",
        chat_types=PRIVATE_CHAT_TYPES,
    )

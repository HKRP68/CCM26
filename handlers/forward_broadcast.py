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
from collections.abc import Iterable
from dataclasses import dataclass

from telegram import Update
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import ContextTypes

from database import get_session
from models import BotChat
from services.config_service import get_config

logger = logging.getLogger(__name__)

ADMIN_ID_ENV_VARS = (
    "BOT_ADMIN_IDS",
    "ADMIN_IDS",
    "ADMIN_USER_IDS",
    "SUDO_USERS",
    "OWNER_IDS",
    "ADMIN_CHAT_ID",
)

GROUP_CHAT_TYPES = ("group", "supergroup")
PRIVATE_CHAT_TYPES = ("private",)
DEFAULT_DELAY_SECONDS = 0.05


@dataclass(frozen=True)
class ForwardResult:
    """Counters from a forward broadcast run."""

    total: int
    sent: int
    failed: int


def _parse_id_list(raw_values: Iterable[str | None]) -> set[int]:
    """Parse comma/space separated positive Telegram user IDs."""
    ids: set[int] = set()
    for raw in raw_values:
        if not raw:
            continue
        normalized = str(raw).replace(";", ",").replace("\n", ",")
        for part in normalized.replace(" ", ",").split(","):
            token = part.strip()
            if not token:
                continue
            try:
                value = int(token)
            except ValueError:
                continue
            # Negative IDs are groups/channels (for example ADMIN_CHAT_ID can be
            # a staff group). They cannot identify an individual command sender.
            if value > 0:
                ids.add(value)
    return ids


def configured_admin_ids() -> set[int]:
    """Return global bot-admin Telegram user IDs from env and admin config.

    The environment variables are the primary source. As a convenience for
    deployments that already maintain Telegram admin IDs in the maintenance
    settings, ``maintenance_bypass_ids`` is also accepted.
    """
    raw_values = [os.getenv(name) for name in ADMIN_ID_ENV_VARS]
    try:
        raw_values.append((get_config() or {}).get("maintenance_bypass_ids"))
    except Exception:
        logger.exception("Failed to load maintenance bypass admin IDs")
    return _parse_id_list(raw_values)


def is_forward_admin(user_id: int | None, admin_ids: set[int] | None = None) -> bool:
    """Check whether a Telegram user may run forward-broadcast commands."""
    if user_id is None:
        return False
    allowed = admin_ids if admin_ids is not None else configured_admin_ids()
    return int(user_id) in allowed


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
) -> ForwardResult:
    """Forward one source message to each target chat."""
    sent = 0
    failed = 0
    delay = _forward_delay_seconds()

    for chat_id in chat_ids:
        try:
            await context.bot.forward_message(
                chat_id=chat_id,
                from_chat_id=from_chat_id,
                message_id=message_id,
            )
            sent += 1
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

    status = await message.reply_text(
        f"🚀 Forwarding replied message to {len(chat_ids)} {target_label} chat(s)…"
    )
    result = await forward_replied_message(
        context,
        chat_ids=chat_ids,
        from_chat_id=source.chat_id,
        message_id=source.message_id,
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

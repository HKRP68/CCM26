"""Track Telegram chats the bot is in, so admin broadcasts can target them.

Two entry points:
  1. `record_chat(update)` — called from middleware on EVERY incoming update.
     Upserts the chat row (cheap, debounced via last_seen_at).
  2. `handle_chat_member_update(update, context)` — registered as a
     ChatMemberHandler to detect when the bot is added/kicked.
"""

import logging
from datetime import datetime
from telegram import Update, Chat, ChatMember, ChatMemberUpdated
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


def record_chat(update: Update):
    """Backfill — every update upserts the chat into bot_chats.

    Cheap, non-blocking. Failures are swallowed silently so we never
    drop user messages over telemetry.
    """
    try:
        chat = update.effective_chat
        if not chat:
            return
        from database import get_session
        from models import BotChat
        s = get_session()
        try:
            row = (s.query(BotChat)
                    .filter(BotChat.chat_id == chat.id)
                    .first())
            now = datetime.utcnow()
            if not row:
                row = BotChat(
                    chat_id=chat.id,
                    chat_type=chat.type or "group",
                    title=chat.title,
                    username=chat.username,
                    is_active=True,
                    joined_at=now,
                    last_seen_at=now,
                )
                s.add(row)
            else:
                # Update metadata + last_seen
                row.title = chat.title or row.title
                row.username = chat.username or row.username
                row.chat_type = chat.type or row.chat_type
                row.last_seen_at = now
                if not row.is_active:
                    # Bot was kicked previously but is back
                    row.is_active = True
                    row.left_at = None
                    row.joined_at = now
            s.commit()
        except Exception:
            s.rollback()
        finally:
            s.close()
    except Exception:
        logger.debug("record_chat failed (non-fatal)", exc_info=True)


async def handle_chat_member_update(update: Update,
                                      ctx: ContextTypes.DEFAULT_TYPE):
    """Handle changes to bot's status in a chat: added → mark active,
    kicked/banned/left → mark inactive."""
    try:
        cmu: ChatMemberUpdated = update.my_chat_member
        if not cmu:
            return
        old_status = cmu.old_chat_member.status if cmu.old_chat_member else None
        new_status = cmu.new_chat_member.status
        chat = cmu.chat

        # We only care about the BOT's status change here
        from database import get_session
        from models import BotChat
        s = get_session()
        try:
            row = (s.query(BotChat)
                    .filter(BotChat.chat_id == chat.id)
                    .first())
            now = datetime.utcnow()

            if new_status in (ChatMember.LEFT, ChatMember.BANNED):
                # Bot kicked or banned
                if row:
                    row.is_active = False
                    row.left_at = now
                logger.info(f"Bot removed from chat {chat.id} ({chat.title})")
            else:
                # Bot added or status upgraded (member→admin etc.)
                if not row:
                    row = BotChat(
                        chat_id=chat.id,
                        chat_type=chat.type or "group",
                        title=chat.title,
                        username=chat.username,
                        is_active=True,
                        joined_at=now,
                        last_seen_at=now,
                    )
                    s.add(row)
                else:
                    row.is_active = True
                    row.title = chat.title or row.title
                    row.username = chat.username or row.username
                    row.chat_type = chat.type or row.chat_type
                    row.last_seen_at = now
                    if row.left_at:
                        row.joined_at = now  # readded
                        row.left_at = None
                if old_status in (ChatMember.LEFT, None):
                    logger.info(f"Bot ADDED to chat {chat.id} ({chat.title})")

            s.commit()
        except Exception:
            s.rollback()
            logger.exception("chat_member update failed")
        finally:
            s.close()
    except Exception:
        logger.exception("handle_chat_member_update outer failed")

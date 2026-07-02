"""Track Telegram chats the bot is in, so admin broadcasts can target them.

Two entry points:
  1. `record_chat(update)` — called from middleware on EVERY incoming update.
     Throttled in-memory: at most one DB write per chat per RECORD_THROTTLE_SECONDS.
     This keeps Neon compute hours low — the bot can field thousands of
     messages without touching `bot_chats` more than once per chat per cycle.
  2. `handle_chat_member_update(update, context)` — registered as a
     ChatMemberHandler to detect when the bot is added/kicked. Always writes.
"""

import logging
from datetime import datetime, timedelta
from telegram import Update, Chat, ChatMember, ChatMemberUpdated
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# Throttle: skip DB write if we updated this chat's last_seen_at recently
RECORD_THROTTLE_SECONDS = 600  # 10 minutes

# In-memory cache of {chat_id: last_record_at_datetime}
_LAST_SEEN_MEM = {}

# Prune the throttle cache once it grows past this many chats so it can't leak
# an unbounded entry per chat the bot has ever seen.
_LAST_SEEN_MEM_MAX = 10000


def _prune_last_seen(now):
    """Drop entries older than the throttle window — they no longer suppress
    a write, so keeping them only wastes memory."""
    cutoff = RECORD_THROTTLE_SECONDS
    stale = [cid for cid, ts in _LAST_SEEN_MEM.items()
             if (now - ts).total_seconds() >= cutoff]
    for cid in stale:
        _LAST_SEEN_MEM.pop(cid, None)
    # If everything is still fresh (huge active chat count), hard-cap to avoid
    # unbounded growth.
    if len(_LAST_SEEN_MEM) > _LAST_SEEN_MEM_MAX:
        _LAST_SEEN_MEM.clear()


def record_chat(update: Update):
    """Backfill — every update upserts the chat into bot_chats.

    Throttled to at most one write per chat per RECORD_THROTTLE_SECONDS.
    Failures swallowed silently — we never drop user messages over telemetry.
    """
    try:
        chat = update.effective_chat
        if not chat:
            return

        # Throttle: if we recorded this chat recently, skip the DB hit
        now = datetime.utcnow()
        last = _LAST_SEEN_MEM.get(chat.id)
        if last is not None and (now - last).total_seconds() < RECORD_THROTTLE_SECONDS:
            return
        if len(_LAST_SEEN_MEM) > _LAST_SEEN_MEM_MAX:
            _prune_last_seen(now)

        from database import get_session
        from models import BotChat
        s = get_session()
        try:
            row = (s.query(BotChat)
                    .filter(BotChat.chat_id == chat.id)
                    .first())
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
            _LAST_SEEN_MEM[chat.id] = now  # mark cache only on success
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

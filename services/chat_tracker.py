"""Track Telegram chats the bot is in, so admin broadcasts can target them.

Three entry points:
  1. `record_chat(update)` — called from middleware on EVERY incoming update.
     Throttled in-memory: at most one DB write per chat per RECORD_THROTTLE_SECONDS.
     This keeps Neon compute hours low — the bot can field thousands of
     messages without touching `bot_chats` more than once per chat per cycle.
  2. `record_chat_member(update)` — also called from middleware, learns WHICH
     managers are in each group (Telegram will not tell a bot), so /owners can
     answer "who in this group owns this player". Throttled far harder than
     `record_chat`: one write per member per chat per MEMBER_THROTTLE_SECONDS.
  3. `handle_chat_member_update(update, context)` — registered as a
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


# ════════════════════════════════════════════════════════════════════
# Group membership — who is in which group (powers /owners)
# ════════════════════════════════════════════════════════════════════

# One membership write per (chat, member) per this many seconds. Membership
# barely changes, so this is deliberately far coarser than the chat throttle:
# a 200-strong group costs ~200 writes a day, not one per message.
MEMBER_THROTTLE_SECONDS = 6 * 3600  # 6 hours

_MEMBER_SEEN_MEM = {}
_MEMBER_SEEN_MEM_MAX = 50000

GROUP_CHAT_TYPES = ("group", "supergroup")


def _prune_member_seen(now):
    """Drop membership-throttle entries that no longer suppress a write."""
    stale = [key for key, ts in _MEMBER_SEEN_MEM.items()
             if (now - ts).total_seconds() >= MEMBER_THROTTLE_SECONDS]
    for key in stale:
        _MEMBER_SEEN_MEM.pop(key, None)
    if len(_MEMBER_SEEN_MEM) > _MEMBER_SEEN_MEM_MAX:
        _MEMBER_SEEN_MEM.clear()


def _upsert_member(session, chat_id, user_id, now, active=True):
    """Create or refresh one ``chat_members`` row. Caller owns the commit."""
    from models import ChatMember as ChatMemberRow
    row = (session.query(ChatMemberRow)
           .filter(ChatMemberRow.chat_id == chat_id,
                   ChatMemberRow.user_id == user_id)
           .first())
    if not row:
        if not active:
            return None  # nothing to deactivate
        row = ChatMemberRow(chat_id=chat_id, user_id=user_id, is_active=True,
                            first_seen_at=now, last_seen_at=now)
        session.add(row)
        return row
    row.is_active = active
    row.last_seen_at = now
    return row


def record_chat_member(update: Update):
    """Learn group membership from ordinary activity.

    Every update from a group tells us one true thing: this user is in this
    group right now. That is the only membership signal a bot gets, so it is
    what /owners is built on. Only users who have already done /debut are
    recorded — the rest own nothing, so a row for them would be dead weight.

    Also acts on the two explicit membership events Telegram *does* send:
    ``new_chat_members`` (record immediately, no throttle) and
    ``left_chat_member`` (flip the row inactive so they stop being counted).

    Best-effort: never raises, never blocks a user's message.
    """
    try:
        chat = getattr(update, "effective_chat", None)
        if not chat or (chat.type or "") not in GROUP_CHAT_TYPES:
            return

        message = (getattr(update, "effective_message", None)
                   or getattr(update, "message", None))
        joined = list(getattr(message, "new_chat_members", None) or [])
        left = getattr(message, "left_chat_member", None)
        sender = getattr(update, "effective_user", None)

        now = datetime.utcnow()

        # Joins/leaves are rare and authoritative — always write them through.
        forced = [(u, True) for u in joined if not getattr(u, "is_bot", False)]
        if left is not None and not getattr(left, "is_bot", False):
            forced.append((left, False))

        throttled = None
        if (sender is not None and not getattr(sender, "is_bot", False)
                and not any(u.id == sender.id for u, _ in forced)):
            key = (chat.id, sender.id)
            last = _MEMBER_SEEN_MEM.get(key)
            if last is None or (now - last).total_seconds() >= MEMBER_THROTTLE_SECONDS:
                throttled = sender

        if not forced and throttled is None:
            return

        from database import get_session
        from models import User
        s = get_session()
        try:
            tg_ids = [u.id for u, _ in forced]
            if throttled is not None:
                tg_ids.append(throttled.id)
            known = {u.telegram_id: u.id for u in
                     s.query(User).filter(User.telegram_id.in_(tg_ids)).all()}

            wrote = False
            for tg_user, active in forced:
                uid = known.get(tg_user.id)
                if uid and _upsert_member(s, chat.id, uid, now, active) is not None:
                    wrote = True
            if throttled is not None:
                uid = known.get(throttled.id)
                if uid:
                    _upsert_member(s, chat.id, uid, now, True)
                    wrote = True

            if wrote:
                s.commit()
            # Mark the throttle only once the write actually landed, so a failed
            # write is retried on the next message instead of being skipped.
            if throttled is not None and known.get(throttled.id):
                if len(_MEMBER_SEEN_MEM) > _MEMBER_SEEN_MEM_MAX:
                    _prune_member_seen(now)
                _MEMBER_SEEN_MEM[(chat.id, throttled.id)] = now
        except Exception:
            s.rollback()
            logger.debug("record_chat_member write failed (non-fatal)",
                         exc_info=True)
        finally:
            s.close()
    except Exception:
        logger.debug("record_chat_member failed (non-fatal)", exc_info=True)


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

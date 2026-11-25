"""Chat XP + anti-spam for the Official CMU Group.

One CXP per eligible conversational message, defended against spam cheaply:

* an **in-memory fast path** (per-user cooldown, per-IST-day counter, and a
  bounded expiring cache of recent message fingerprints) answers the
  overwhelming majority of messages without touching the database — only a
  message that passes every gate pays for one award transaction, and the 45s
  cooldown bounds that to roughly one write per active chatter per cooldown;
* the authoritative per-day limit and overall cap still live in the database
  (see :func:`services.community_xp_service.award_xp`), so a restart cannot
  grant more than the configured maximum.

Ignored: commands, bot/forwarded/edited/service messages, sticker/GIF/emoji-only
messages, duplicates and meaningless character repetition, and anything sent
while a community game is live in the chat (those are treated as game answers).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime

logger = logging.getLogger(__name__)

# In-memory per-user state. Keyed by user_id.
#   _LAST_REWARD[user_id] = epoch_seconds of last rewarded message
#   _DAY_COUNT[user_id]    = (ist_date_str, count)
#   _FINGERPRINTS[user_id] = {fingerprint: epoch_seconds}
_LAST_REWARD: dict = {}
_DAY_COUNT: dict = {}
_FINGERPRINTS: dict = {}

_MEM_MAX_USERS = 50000
_FP_PER_USER = 40            # recent fingerprints kept per user
_FP_TTL_SECONDS = 600        # a repeated message within 10 min is a duplicate


def _prune(now_epoch):
    if len(_LAST_REWARD) > _MEM_MAX_USERS:
        _LAST_REWARD.clear()
    if len(_DAY_COUNT) > _MEM_MAX_USERS:
        _DAY_COUNT.clear()
    if len(_FINGERPRINTS) > _MEM_MAX_USERS:
        _FINGERPRINTS.clear()


def normalize(text: str) -> str:
    """Lowercase, strip accents/punctuation/whitespace → alnum fingerprint."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    t = t.lower()
    t = re.sub(r"[^a-z0-9]+", "", t)
    return t


def _is_meaningful(norm: str, min_chars: int) -> bool:
    if len(norm) < min_chars:
        return False
    # Reject meaningless repetition, e.g. "aaaaaaaa" or "lolololol".
    uniq = len(set(norm))
    if uniq <= 2:
        return False
    return True


def message_is_eligible(message, *, min_chars: int) -> tuple[bool, str]:
    """Pure check: is this a rewardable conversational message?

    Returns (eligible, fingerprint). Does not touch the database or the
    in-memory counters.
    """
    if message is None:
        return False, ""
    # Edited messages arrive as edited_message; a plain `message` here is fresh.
    if getattr(message, "forward_origin", None) or getattr(message, "forward_date", None):
        return False, ""
    # Service messages (joins, pins, etc.) have no text and special fields.
    if (getattr(message, "new_chat_members", None)
            or getattr(message, "left_chat_member", None)
            or getattr(message, "pinned_message", None)):
        return False, ""
    # Media-only messages: sticker/GIF/photo/etc. with no caption text.
    if getattr(message, "sticker", None) or getattr(message, "animation", None):
        return False, ""
    text = message.text or message.caption or ""
    if not text:
        return False, ""
    # Commands are not conversation.
    if text.lstrip().startswith("/"):
        return False, ""
    norm = normalize(text)
    if not _is_meaningful(norm, min_chars):
        return False, ""
    return True, norm


def _within_cooldown(user_id, now_epoch, cooldown):
    last = _LAST_REWARD.get(user_id)
    return last is not None and (now_epoch - last) < cooldown


def _day_count(user_id, day):
    rec = _DAY_COUNT.get(user_id)
    if rec is None or rec[0] != day:
        return 0
    return rec[1]


def _is_duplicate(user_id, fp, now_epoch):
    fps = _FINGERPRINTS.get(user_id)
    if not fps:
        return False
    ts = fps.get(fp)
    return ts is not None and (now_epoch - ts) < _FP_TTL_SECONDS


def _remember(user_id, fp, now_epoch):
    fps = _FINGERPRINTS.setdefault(user_id, {})
    # Drop expired + cap size.
    if len(fps) >= _FP_PER_USER:
        for k in [k for k, v in list(fps.items()) if now_epoch - v >= _FP_TTL_SECONDS]:
            fps.pop(k, None)
        while len(fps) >= _FP_PER_USER:
            fps.pop(next(iter(fps)), None)
    fps[fp] = now_epoch


def should_skip_for_active_game(chat_id) -> bool:
    """A message sent while a game is live in this chat is a game answer, not
    conversation — don't pay chat XP for it."""
    try:
        from services import community_game_service as G
        return G.has_active_game(chat_id)
    except Exception:
        return False


async def maybe_award_chat_xp(update, *, tg_user_id: int, chat_id: int,
                              config: dict, now=None) -> bool:
    """Award one CXP for an eligible message. Returns True if credited.

    The in-memory fast-path gates are keyed by Telegram id, so the common case
    (not eligible / within cooldown / over the cap / duplicate) costs no
    database access at all. Only a message that passes every gate resolves the
    manager's row and awards, off the event loop. Best-effort: never raises.
    """
    try:
        import time as _time
        from services import community_xp_service as X

        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        chat_cfg = config.get("chat", {})
        min_chars = int(chat_cfg.get("min_chars", 8))
        cooldown = int(chat_cfg.get("cooldown_seconds", 45))
        daily_limit = int(chat_cfg.get("daily_limit", 50))

        eligible, fp = message_is_eligible(message, min_chars=min_chars)
        if not eligible:
            return False
        if should_skip_for_active_game(chat_id):
            return False

        now = now or datetime.utcnow()
        now_epoch = _time.time()
        day = X.ist_today_str(now)

        _prune(now_epoch)
        if _within_cooldown(tg_user_id, now_epoch, cooldown):
            return False
        if _day_count(tg_user_id, day) >= daily_limit:
            return False
        if _is_duplicate(tg_user_id, fp, now_epoch):
            return False

        # Reserve the in-memory slot *before* the DB round trip so two messages
        # racing on the event loop can't both award.
        _LAST_REWARD[tg_user_id] = now_epoch
        _remember(tg_user_id, fp, now_epoch)

        msg_id = getattr(message, "message_id", None)
        xp = int(config.get("xp", {}).get("chat", {}).get("xp", 1))

        import asyncio
        credited = await asyncio.to_thread(
            _award_worker, tg_user_id, chat_id, msg_id, xp, daily_limit, day)

        if credited:
            rec = _DAY_COUNT.get(tg_user_id)
            _DAY_COUNT[tg_user_id] = (day, (rec[1] + 1 if rec and rec[0] == day else 1))
            return True
        return False
    except Exception:
        logger.debug("maybe_award_chat_xp failed (non-fatal)", exc_info=True)
        return False


def _award_worker(tg_user_id, chat_id, msg_id, xp, daily_limit, day) -> bool:
    """Resolve the manager and award chat XP (blocking; own session)."""
    from database import get_session
    from models import User
    from services import community_xp_service as X
    s = get_session()
    try:
        u = s.query(User).filter(User.telegram_id == tg_user_id).first()
        if u is None:
            return False
        res = X.award_xp(
            u.id, X.ACT_CHAT, xp, event_key=f"chat:{u.id}:{chat_id}:{msg_id}",
            chat_id=chat_id, detail="chat message", session=s,
            activity_daily_limit=daily_limit)
        if res.get("credited"):
            X.bump_profile_counter(s, u.id, "chat_messages", day_ist=day)
            try:
                from services import community_achievement_service as A
                A.check_chat_achievements(s, u.id)
            except Exception:
                logger.debug("chat achievement check failed", exc_info=True)
            s.commit()
            return True
        s.commit()
        return False
    except Exception:
        s.rollback()
        logger.debug("_award_worker failed (non-fatal)", exc_info=True)
        return False
    finally:
        s.close()


def reset_memory():
    """Test helper — clear all in-memory state."""
    _LAST_REWARD.clear()
    _DAY_COUNT.clear()
    _FINGERPRINTS.clear()

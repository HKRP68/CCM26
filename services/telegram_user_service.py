"""Helpers for keeping Telegram user details fresh and resolving command targets."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import or_

from models import User

logger = logging.getLogger(__name__)


def _clean_username(username: str | None) -> str:
    return (username or "").strip().lstrip("@")


def sync_telegram_user(session, tg_user: Any):
    """Refresh stored username/first_name for a Telegram user that already debuted.

    Telegram does not let bots resolve arbitrary @usernames to user ids. The best
    source of truth is therefore every real Telegram user object the bot receives
    (command sender, replied-message author, text_mention entities). When a user
    changes or adds a username, this updates the website and future lookups.
    """
    if tg_user is None or getattr(tg_user, "id", None) is None:
        return None

    user = session.query(User).filter(User.telegram_id == tg_user.id).first()
    if not user:
        return None

    changed = False
    username = _clean_username(getattr(tg_user, "username", None))
    first_name = (getattr(tg_user, "first_name", None) or "").strip()

    if (user.username or "") != username:
        user.username = username
        changed = True
    if first_name and (user.first_name or "") != first_name:
        user.first_name = first_name
        changed = True

    if changed:
        session.flush()
    return user


def sync_update_users(session, update: Any) -> None:
    """Refresh known users visible in an incoming update."""
    sync_telegram_user(session, getattr(update, "effective_user", None))

    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    if message is None:
        return

    reply = getattr(message, "reply_to_message", None)
    if reply is not None:
        sync_telegram_user(session, getattr(reply, "from_user", None))


def record_miniapp_origin(tg_id, chat_id) -> None:
    """Remember the group a user opened the Mini App from.

    Called from the bot command handlers that surface a Mini App launch button
    in a group (``/app``, ``/daily``, ``/gspin``, the player market). Persisting
    the origin here — server-side, the moment the command runs — means Mini App
    activity (daily, packs, gspin, buys) can echo back into that group even when
    Telegram drops the launch ``start_param`` from the signed ``initData`` (a
    real cross-client quirk) or serves a cached Mini App page. Best-effort and
    self-contained; never raises. Only group/supergroup ids (negative) are kept.
    """
    try:
        cid = int(chat_id)
        uid = int(tg_id)
    except (TypeError, ValueError):
        return
    if cid >= 0:
        return
    from database import get_session
    from models import User
    s = get_session()
    try:
        u = s.query(User).filter(User.telegram_id == uid).first()
        if u and u.last_miniapp_chat_id != cid:
            u.last_miniapp_chat_id = cid
            s.commit()
    except Exception:
        s.rollback()
        logger.exception("record_miniapp_origin failed")
    finally:
        s.close()

    for entity in (getattr(message, "entities", None) or []):
        sync_telegram_user(session, getattr(entity, "user", None))


def _target_from_reply(session, message: Any):
    reply = getattr(message, "reply_to_message", None) if message is not None else None
    tg_user = getattr(reply, "from_user", None) if reply is not None else None
    if tg_user is None or getattr(tg_user, "is_bot", False):
        return None
    return sync_telegram_user(session, tg_user)


def _target_from_text_mention(session, message: Any, command_name: str):
    if message is None:
        return None
    text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    for entity in (getattr(message, "entities", None) or []):
        if getattr(entity, "type", None) != "text_mention":
            continue
        try:
            mention_text = text[entity.offset: entity.offset + entity.length]
        except Exception:
            mention_text = ""
        # Do not use the command sender if Telegram attached a text_mention to
        # the slash command itself for any client-specific reason.
        if mention_text.startswith(f"/{command_name}"):
            continue
        user = sync_telegram_user(session, getattr(entity, "user", None))
        if user:
            return user
    return None


def _target_from_arg(session, raw_arg: str | None):
    raw = (raw_arg or "").strip()
    if not raw:
        return None, "missing"

    if raw.isdigit():
        return session.query(User).filter(User.telegram_id == int(raw)).first(), "user_id"

    if not raw.startswith("@"):
        return None, "not_mention"

    username = _clean_username(raw)
    if not username:
        return None, "missing"

    return session.query(User).filter(User.username.ilike(username)).first(), "username"


def resolve_command_target(session, update: Any, context: Any, command_name: str):
    """Resolve another-user command targets by reply, text_mention, user_id, or @username.

    Returns ``(user, reason)``. ``reason`` is one of: reply, text_mention,
    user_id, username, missing, not_mention, not_found.
    """
    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    args = list(getattr(context, "args", None) or [])

    if args:
        user, source = _target_from_arg(session, args[0])
        if user:
            return user, source
        if source == "not_mention":
            # Telegram clients can send a clickable no-username mention as plain
            # text plus a text_mention entity. That text may look like "Raj",
            # so only accept it when the entity gives us the real user_id.
            user = _target_from_text_mention(session, message, command_name)
            if user:
                return user, "text_mention"
        return None, source if source in {"missing", "not_mention"} else "not_found"

    user = _target_from_reply(session, message)
    if user:
        return user, "reply"

    user = _target_from_text_mention(session, message, command_name)
    if user:
        return user, "text_mention"

    return None, "missing"


def user_lookup_filter(q: str):
    """Build website user-search filter for username, name, app id, or Telegram id."""
    like = f"%{q}%"
    filters = [User.username.ilike(like), User.first_name.ilike(like)]
    if q.isdigit():
        value = int(q)
        filters.extend([User.id == value, User.telegram_id == value])
    return or_(*filters)

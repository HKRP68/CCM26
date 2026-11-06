"""Account warnings and their audit trail. Callers own commit/rollback.

The users row serializes warning changes on PostgreSQL and SQLite, keeping
the five-warning threshold and removals consistent across concurrent updates.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, update

from models import AdminLog, User, UserWarning
from services.admin_ids import is_bot_admin

WARN_LIMIT = 5


@dataclass(frozen=True)
class WarningResult:
    telegram_id: int
    name: str
    count: int
    is_banned: bool
    warning_ban: bool
    action: str
    warning_id: int
    reason: str
    changed: bool
    unbanned: bool = False


def lock_user(session, user):
    """Acquire a write lock before counting warnings or changing ban ownership."""
    session.execute(update(User).where(User.id == user.id).values(
        is_banned=User.is_banned, updated_at=User.updated_at),
        execution_options={"synchronize_session": False})
    session.refresh(user)


def find_user(session, target):
    if target.isascii() and target.isdigit():
        user = session.query(User).filter(User.telegram_id == int(target)).first()
    else:
        matches = (session.query(User)
                   .filter(func.lower(User.username) == target.lstrip("@").lower())
                   .limit(2).all())
        if len(matches) > 1:
            raise ValueError("More than one stored player has that username. Use their Telegram user ID.")
        user = matches[0] if matches else None
    if user is None:
        raise ValueError("Player not found. Use a Telegram user ID or reply to a player who has run /debut.")
    return user


def active_warnings(session, user_id):
    return session.query(UserWarning).filter(
        UserWarning.user_id == user_id, UserWarning.revoked_at.is_(None))


def _audit(session, user, action, detail):
    session.add(AdminLog(
        action=action, target_type="user", target_id=user.id,
        target_name=(user.username or user.first_name or str(user.telegram_id))[:150],
        detail=detail[:500]))


def change_warning(session, target, *, action, reason, admin_telegram_id,
                   message_key=None):
    """Issue/revoke one warning, preserving history and manual bans.

    A Telegram chat/message pair deduplicates retries, including retries after
    a restart. Removing a warning always revokes the newest active warning.
    """
    if not is_bot_admin(admin_telegram_id):
        raise ValueError("Only bot admins can change warnings.")
    if action not in {"warn", "unwarn"}:
        raise ValueError("Unknown warning action.")
    reason = (reason or "").strip()
    if len(reason) > 500:
        raise ValueError("Keep the reason to 500 characters or fewer.")
    reason = reason or ("No reason provided." if action == "warn" else "Removed by admin.")
    user = find_user(session, target)
    if action == "warn" and is_bot_admin(user.telegram_id):
        raise ValueError("Bot admins and owners cannot receive warnings.")
    lock_user(session, user)

    count = active_warnings(session, user.id).count()
    name = f"@{user.username}" if user.username else user.first_name or "Player"

    def result(warning, *, changed, unbanned=False):
        return WarningResult(
            telegram_id=user.telegram_id, name=name, count=count,
            is_banned=bool(user.is_banned), warning_ban=bool(user.warning_ban),
            action=action, warning_id=warning.id, reason=reason,
            changed=changed, unbanned=unbanned)

    if message_key is not None:
        chat_col, message_col = (
            (UserWarning.issued_chat_id, UserWarning.issued_message_id)
            if action == "warn" else
            (UserWarning.revoked_chat_id, UserWarning.revoked_message_id))
        previous = session.query(UserWarning).filter(
            chat_col == message_key[0], message_col == message_key[1]).first()
        if previous is not None:
            if previous.user_id != user.id:
                raise ValueError("This moderation command was already processed for another player.")
            return result(previous, changed=False)

    now = datetime.utcnow()
    unbanned = False
    if action == "warn":
        if user.is_banned:
            raise ValueError("This player is already banned. Use /cmuwarns to inspect their warnings.")
        if count >= WARN_LIMIT:
            raise ValueError("This player already has five active warnings. Remove a warning before adding another.")
        warning = UserWarning(user_id=user.id, reason=reason,
                              issued_by=admin_telegram_id, created_at=now)
        if message_key is not None:
            warning.issued_chat_id, warning.issued_message_id = message_key
        session.add(warning)
        count += 1
        if count == WARN_LIMIT:
            user.is_banned = True
            user.warning_ban = True
            user.banned_at = now
            user.ban_reason = f"Reached {WARN_LIMIT} active warnings. Latest reason: {reason}"[:500]
            _audit(session, user, "user_warning_ban",
                   f"Automatic ban at {count}/{WARN_LIMIT} warnings; Telegram admin {admin_telegram_id}")
    else:
        warning = active_warnings(session, user.id).order_by(
            UserWarning.created_at.desc(), UserWarning.id.desc()).first()
        if warning is None:
            raise ValueError("This player has no active warnings to remove.")
        warning.revoked_at = now
        warning.revoked_by = admin_telegram_id
        warning.revoke_reason = reason
        if message_key is not None:
            warning.revoked_chat_id, warning.revoked_message_id = message_key
        count -= 1
        if user.warning_ban and count < WARN_LIMIT:
            unbanned = bool(user.is_banned)
            user.is_banned = False
            user.warning_ban = False
            user.ban_reason = None
            user.banned_at = None
            _audit(session, user, "user_warning_unban",
                   f"Warning ban lifted at {count}/{WARN_LIMIT}; Telegram admin {admin_telegram_id}")

    session.flush()
    _audit(session, user, f"user_{action}",
           f"Telegram admin {admin_telegram_id}; warning #{warning.id}; "
           f"active {count}/{WARN_LIMIT}; reason: {reason}")
    return result(warning, changed=True, unbanned=unbanned)

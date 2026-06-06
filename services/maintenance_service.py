"""Maintenance mode helpers.

The bot's middleware (in bot.py) calls `is_maintenance_for(user_telegram_id)`
on every update. If True, the command handler is short-circuited with a
polite "under maintenance" message that includes the ETA if set.

Admin bypass: telegram_ids in `maintenance_bypass_ids` (comma-separated)
are allowed to use the bot normally even during maintenance — useful for
admins testing through the lockout.

Match protection: matches already in progress continue to work. New /playmatch,
/vsbot etc. are blocked. The middleware checks the update type — callback
queries (taps on in-match buttons) are allowed through so existing matches
can finish.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def _bypass_ids(cfg):
    """Parse comma-separated bypass IDs from config."""
    raw = (cfg or {}).get("maintenance_bypass_ids") or ""
    out = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk: continue
        try:
            out.add(int(chunk))
        except ValueError:
            continue
    return out


def is_maintenance_active(cfg=None):
    """True if maintenance mode is currently on.

    If `maintenance_until` is set and in the past, treats maintenance as
    OFF (auto-expire) — but we DON'T flip the DB flag here; admin still
    has to acknowledge. This way users get correct UX without race condition.
    """
    if cfg is None:
        from services.config_service import get_config
        cfg = get_config()
    if not cfg.get("is_maintenance"):
        return False
    until = cfg.get("maintenance_until")
    if until and isinstance(until, datetime) and until < datetime.utcnow():
        return False  # auto-expired (admin still needs to disable)
    return True


def is_bypassed(telegram_id, cfg=None):
    """True if this telegram_id is whitelisted to bypass maintenance."""
    if cfg is None:
        from services.config_service import get_config
        cfg = get_config()
    return int(telegram_id) in _bypass_ids(cfg)


def get_maintenance_message(cfg=None):
    """Render the user-facing maintenance message with the ETA filled in
    if available. Falls back to a sensible default if no custom message set.
    """
    if cfg is None:
        from services.config_service import get_config
        cfg = get_config()
    custom = (cfg.get("maintenance_message") or "").strip()
    until = cfg.get("maintenance_until")

    eta_line = ""
    if until and isinstance(until, datetime):
        # Show in user-friendly format. We're storing UTC; convert to IST
        # (UTC+5:30) since the user is in India per memories.
        try:
            from datetime import timedelta
            ist = until + timedelta(hours=5, minutes=30)
            eta_line = f"\n\n⏰ <b>Back at:</b> {ist.strftime('%I:%M %p IST, %d %b')}"
        except Exception:
            eta_line = f"\n\n⏰ Back at: {until.strftime('%H:%M UTC')}"

    if custom:
        # Allow {eta} placeholder, but also append eta_line if not present
        if "{eta}" in custom:
            try:
                from datetime import timedelta
                eta_str = ""
                if until and isinstance(until, datetime):
                    ist = until + timedelta(hours=5, minutes=30)
                    eta_str = ist.strftime("%I:%M %p IST, %d %b")
                return custom.replace("{eta}", eta_str or "soon")
            except Exception:
                return custom
        return custom + eta_line

    # Default message
    return (
        "🛠️ <b>Bot is under maintenance.</b>\n\n"
        "We'll be back shortly — thanks for your patience!"
        + eta_line
    )


def is_command_update(update):
    """Return True when the update message is a bot command.

    During maintenance we still block non-command text so normal handlers do
    not run, but only command messages should receive a maintenance reply.
    """
    message = getattr(update, "message", None)
    text = (getattr(message, "text", None) or "").lstrip() if message else ""
    if not text:
        return False
    if text.startswith("/"):
        return True

    # Be defensive for test doubles or PTB message-like objects that expose
    # Telegram entities even when text has not been normalized.
    for entity in getattr(message, "entities", None) or []:
        entity_type = getattr(entity, "type", None)
        offset = getattr(entity, "offset", None)
        if entity_type == "bot_command" and offset == 0:
            return True
    return False


def should_reply_with_maintenance(update):
    """True when a blocked update should receive the maintenance message."""
    return is_command_update(update)


def should_block_update(update, cfg=None):
    """Decide whether to block this update.

    Rules:
      - Maintenance off → never block
      - User is in bypass list → never block
      - Callback queries (taps on in-progress match buttons) → allowed
        through so users can finish matches already in flight
      - Command messages → block and show the maintenance message
      - Non-command messages → block silently so group chats are not spammed
    """
    if cfg is None:
        from services.config_service import get_config
        cfg = get_config()
    if not is_maintenance_active(cfg):
        return False
    user = update.effective_user
    if user and is_bypassed(user.id, cfg):
        return False
    # Allow callback queries (in-progress match interactions)
    if update.callback_query:
        return False
    return True

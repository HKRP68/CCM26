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

Mini App: the same lock covers the Telegram Mini App. `should_block_miniapp_path`
is called from a Flask before-request hook in admin.py, so every Mini App API —
including endpoints added later — is blocked by default. The live-match
endpoints listed in MINIAPP_LIVE_MATCH_PATHS stay open, mirroring the bot's
callback-query allowance so a match already in flight can still be finished.
"""

import logging
import os
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


def _normalize_bot_username(username):
    """Normalize a Telegram bot username for case-insensitive comparison."""
    return (username or "").strip().lstrip("@").casefold()


def _command_token_from_text(text):
    """Return the leading slash-command token from text, if present."""
    text = (text or "").lstrip()
    if not text.startswith("/"):
        return ""
    return text.split(maxsplit=1)[0]


def _is_command_for_this_bot(command_token, bot_username=None):
    """True for bare commands or commands addressed to this bot.

    Telegram group chats can contain commands explicitly addressed to another
    bot (for example, ``/help@OtherBot``). Normal CommandHandlers ignore those,
    so the maintenance middleware must ignore them too instead of sending this
    bot's maintenance response.
    """
    if not command_token.startswith("/"):
        return False

    if "@" not in command_token:
        return True

    addressed_username = _normalize_bot_username(command_token.rsplit("@", 1)[1])
    this_username = _normalize_bot_username(bot_username or os.getenv("BOT_USERNAME"))
    return bool(this_username and addressed_username == this_username)


def is_command_update(update, bot_username=None):
    """Return True when the update message is a command for this bot.

    During maintenance we still block non-command text so normal handlers do
    not run, but only bare commands and commands addressed to this bot should
    receive a maintenance reply. Commands addressed to another bot are treated
    like group chatter and blocked silently.
    """
    message = getattr(update, "message", None)
    text = (getattr(message, "text", None) or "") if message else ""
    token = _command_token_from_text(text)
    if token:
        return _is_command_for_this_bot(token, bot_username)

    # Be defensive for test doubles or PTB message-like objects that expose
    # Telegram entities even when text has not been normalized.
    lstripped_text = text.lstrip()
    leading_spaces = len(text) - len(lstripped_text)
    for entity in getattr(message, "entities", None) or []:
        entity_type = getattr(entity, "type", None)
        offset = getattr(entity, "offset", None)
        if entity_type != "bot_command" or offset not in (0, leading_spaces):
            continue
        length = getattr(entity, "length", None)
        entity_token = (
            lstripped_text[:length]
            if length
            else _command_token_from_text(lstripped_text)
        )
        return _is_command_for_this_bot(entity_token, bot_username)
    return False


def should_reply_with_maintenance(update, bot_username=None):
    """True when a blocked update should receive the maintenance message."""
    return is_command_update(update, bot_username)


# ── Mini App lock ───────────────────────────────────────────────────────────
# Anything the Mini App can call lives under one of these prefixes. Blocking by
# prefix (rather than by listing endpoints) means a Mini App API added later is
# locked during maintenance by default instead of quietly staying open.
MINIAPP_GUARDED_PREFIXES = (
    "/api/webapp/",
    "/api/fantasy/",
    "/api/ipl16/",
    "/api/ads/",
    "/api/adsgram/",
    "/api/monetag/",
)

# Endpoints a match already in progress needs to reach the final ball. These
# stay open during maintenance for the same reason the bot lets callback
# queries through: locking mid-over would strand a live match, not protect it.
# None of these can start a new match — the Mini App creates matches through
# /api/webapp/quickmatch/start and the bot's own commands, which stay blocked.
MINIAPP_LIVE_MATCH_PATHS = (
    "/api/webapp/match/",
    "/api/webapp/quickmatch/phase",
    "/api/webapp/quickmatch/abandon",
)


def is_miniapp_path(path):
    """True when this request path belongs to a lockable Mini App API."""
    path = path or ""
    return any(path.startswith(prefix) for prefix in MINIAPP_GUARDED_PREFIXES)


def is_miniapp_live_match_path(path):
    """True for the Mini App endpoints an in-flight match still needs."""
    path = path or ""
    return any(path == allowed or path.startswith(allowed)
               for allowed in MINIAPP_LIVE_MATCH_PATHS)


def should_block_miniapp_path(path, telegram_id=None, cfg=None):
    """Decide whether a Mini App request should be blocked.

    Rules mirror :func:`should_block_update`:
      - Maintenance off → never block
      - Path is not a Mini App API → not ours to block
      - User is in the bypass list → never block
      - Live-match endpoints → allowed through so matches can finish
      - Everything else → blocked
    """
    if not is_miniapp_path(path):
        return False
    if cfg is None:
        from services.config_service import get_config
        cfg = get_config()
    if not is_maintenance_active(cfg):
        return False
    if telegram_id is not None and is_bypassed(telegram_id, cfg):
        return False
    return not is_miniapp_live_match_path(path)


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

"""In-memory flags that let the match heartbeat fast-skip when nothing
needs doing — keeps Neon's compute idle when the bot is idle.

The hot path (heartbeat tick) reads these without touching the DB. The
flags get refreshed on:
  - Bot startup (one query to count active matches + notifications)
  - Each match start / end (increment / decrement)
  - Each scheduled-notification CRUD (increment / decrement)
  - Periodically from the heartbeat itself (defensive sync, every 5 min)

If you can't easily intercept all match start/end paths, the periodic
sync still catches drift within ~5 minutes.

Persistence: these flags live on Application.bot_data so they survive
across handler calls but reset on process restart. Startup re-syncs from
DB on the first tick.
"""

from datetime import datetime

# Keys on bot_data
_K_ACTIVE_MATCHES = "_hb_active_matches_count"
_K_LAST_SYNC = "_hb_last_sync_at"
_K_NOTIFICATIONS_FLAG = "_hb_notifs_enabled"

# Force a defensive resync from DB at most every this many seconds
SYNC_INTERVAL = 300  # 5 minutes


def _bot_data(context_or_app):
    """Get the bot_data dict from either a Context or Application."""
    if hasattr(context_or_app, "bot_data"):
        return context_or_app.bot_data
    if hasattr(context_or_app, "application"):
        return context_or_app.application.bot_data
    # Fallback: empty dict (shouldn't happen in practice)
    return {}


def has_active_matches(context_or_app) -> bool:
    """Cheap in-memory check. Returns True only if we know matches exist.

    Defensively re-syncs from DB once per SYNC_INTERVAL so a missed
    increment/decrement (e.g. crash) doesn't permanently lock us into
    the wrong state.
    """
    bd = _bot_data(context_or_app)
    now = datetime.utcnow()

    # Defensive periodic resync
    last = bd.get(_K_LAST_SYNC)
    if last is None or (now - last).total_seconds() > SYNC_INTERVAL:
        try:
            from database import get_session
            from models import MatchState
            s = get_session()
            try:
                count = s.query(MatchState).count()
            finally:
                s.close()
            bd[_K_ACTIVE_MATCHES] = int(count)
            bd[_K_LAST_SYNC] = now
        except Exception:
            # If DB is unreachable, fall back to current cached value
            pass

    return (bd.get(_K_ACTIVE_MATCHES) or 0) > 0


def set_active_match_count(context_or_app, n: int):
    """Sync the active-match count after the heartbeat actually queried DB.

    Allows the in-memory flag to track reality.
    """
    bd = _bot_data(context_or_app)
    bd[_K_ACTIVE_MATCHES] = int(n)
    bd[_K_LAST_SYNC] = datetime.utcnow()


def increment_active_matches(context_or_app):
    """Call when a new match starts."""
    bd = _bot_data(context_or_app)
    bd[_K_ACTIVE_MATCHES] = (bd.get(_K_ACTIVE_MATCHES) or 0) + 1


def decrement_active_matches(context_or_app):
    """Call when a match ends. Floors at 0."""
    bd = _bot_data(context_or_app)
    bd[_K_ACTIVE_MATCHES] = max(0, (bd.get(_K_ACTIVE_MATCHES) or 0) - 1)


# ───── Notification flag ─────
# We don't track individual schedules — just a yes/no whether ANY active
# schedule exists. Set by notification_service on CRUD; re-synced from DB
# at startup.

_LAST_NOTIF_SYNC = {"at": None}
_NOTIF_CACHED = {"value": None}


def has_due_notifications() -> bool:
    """Cheap check: are there any active notification schedules at all?

    If none, the heartbeat can skip the notification tick entirely.
    Cached for SYNC_INTERVAL seconds.
    """
    now = datetime.utcnow()
    last = _LAST_NOTIF_SYNC["at"]
    if last is None or (now - last).total_seconds() > SYNC_INTERVAL:
        try:
            from database import get_session
            from models import NotificationSchedule
            s = get_session()
            try:
                n = (s.query(NotificationSchedule)
                     .filter(NotificationSchedule.is_active == True).count())
            finally:
                s.close()
            _NOTIF_CACHED["value"] = (n > 0)
            _LAST_NOTIF_SYNC["at"] = now
        except Exception:
            # On error, assume notifications might be due (safer to over-tick)
            return True
    return bool(_NOTIF_CACHED["value"])


def invalidate_notification_cache():
    """Call after creating/editing/deleting a notification schedule."""
    _LAST_NOTIF_SYNC["at"] = None
    _NOTIF_CACHED["value"] = None

"""Log all user actions to ActivityLog table."""

import logging
from datetime import datetime, timedelta

from models import ActivityLog

logger = logging.getLogger(__name__)

# The website reports retention on the IST calendar, so throttle "start" rows
# per IST day to match (timestamps themselves are still stored in UTC).
_IST_OFFSET = timedelta(hours=5, minutes=30)


def log_activity(session, user_id: int, action: str, detail: str = "",
                 coins_change: int = 0, gems_change: int = 0,
                 player_name: str = None, player_rating: int = None):
    """Write one activity row. Call before session.commit()."""
    entry = ActivityLog(
        user_id=user_id,
        action=action,
        detail=detail,
        coins_change=coins_change,
        gems_change=gems_change,
        player_name=player_name,
        player_rating=player_rating,
    )
    session.add(entry)


# ── Daily "start" activity (for User Retention stats) ──────────────────────
# Every /start by an existing user should count toward "users who started or
# did any activity today". We only need one row per user per calendar day for
# the retention metric, so an in-memory throttle keyed by (user_id, UTC date)
# keeps the ActivityLog table from bloating when users spam /start.
_START_SEEN_MEM = {}
# Hard cap so the throttle cache can't grow unbounded over a long uptime.
_START_SEEN_MEM_MAX = 50000


def record_start(telegram_id: int):
    """Log a daily 'start' activity for the existing user with this Telegram id.

    New users who haven't run /debut yet have no ``User`` row (and the
    ``ActivityLog.user_id`` foreign key would reject them), so those are
    skipped — they get counted the moment they take a real action. Throttled
    to at most one row per user per IST day. Best-effort: any failure is
    swallowed so telemetry never blocks the welcome message.
    """
    if not telegram_id:
        return
    try:
        now = datetime.utcnow()
        today = (now + _IST_OFFSET).date()

        from database import get_session
        from models import User

        s = get_session()
        try:
            user = (s.query(User)
                    .filter(User.telegram_id == telegram_id)
                    .first())
            if not user:
                return  # not registered yet — nothing to attribute the start to

            # Throttle: one 'start' row per user per calendar day.
            seen = _START_SEEN_MEM.get(user.id)
            if seen == today:
                return
            if len(_START_SEEN_MEM) > _START_SEEN_MEM_MAX:
                _START_SEEN_MEM.clear()
            _START_SEEN_MEM[user.id] = today

            log_activity(s, user.id, "start", detail="Opened the bot")
            s.commit()
        finally:
            s.close()
    except Exception:
        logger.exception("record_start failed for telegram_id=%s", telegram_id)

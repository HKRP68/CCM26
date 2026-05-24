"""Notification service — FOMO-style push messages.

The runner (called by match_heartbeat tick) checks all active schedules:
  - Is current time within schedule's window?
  - Has enough time passed since last fire?
  - If yes: select target users, send to each, log delivery, update last_fired_at.

Time handling: schedules are configured in IST (UTC+5:30). The job evaluates
current IST time when checking firing conditions.

FOMO templates: built-in starter pack admins can quick-add.
"""

import logging
import random
from datetime import datetime, timedelta, timezone

from models import (
    NotificationSchedule, NotificationLog, User, UserStats, UserRoster,
)

logger = logging.getLogger(__name__)

# India Standard Time
IST = timezone(timedelta(hours=5, minutes=30))


# ════════════════════════════════════════════════════════════════════
# Built-in FOMO template starter pack
# Admin can quick-add these via "Add starter pack" button
# ════════════════════════════════════════════════════════════════════

STARTER_TEMPLATES = [
    {
        "name": "Daily reward expiring soon",
        "message": ("⏰ <b>{first_name}, your /daily reward is waiting!</b>\n\n"
                    "Don't break your login streak. Tap <b>/daily</b> now to claim "
                    "free coins + a player. Your streak resets at midnight UTC."),
        "target_filter": "inactive_24h",
        "fire_hour": 19, "fire_minute": 0,
        "window_start_hour": 17, "window_end_hour": 23,
    },
    {
        "name": "Player market refresh",
        "message": ("🛒 <b>The market just refreshed!</b>\n\n"
                    "5 new <b>87+ rated</b> players are available at <b>10% off</b> "
                    "for the next 24 hours. Tap <b>/playermarket</b> to grab them "
                    "before someone else does!"),
        "target_filter": "all",
        "fire_hour": 12, "fire_minute": 0,
        "window_start_hour": 10, "window_end_hour": 14,
    },
    {
        "name": "Free GSpin reminder",
        "message": ("🎡 <b>{first_name}, your free GSpin is ready!</b>\n\n"
                    "Free spins refresh every 6 hours. Could land coins, gems, "
                    "or even a rare card. Tap <b>/gspin</b> now."),
        "target_filter": "active",
        "fire_hour": 16, "fire_minute": 30,
        "window_start_hour": 13, "window_end_hour": 21,
    },
    {
        "name": "Win streak warning",
        "message": ("🔥 <b>Your {streak}-win streak is on the line!</b>\n\n"
                    "Don't lose momentum — challenge a bot or another player today "
                    "to keep it alive. Streaks unlock big rewards!\n\n"
                    "🤖 <b>/vsbot 5</b> — quick 5-over match"),
        "target_filter": "has_streak",
        "fire_hour": 20, "fire_minute": 30,
        "window_start_hour": 18, "window_end_hour": 23,
    },
    {
        "name": "Quest reminder evening",
        "message": ("🎯 <b>Your daily quests reset in a few hours!</b>\n\n"
                    "Check <b>/myquest</b> — you might have rewards ready to claim. "
                    "Daily quest progress is lost at midnight UTC if not completed."),
        "target_filter": "all",
        "fire_hour": 21, "fire_minute": 30,
        "window_start_hour": 20, "window_end_hour": 23,
    },
    {
        "name": "Long absence won't-be-forgotten",
        "message": ("👋 <b>We miss you, {first_name}!</b>\n\n"
                    "Your team is gathering dust 🪦. Come back — your /daily reward "
                    "is waiting, and we just shipped new features. Type <b>/start</b> "
                    "to jump back in."),
        "target_filter": "inactive_24h",
        "fire_hour": 18, "fire_minute": 0,
        "window_start_hour": 17, "window_end_hour": 22,
    },
]


# ════════════════════════════════════════════════════════════════════
# Time helpers
# ════════════════════════════════════════════════════════════════════

def now_ist():
    return datetime.now(IST)


def in_window(window_start_hour, window_end_hour):
    """True if current IST hour is within [start, end). Handles wraparound."""
    h = now_ist().hour
    if window_start_hour <= window_end_hour:
        return window_start_hour <= h < window_end_hour
    # Wraparound (e.g. 22 -> 6)
    return h >= window_start_hour or h < window_end_hour


def should_fire(schedule):
    """Check whether this schedule should fire right now."""
    if not schedule.is_active:
        return False

    # Window gate — but only if window is meaningful.
    # If start == end, treat it as "no window restriction" (always allowed).
    has_window = schedule.window_start_hour != schedule.window_end_hour
    if has_window and not in_window(schedule.window_start_hour, schedule.window_end_hour):
        return False

    now_local = now_ist()

    if schedule.schedule_type == "daily":
        target_h = schedule.fire_hour or 0
        target_m = schedule.fire_minute or 0
        # Has it fired today already (in IST date)?
        if schedule.last_fired_at:
            last_local = schedule.last_fired_at.replace(tzinfo=timezone.utc).astimezone(IST)
            if last_local.date() == now_local.date():
                return False  # already fired today
        # Are we past the fire time?
        return (now_local.hour, now_local.minute) >= (target_h, target_m)

    elif schedule.schedule_type == "interval":
        # Interval respects window if set, but the timing is "every N hours since
        # last fire", not tied to wall-clock time
        if not schedule.last_fired_at:
            return True
        delta = datetime.utcnow() - schedule.last_fired_at
        return delta >= timedelta(hours=schedule.interval_hours or 24)

    elif schedule.schedule_type == "one_off":
        # Fires once if not yet fired
        return schedule.last_fired_at is None

    return False


# ════════════════════════════════════════════════════════════════════
# User selection
# ════════════════════════════════════════════════════════════════════

def select_target_users(session, target_filter):
    """Return list of User objects matching the filter."""
    q = session.query(User).filter(User.telegram_id != -1)  # exclude bot user

    if target_filter == "all":
        return q.all()

    if target_filter == "active":
        # Played a match in last 7 days
        cutoff = datetime.utcnow() - timedelta(days=7)
        return q.filter(User.last_match_date.isnot(None),
                        User.last_match_date >= cutoff).all()

    if target_filter == "inactive_24h":
        # Has played at all, but not in last 24h
        cutoff = datetime.utcnow() - timedelta(hours=24)
        users = q.filter(User.last_match_date.isnot(None),
                         User.last_match_date < cutoff).all()
        # Also include users who never played (debut'd but no match)
        never_played = q.filter(User.last_match_date.is_(None),
                                User.created_at < cutoff).all()
        return users + never_played

    if target_filter == "low_coins":
        return q.filter(User.total_coins < 5000).all()

    if target_filter == "has_streak":
        return q.filter(User.win_streak >= 2).all()

    return q.all()


# ════════════════════════════════════════════════════════════════════
# Message rendering + sending
# ════════════════════════════════════════════════════════════════════

def render_message(template, user):
    """Substitute placeholders. Safe — no .format() to avoid format-spec injection."""
    out = template or ""
    out = out.replace("{first_name}", user.first_name or user.username or "Player")
    out = out.replace("{username}", user.username or "")
    out = out.replace("{coins}", f"{user.total_coins or 0:,}")
    out = out.replace("{gems}", str(user.total_gems or 0))
    out = out.replace("{streak}", str(user.win_streak or 0))
    out = out.replace("{best_streak}", str(user.best_streak or 0))
    out = out.replace("{matches_won}", str(user.matches_won or 0))
    out = out.replace("{quest_points}", str(user.quest_points or 0))
    return out


async def send_to_user(bot, user, message_text, schedule_id=None, session=None):
    """Send notification to one user. Returns (delivered, error_text)."""
    try:
        await bot.send_message(
            chat_id=user.telegram_id,
            text=message_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        if session is not None:
            session.add(NotificationLog(
                schedule_id=schedule_id, user_id=user.id,
                sent_at=datetime.utcnow(), delivered=True,
            ))
        return True, None
    except Exception as e:
        err = str(e)[:300]
        logger.warning(f"Failed to notify user {user.id}: {err}")
        if session is not None:
            session.add(NotificationLog(
                schedule_id=schedule_id, user_id=user.id,
                sent_at=datetime.utcnow(), delivered=False,
                error_text=err,
            ))
        return False, err


async def fire_schedule(bot, session, schedule):
    """Fire one schedule — select users, send, log, update last_fired_at.
    Returns (sent_count, failed_count).
    """
    users = select_target_users(session, schedule.target_filter or "all")
    if not users:
        # Still mark as fired so it doesn't keep retrying within the same window
        schedule.last_fired_at = datetime.utcnow()
        session.flush()
        return 0, 0

    sent_ok = 0
    sent_fail = 0
    for u in users:
        msg = render_message(schedule.message, u)
        ok, _ = await send_to_user(bot, u, msg, schedule.id, session)
        if ok:
            sent_ok += 1
        else:
            sent_fail += 1
    schedule.last_fired_at = datetime.utcnow()
    schedule.sent_count = (schedule.sent_count or 0) + sent_ok
    session.flush()
    return sent_ok, sent_fail


async def fire_one_off(bot, session, schedule_id, message_override=None,
                       target_override=None):
    """Send a one-time blast — used by the admin "Send Now" button.
    Doesn't update schedule.last_fired_at unless the schedule type is one_off.
    """
    schedule = session.query(NotificationSchedule).get(schedule_id)
    if not schedule:
        return 0, 0
    msg = message_override or schedule.message
    target = target_override or schedule.target_filter or "all"
    users = select_target_users(session, target)
    sent_ok = 0
    sent_fail = 0
    for u in users:
        rendered = render_message(msg, u)
        ok, _ = await send_to_user(bot, u, rendered, schedule_id, session)
        if ok: sent_ok += 1
        else: sent_fail += 1
    if schedule.schedule_type == "one_off":
        schedule.last_fired_at = datetime.utcnow()
    schedule.sent_count = (schedule.sent_count or 0) + sent_ok
    session.flush()
    return sent_ok, sent_fail


# ════════════════════════════════════════════════════════════════════
# Tick — called every minute by the heartbeat
# ════════════════════════════════════════════════════════════════════

# Track last tick time so we only run notification logic once per minute
_LAST_TICK_MINUTE = None


async def maybe_tick(application):
    """Check all active schedules; fire any that are due. Called from heartbeat.
    De-dupes to once per minute since heartbeat runs every 30s.
    """
    global _LAST_TICK_MINUTE
    now_min = datetime.utcnow().replace(second=0, microsecond=0)
    if _LAST_TICK_MINUTE == now_min:
        return
    _LAST_TICK_MINUTE = now_min

    try:
        from database import get_session
        session = get_session()
        try:
            schedules = (session.query(NotificationSchedule)
                         .filter(NotificationSchedule.is_active == True).all())
            for sched in schedules:
                if should_fire(sched):
                    sent, failed = await fire_schedule(application.bot, session, sched)
                    session.commit()
                    logger.info(f"Notification '{sched.name}' fired: {sent} sent, {failed} failed")
        finally:
            session.close()
    except Exception:
        logger.exception("notification tick failed")

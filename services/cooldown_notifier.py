"""Cooldown-ready notifications.

A background job scans users whose cooldowns (daily / gspin / claim / free pack)
have just become available and sends them a friendly Telegram nudge, e.g.
"🎁 Your Daily reward is ready! Use /daily".

To avoid spamming, each cooldown has a `notified_X_ready` flag on UserStats:
  - When cooldown becomes ready AND flag is False → send message, set flag True
  - When cooldown is NOT ready (user acted, or never used) → reset flag to False

This means each "ready" event notifies exactly once. The flag self-resets the
next time the user consumes the cooldown (which pushes last_X forward), so we
don't need to touch every action handler.

Notifications respect a quiet-hours window (default 23:00-07:00 IST) so we
don't wake people up.
"""

import logging
import os
from datetime import datetime, timedelta
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from services.miniapp_buttons import miniapp_button
from services.quota_service import get_quota_status

logger = logging.getLogger(__name__)

# Quiet hours (IST). No nudges sent during this window.
QUIET_START_HOUR = 23
QUIET_END_HOUR = 7
IST_OFFSET_HOURS = 5.5

# How many DMs to send per tick (avoid long-running jobs on free tier)
BATCH_SIZE = 50

# How many user rows to examine per tick. The scan used to pull the entire
# users table into memory on every run — at peak that is the process's biggest
# allocation and the job overran its own 5-minute interval. Now it walks the
# table in id order, a slice per tick, resuming where it left off.
SCAN_LIMIT = int(os.getenv("COOLDOWN_SCAN_LIMIT", "500"))

# Pace the DMs so a batch can't monopolise the bot's Telegram connection.
SENDS_PER_SECOND = float(os.getenv("COOLDOWN_SENDS_PER_SECOND", "10"))

# Rolling cursor into the users table, so consecutive ticks advance instead of
# rescanning the same prefix forever.
_SCAN_AFTER_ID = 0

# Cooldown definitions include both legacy last_* cooldowns and Mini App
# quota-backed actions. Quota-backed actions are considered ready whenever at
# least one free/ad slot is available in the same cycle used by the Mini App.


def _ist_now():
    return datetime.utcnow() + timedelta(hours=IST_OFFSET_HOURS)


def _in_quiet_hours():
    h = _ist_now().hour
    if QUIET_START_HOUR > QUIET_END_HOUR:
        # window crosses midnight, e.g. 23..7
        return h >= QUIET_START_HOUR or h < QUIET_END_HOUR
    return QUIET_START_HOUR <= h < QUIET_END_HOUR


def _get_cooldowns(session):
    """Return list of cooldown specs with resolved durations (seconds)."""
    from config import DAILY_COOLDOWN, GSPIN_COOLDOWN, CLAIM_COOLDOWN
    try:
        from services.command_config_service import get_cooldown
        daily_cd = get_cooldown(session, "daily", DAILY_COOLDOWN)
        gspin_cd = get_cooldown(session, "gspin", GSPIN_COOLDOWN)
        claim_cd = get_cooldown(session, "claim", CLAIM_COOLDOWN)
    except Exception:
        daily_cd, gspin_cd, claim_cd = DAILY_COOLDOWN, GSPIN_COOLDOWN, CLAIM_COOLDOWN

    try:
        from services.free_pack_service import get_cooldown_minutes
        free_pack_cd = get_cooldown_minutes(session) * 60
    except Exception:
        free_pack_cd = 3600

    return [
        {
            "field": "last_daily", "flag": "notified_daily_ready",
            "cooldown": daily_cd, "quota_kind": "daily", "miniapp_tab": "daily",
            "button_label": "📅 Open Daily",
            "message": "📅 Your daily reward is ready! Open the Mini App to claim it.",
            "short": "📅 Daily reward", "command": "/daily",
        },
        {
            "field": "last_gspin", "flag": "notified_gspin_ready",
            "cooldown": gspin_cd, "quota_kind": "spin", "miniapp_tab": "spin",
            "button_label": "🎴 Open Lucky Card Pick",
            "message": "🎴 Your Lucky Card Pick is ready! Open the Mini App to pick a card.",
            "short": "🎴 Lucky Card Pick", "command": "/gspin",
        },
        {
            "field": "last_claim", "flag": "notified_claim_ready",
            "cooldown": claim_cd, "tiered": True,
            "message": "⏰ Your hourly Claim is ready! Use /claim for a free player + coins.",
            "short": "⏰ Hourly claim", "command": "/claim",
        },
        {
            "field": "last_free_pack", "flag": "notified_free_pack_ready",
            "cooldown": free_pack_cd,
            "message": "📦 Your Free Pack is ready! Open the app to watch an ad and claim it.",
            "short": "📦 Free Pack", "command": "the Mini App", "miniapp_tab": "freepack",
            "button_label": "📦 Open Free Pack",
        },
    ]


def _is_ready(cd, user, stats, session, now=None):
    """True when the cooldown ``cd`` can be used by ``user`` right now."""
    now = now or datetime.utcnow()
    quota_kind = cd.get("quota_kind")
    if quota_kind:
        try:
            quota = get_quota_status(stats, quota_kind, session=session, user=user)
            return not quota["all_used"]
        except Exception:
            return False
    last = getattr(stats, cd["field"], None)
    if last is None:
        # Never used: a legacy cooldown is available from the start.
        return True
    cd_seconds = cd["cooldown"]
    if cd.get("tiered"):
        try:
            from services.subscription_service import cooldown_seconds as _tier_cd
            cd_seconds = _tier_cd(user, cd_seconds)
        except Exception:
            pass
    return (now - last).total_seconds() >= cd_seconds


def ready_now(session, user, stats):
    """``(label, command)`` pairs for every reward ``user`` can collect now.

    Shared with the /start status card, so "ready" means the same thing in the
    DM nudge and on the welcome screen.
    """
    if stats is None:
        return []
    out = []
    now = datetime.utcnow()
    for cd in _get_cooldowns(session):
        try:
            if _is_ready(cd, user, stats, session, now):
                out.append((cd["short"], cd["command"]))
        except Exception:
            logger.debug("ready_now check failed for %s", cd.get("field"))
    return out


def _streak_at_risk(stats):
    """The login streak a user loses at UTC midnight unless they open the bot.

    Returns the streak length when it is 2+ days and today's login has not
    happened yet (last login was yesterday), else 0.
    """
    try:
        from services.login_streak_service import _yesterday
        streak = int(getattr(stats, "login_streak", 0) or 0)
        if streak >= 2 and getattr(stats, "last_login_date", None) == _yesterday():
            return streak
    except Exception:
        pass
    return 0


def _combined_message(cds, streak=0):
    """One DM for everything that became ready in this tick."""
    if len(cds) == 1:
        text = cds[0]["message"]
    else:
        lines = ["🎁 <b>Rewards ready for you!</b>", ""]
        for cd in cds:
            lines.append(f"• {cd['short']} — {cd['command']}")
        text = "\n".join(lines)
    if streak:
        hours_left = 24 - datetime.utcnow().hour
        text += (f"\n\n🔥 Your <b>{streak}-day login streak</b> resets in "
                 f"~{hours_left}h. Open the Mini App to keep it.")
    return text


def _mark_dm_blocked(user_ids):
    """Remember users Telegram refuses to deliver to (worker thread)."""
    if not user_ids:
        return
    from database import get_session
    from models import User
    session = get_session()
    try:
        (session.query(User).filter(User.id.in_(list(user_ids)))
         .update({User.dm_blocked: True}, synchronize_session=False))
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("dm_blocked update failed")
    finally:
        session.close()


async def _send_dm(application, telegram_id, text, reply_markup=None):
    """Send a DM. Returns True, False, or "blocked" when the user blocked the bot."""
    try:
        await application.bot.send_message(
            chat_id=telegram_id, text=text, parse_mode="HTML",
            reply_markup=reply_markup, disable_web_page_preview=True)
        return True
    except Exception as e:
        # Common: bot blocked, chat not found — don't spam logs
        msg = str(e).lower()
        if "blocked" in msg or "deactivated" in msg:
            return "blocked"
        if "not found" not in msg:
            logger.debug(f"Cooldown notify send failed for {telegram_id}: {e}")
        return False


def _scan_slice(after_id, quiet):
    """Examine one slice of users; clear stale flags; return the DMs to send.

    Pure database work — runs in a worker thread so it never blocks the bot's
    event loop. Returns (pending, next_after_id, wrapped) where ``pending`` is
    the list of nudges still to be delivered and ``wrapped`` means the scan
    reached the end of the table, so the next tick should start from the top.
    """
    from database import get_session
    from models import User, UserStats

    session = get_session()
    pending = []
    last_id = after_id
    try:
        cooldowns = _get_cooldowns(session)
        now = datetime.utcnow()

        # Users who have a stats row + are not banned, walked in id order so
        # consecutive ticks make progress. Skip users who opted out via
        # /notifications (NULL = still enabled).
        rows = (session.query(User, UserStats)
                .join(UserStats, UserStats.user_id == User.id)
                .filter(User.is_banned == False)
                .filter(User.notifications_enabled.isnot(False))
                .filter(User.dm_blocked.isnot(True))
                .filter(User.id > after_id)
                .order_by(User.id)
                .limit(SCAN_LIMIT)
                .all())

        for user, stats in rows:
            if len(pending) >= BATCH_SIZE:
                # Batch is full. Stop here and leave the cursor on the last
                # user we finished, so the next tick resumes at this point
                # instead of skipping everyone we didn't get to.
                break
            last_id = user.id
            if not user.telegram_id:
                continue
            for cd in cooldowns:
                last = getattr(stats, cd["field"], None)
                flag_set = getattr(stats, cd["flag"], False)
                quota_kind = cd.get("quota_kind")
                ready = _is_ready(cd, user, stats, session, now)

                if last is None and not quota_kind:
                    # Never used this legacy feature — keep flag clear.
                    if flag_set:
                        setattr(stats, cd["flag"], False)
                    continue

                if ready and not flag_set:
                    # Newly ready → queue a nudge (unless quiet hours).
                    # If quiet, leave the flag False so we notify once quiet ends.
                    if not quiet:
                        pending.append({
                            "telegram_id": user.telegram_id,
                            "user_id": user.id,
                            "cd": cd,
                            "streak": _streak_at_risk(stats),
                        })
                elif not ready and flag_set:
                    # User acted (cooldown/quota reset) → clear flag so next ready notifies
                    setattr(stats, cd["flag"], False)

        # Fewer rows than we asked for means we reached the end of the table.
        wrapped = len(rows) < SCAN_LIMIT and len(pending) < BATCH_SIZE
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("cooldown notification scan failed")
        return [], after_id, False
    finally:
        session.close()
    return pending, last_id, wrapped


def _mark_notified(marks):
    """Set the notified_* flags for DMs that actually went out (worker thread)."""
    from database import get_session
    from models import UserStats

    if not marks:
        return
    session = get_session()
    try:
        by_user = {}
        for user_id, flag in marks:
            by_user.setdefault(user_id, []).append(flag)
        rows = (session.query(UserStats)
                .filter(UserStats.user_id.in_(list(by_user)))
                .all())
        for row in rows:
            for flag in by_user.get(row.user_id, ()):
                setattr(row, flag, True)
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("cooldown notification flag update failed")
    finally:
        session.close()


async def run_cooldown_notifications(application):
    """One tick: scan a slice of users and notify on newly-ready cooldowns.

    Designed to be called by job_queue.run_repeating. The database work happens
    on worker threads; only the Telegram sends run on the event loop, paced so
    a batch of nudges can't stall the commands users are typing.
    """
    import asyncio
    global _SCAN_AFTER_ID

    # Quiet hours — skip sending (but we still reset flags below so users get
    # notified once quiet hours end, not repeatedly)
    quiet = _in_quiet_hours()

    pending, last_id, wrapped = await asyncio.to_thread(
        _scan_slice, _SCAN_AFTER_ID, quiet)

    # Walked off the end of the table → start over from the top next tick.
    _SCAN_AFTER_ID = 0 if wrapped else last_id

    if not pending:
        return

    # Group by user: one DM listing everything that became ready, rather than
    # up to four separate pings in the same minute.
    by_user = {}
    for item in pending:
        entry = by_user.setdefault(item["user_id"], {
            "telegram_id": item["telegram_id"], "cds": [],
            "streak": item.get("streak", 0)})
        entry["cds"].append(item["cd"])

    marks = []
    blocked = []
    interval = 1.0 / max(SENDS_PER_SECOND, 1)
    for user_id, entry in by_user.items():
        cds = entry["cds"]
        kb_rows = []
        buttons = []
        for cd in cds:
            if cd.get("miniapp_tab"):
                btn = miniapp_button(cd["button_label"], cd["miniapp_tab"],
                                     is_private=True)
                if btn is not None:
                    buttons.append(btn)
        for i in range(0, len(buttons), 2):
            kb_rows.append(buttons[i:i + 2])
        # Always offer a one-tap opt-out so users can silence these reminders
        # straight from the message.
        kb_rows.append([InlineKeyboardButton(
            "🔕 Turn off notifications", callback_data="notif_toggle:off")])

        ok = await _send_dm(application, entry["telegram_id"],
                            _combined_message(cds, entry["streak"]),
                            InlineKeyboardMarkup(kb_rows))
        if ok is True:
            marks.extend((user_id, cd["flag"]) for cd in cds)
        elif ok == "blocked":
            blocked.append(user_id)
        await asyncio.sleep(interval)

    if blocked:
        await asyncio.to_thread(_mark_dm_blocked, blocked)
    await asyncio.to_thread(_mark_notified, marks)
    if marks:
        logger.info(f"Cooldown notifications: sent {len(marks)}")

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
        },
        {
            "field": "last_gspin", "flag": "notified_gspin_ready",
            "cooldown": gspin_cd, "quota_kind": "spin", "miniapp_tab": "spin",
            "button_label": "🎴 Open Lucky Card Pick",
            "message": "🎴 Your Lucky Card Pick is ready! Open the Mini App to pick a card.",
        },
        {
            "field": "last_claim", "flag": "notified_claim_ready",
            "cooldown": claim_cd, "tiered": True,
            "message": "⏰ Your hourly Claim is ready! Use /claim for a free player + coins.",
        },
        {
            "field": "last_free_pack", "flag": "notified_free_pack_ready",
            "cooldown": free_pack_cd,
            "message": "📦 Your Free Pack is ready! Open the app to watch an ad and claim it.",
        },
    ]


async def _send_dm(application, telegram_id, text, reply_markup=None):
    """Send a DM; swallow errors (user may have blocked the bot)."""
    try:
        await application.bot.send_message(
            chat_id=telegram_id, text=text, parse_mode="HTML",
            reply_markup=reply_markup, disable_web_page_preview=True)
        return True
    except Exception as e:
        # Common: bot blocked, chat not found — don't spam logs
        msg = str(e).lower()
        if "blocked" not in msg and "not found" not in msg and "deactivated" not in msg:
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

                ready = False
                if quota_kind:
                    try:
                        quota = get_quota_status(stats, quota_kind, session=session, user=user)
                        ready = not quota["all_used"]
                    except Exception:
                        ready = False
                elif last is not None:
                    elapsed = (now - last).total_seconds()
                    cd_seconds = cd["cooldown"]
                    if cd.get("tiered"):
                        try:
                            from services.subscription_service import cooldown_seconds as _tier_cd
                            cd_seconds = _tier_cd(user, cd_seconds)
                        except Exception:
                            pass
                    ready = elapsed >= cd_seconds

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

    marks = []
    interval = 1.0 / max(SENDS_PER_SECOND, 1)
    for item in pending:
        cd = item["cd"]
        kb_rows = []
        if cd.get("miniapp_tab"):
            btn = miniapp_button(cd["button_label"], cd["miniapp_tab"],
                                 is_private=True)
            if btn is not None:
                kb_rows.append([btn])
        # Always offer a one-tap opt-out so users can silence these reminders
        # straight from the message.
        kb_rows.append([InlineKeyboardButton(
            "🔕 Turn off notifications", callback_data="notif_toggle:off")])

        ok = await _send_dm(application, item["telegram_id"], cd["message"],
                            InlineKeyboardMarkup(kb_rows))
        if ok:
            marks.append((item["user_id"], cd["flag"]))
        await asyncio.sleep(interval)

    await asyncio.to_thread(_mark_notified, marks)
    if marks:
        logger.info(f"Cooldown notifications: sent {len(marks)}")

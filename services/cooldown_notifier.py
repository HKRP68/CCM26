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
from datetime import datetime, timedelta
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from services.miniapp_buttons import miniapp_button
from services.quota_service import get_quota_status

logger = logging.getLogger(__name__)

# Quiet hours (IST). No nudges sent during this window.
QUIET_START_HOUR = 23
QUIET_END_HOUR = 7
IST_OFFSET_HOURS = 5.5

# How many users to process per tick (avoid long-running jobs on free tier)
BATCH_SIZE = 50

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
            "button_label": "🎡 Open Spin",
            "message": "🎡 Your spin is ready! Open the Mini App to spin the wheel.",
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


async def run_cooldown_notifications(application):
    """One tick: scan a batch of users and notify on newly-ready cooldowns.

    Designed to be called by job_queue.run_repeating.
    """
    from database import get_session
    from models import User, UserStats

    # Quiet hours — skip sending (but we still reset flags below so users get
    # notified once quiet hours end, not repeatedly)
    quiet = _in_quiet_hours()

    session = get_session()
    sent_count = 0
    try:
        cooldowns = _get_cooldowns(session)
        now = datetime.utcnow()

        # Iterate users who have a stats row + are not banned.
        # We process in batches keyed by user id to spread load.
        # Pull users with at least one cooldown field set (i.e. they've played).
        # Skip users who opted out via /notifications (NULL = still enabled).
        rows = (session.query(User, UserStats)
                .join(UserStats, UserStats.user_id == User.id)
                .filter(User.is_banned == False)
                .filter(User.notifications_enabled.isnot(False))
                .all())

        for user, stats in rows:
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
                    # Newly ready → notify (unless quiet hours)
                    if not quiet:
                        kb_rows = []
                        if cd.get("miniapp_tab"):
                            btn = miniapp_button(
                                cd["button_label"], cd["miniapp_tab"],
                                is_private=True)
                            if btn is not None:
                                kb_rows.append([btn])
                        # Always offer a one-tap opt-out so users can silence
                        # these reminders straight from the message.
                        kb_rows.append([InlineKeyboardButton(
                            "🔕 Turn off notifications",
                            callback_data="notif_toggle:off")])
                        reply_markup = InlineKeyboardMarkup(kb_rows)
                        ok = await _send_dm(application, user.telegram_id,
                                            cd["message"], reply_markup)
                        if ok:
                            setattr(stats, cd["flag"], True)
                            sent_count += 1
                            if sent_count >= BATCH_SIZE:
                                # Cap per tick; remaining users get caught next tick
                                session.commit()
                                logger.info(f"Cooldown notifications: sent {sent_count} (batch cap)")
                                return
                    # If quiet, leave flag False so we notify once quiet ends
                elif not ready and flag_set:
                    # User acted (cooldown/quota reset) → clear flag so next ready notifies
                    setattr(stats, cd["flag"], False)

        session.commit()
        if sent_count:
            logger.info(f"Cooldown notifications: sent {sent_count}")
    except Exception:
        session.rollback()
        logger.exception("run_cooldown_notifications failed")
    finally:
        session.close()

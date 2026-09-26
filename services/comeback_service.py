"""Comeback nudges: win back players who have gone quiet.

An hourly job looks for players who have not touched the bot for 1, 3, 7 or
14 days and DMs each of them once per tier with a reward waiting to be
claimed (a button, so the reward is a reason to open the bot rather than a
silent credit). Returning at any point resets the ladder, so the next absence
starts at day 1 again.

"Last active" is the latest of ``User.last_seen_at`` (stamped by a throttled
middleware in bot.py on any update), ``last_match_date`` and ``created_at``.

Respects the /notifications opt-out, skips banned and ``dm_blocked`` users,
honours the cooldown notifier's quiet hours, and never contacts anyone
inactive for longer than :data:`MAX_INACTIVE_DAYS`. At that point the player
is gone, and a DM is more likely to be reported as spam than to bring them
back.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# days inactive → reward. Admin-overridable via GameConfig.comeback_rewards_json.
DEFAULT_TIERS = {
    1: {"coins": 300, "gems": 0},
    3: {"coins": 750, "gems": 5},
    7: {"coins": 1500, "gems": 10},
    14: {"coins": 3000, "gems": 25},
}
MAX_INACTIVE_DAYS = int(os.getenv("COMEBACK_MAX_INACTIVE_DAYS", "30"))
BATCH_SIZE = int(os.getenv("COMEBACK_BATCH_SIZE", "100"))
# Upper bound on candidate pages read per tick (each page is BATCH_SIZE * 5).
MAX_SCAN_PAGES = int(os.getenv("COMEBACK_MAX_SCAN_PAGES", "20"))
SENDS_PER_SECOND = float(os.getenv("COMEBACK_SENDS_PER_SECOND", "8"))
# Test hook: "minutes" makes a tier of N mean N minutes instead of N days.
_UNIT = timedelta(minutes=1) if os.getenv("COMEBACK_TIER_UNIT") == "minutes" \
    else timedelta(days=1)


# ── Config ──────────────────────────────────────────────────────────

def _cfg(cfg=None):
    if cfg is not None:
        return cfg
    try:
        from services.config_service import get_config
        return get_config()
    except Exception:
        return {}


def is_enabled(cfg=None) -> bool:
    return bool((_cfg(cfg) or {}).get("comeback_enabled", True))


def tiers(cfg=None) -> dict:
    """``{days: {"coins", "gems"}}`` sorted by days. Bad JSON → defaults."""
    raw = (_cfg(cfg) or {}).get("comeback_rewards_json")
    if raw:
        try:
            data = json.loads(raw)
            out = {}
            for k, v in data.items():
                days = int(k)
                if days <= 0 or not isinstance(v, dict):
                    continue
                if v.get("enabled") is False:
                    continue
                out[days] = {"coins": max(0, int(v.get("coins", 0) or 0)),
                             "gems": max(0, int(v.get("gems", 0) or 0))}
            if out:
                return dict(sorted(out.items()))
        except Exception:
            logger.warning("comeback_rewards_json is invalid — using defaults")
    return dict(DEFAULT_TIERS)


# ── Pure rules ──────────────────────────────────────────────────────

def last_active(user):
    """The most recent sign of life for ``user`` (a datetime or None)."""
    stamps = [getattr(user, f, None) for f in
              ("last_seen_at", "last_match_date", "created_at")]
    stamps = [t for t in stamps if t is not None]
    return max(stamps) if stamps else None


def due_tier(user, now=None, tier_table=None):
    """The tier (days) to DM ``user`` about now, or None.

    * back since the last nudge → nothing due (the scan resets the ladder);
    * the highest tier crossed that is above the one already sent;
    * never within 36 hours of the previous nudge;
    * nothing past MAX_INACTIVE_DAYS.
    """
    now = now or datetime.utcnow()
    tier_table = tier_table or DEFAULT_TIERS
    seen = last_active(user)
    if seen is None:
        return None
    idle = now - seen
    if idle > MAX_INACTIVE_DAYS * _UNIT:
        return None
    # Never two nudges within 36 hours, whatever the ladder says: a player
    # who drops in every other day should not get a DM every other day.
    sent_at = getattr(user, "comeback_sent_at", None)
    if sent_at is not None and now - sent_at < 1.5 * _UNIT:
        return None
    sent = int(getattr(user, "comeback_tier", 0) or 0)
    crossed = [d for d in tier_table if idle >= d * _UNIT]
    if not crossed:
        return None
    best = max(crossed)
    return best if best > sent else None


def should_reset(user):
    """True when the user came back after a nudge, so the ladder restarts."""
    sent_at = getattr(user, "comeback_sent_at", None)
    if not sent_at or not int(getattr(user, "comeback_tier", 0) or 0):
        return False
    seen = last_active(user)
    return seen is not None and seen > sent_at


# ── Database side ───────────────────────────────────────────────────

def touch_last_seen(telegram_id):
    """Stamp last_seen_at (and clear dm_blocked). Worker thread, best-effort."""
    try:
        from database import get_session
        from models import User
        s = get_session()
        try:
            (s.query(User).filter(User.telegram_id == telegram_id)
             .update({User.last_seen_at: datetime.utcnow(),
                      User.dm_blocked: False}, synchronize_session=False))
            s.commit()
        finally:
            s.close()
    except Exception:
        logger.debug("touch_last_seen failed for %s", telegram_id, exc_info=True)


def _scan(now=None):
    """Reset returned users and pick who is due. Returns send jobs."""
    from database import get_session
    from models import User
    now = now or datetime.utcnow()
    table = tiers()
    horizon = now - MAX_INACTIVE_DAYS * _UNIT - timedelta(days=1)
    first_tier = min(table) if table else 1
    newest = now - first_tier * _UNIT
    s = get_session()
    jobs = []
    try:
        # Everyone who came back after a nudge: restart their ladder.
        returned = (s.query(User)
                    .filter(User.comeback_tier > 0,
                            User.comeback_sent_at.isnot(None),
                            User.last_seen_at > User.comeback_sent_at)
                    .limit(2000).all())
        for u in returned:
            u.comeback_tier = 0
        # Candidates: quiet for at least the first tier, not beyond the horizon.
        q = (s.query(User)
             .filter(User.telegram_id > 0,
                     User.is_banned == False,  # noqa: E712
                     User.notifications_enabled.isnot(False),
                     User.dm_blocked.isnot(True))
             .filter(((User.last_seen_at.is_(None)) | (User.last_seen_at < newest)))
             .filter(((User.last_match_date.is_(None)) | (User.last_match_date < newest)))
             .filter(User.created_at < newest))
        q = q.filter((User.last_seen_at > horizon) | (User.last_match_date > horizon)
                     | (User.created_at > horizon))
        # The 36-hour gap in due_tier, done in SQL so recently nudged users
        # never take up a page.
        gap = now - 1.5 * _UNIT
        q = q.filter((User.comeback_sent_at.is_(None)) | (User.comeback_sent_at < gap))
        # Page by id until the batch is full: users who are quiet but not due
        # yet (their tier already sent) must not starve higher-id users.
        page = BATCH_SIZE * 5
        cursor = 0
        for _ in range(MAX_SCAN_PAGES):
            rows = q.filter(User.id > cursor).order_by(User.id).limit(page).all()
            for u in rows:
                cursor = u.id
                tier = due_tier(u, now, table)
                if tier is None:
                    continue
                jobs.append({"user_id": u.id, "telegram_id": u.telegram_id,
                             "tier": tier, "reward": table[tier],
                             "name": u.first_name or u.username or "",
                             "streak": u.win_streak or 0})
                if len(jobs) >= BATCH_SIZE:
                    break
            if len(jobs) >= BATCH_SIZE or len(rows) < page:
                break
        s.commit()
    except Exception:
        s.rollback()
        logger.exception("comeback scan failed")
        return []
    finally:
        s.close()
    return jobs


def _record_sent(results, now=None):
    """Persist which tiers went out (and who has blocked the bot)."""
    from database import get_session
    from models import User
    now = now or datetime.utcnow()
    s = get_session()
    try:
        for user_id, tier, outcome in results:
            u = s.get(User, user_id)
            if u is None:
                continue
            if outcome == "sent":
                u.comeback_tier = tier
                u.comeback_sent_at = now
                u.comeback_claim_tier = tier
            elif outcome == "blocked":
                u.dm_blocked = True
        s.commit()
    except Exception:
        s.rollback()
        logger.exception("comeback record failed")
    finally:
        s.close()


def claim(session, user, tier):
    """Pay the waiting comeback reward once. Returns the reward dict or None."""
    try:
        tier = int(tier)
    except (TypeError, ValueError):
        return None
    if getattr(user, "comeback_claim_tier", None) != tier:
        return None
    reward = tiers().get(tier) or DEFAULT_TIERS.get(tier)
    if not reward:
        user.comeback_claim_tier = None
        return None
    # One conditional UPDATE: a double tap (or two devices) can pass the check
    # above together, but only one of them matches the WHERE and pays.
    from sqlalchemy import func
    from models import User
    won = (session.query(User)
           .filter(User.id == user.id, User.comeback_claim_tier == tier)
           .update({User.total_coins: func.coalesce(User.total_coins, 0) + reward["coins"],
                    User.total_gems: func.coalesce(User.total_gems, 0) + reward["gems"],
                    User.comeback_claim_tier: None,
                    User.last_seen_at: datetime.utcnow()},
                   synchronize_session=False))
    if won != 1:
        return None
    session.expire(user)
    try:
        from services.activity_service import log_activity
        log_activity(session, user.id, "comeback",
                     f"Comeback reward (day {tier})",
                     coins_change=reward["coins"], gems_change=reward["gems"])
    except Exception:
        logger.exception("comeback activity log failed (non-fatal)")
    return reward


# ── Message ─────────────────────────────────────────────────────────

def _teaser():
    """One line about what is happening right now, if anything."""
    try:
        from database import get_session
        from models import Tournament
        s = get_session()
        try:
            live = (s.query(Tournament)
                    .filter(Tournament.is_active == True)  # noqa: E712
                    .first())
            if live is not None:
                import html as _h
                return f"🏆 <b>{_h.escape(live.name or '')}</b> is live right now."
        finally:
            s.close()
    except Exception:
        pass
    return "🏆 The season leaderboard is still up for grabs this month."


def message_for(job):
    from services.onboarding_service import reward_label
    tier = job["tier"]
    name = job.get("name") or ""
    hello = f"Hey {name}!" if name else "Hey!"
    reward = reward_label(job["reward"]["coins"], job["reward"]["gems"])
    unit = "day" if tier == 1 else "days"
    lead = {
        1: "Your squad missed you today.",
        3: "Your squad has been sitting in the dressing room for a while.",
        7: "It's been a week. Your XI is getting rusty!",
        14: "Two weeks away! Come back and pick up where you left off.",
    }.get(tier, "We haven't seen you in a while.")
    import html as _h
    return (
        f"🏏 <b>{_h.escape(hello)}</b> {lead}\n\n"
        f"🎁 A <b>{tier}-{unit} comeback gift</b> is waiting: <b>{reward}</b>.\n"
        f"{_teaser()}\n\n"
        "Tap below to claim it, then /daily and /claim are probably ready too."
    )


def keyboard_for(job):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from services.onboarding_service import reward_label
    reward = reward_label(job["reward"]["coins"], job["reward"]["gems"])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🎁 Claim {reward}",
                              callback_data=f"comeback_claim_{job['tier']}")],
        [InlineKeyboardButton("🔕 Turn off notifications",
                              callback_data="notif_toggle:off")],
    ])


async def run_comeback_tick(application):
    """One job tick: scan, send, record. Quiet hours skip the whole tick."""
    import asyncio
    if not is_enabled():
        return 0
    try:
        from services.cooldown_notifier import _in_quiet_hours
        if _in_quiet_hours():
            return 0
    except Exception:
        pass
    jobs = await asyncio.to_thread(_scan)
    if not jobs:
        return 0
    results = []
    interval = 1.0 / max(SENDS_PER_SECOND, 1)
    for job in jobs:
        outcome = "failed"
        try:
            await application.bot.send_message(
                chat_id=job["telegram_id"], text=message_for(job),
                parse_mode="HTML", reply_markup=keyboard_for(job),
                disable_web_page_preview=True)
            outcome = "sent"
        except Exception as exc:
            text = str(exc).lower()
            if "blocked" in text or "deactivated" in text:
                outcome = "blocked"
            else:
                logger.debug("comeback DM to %s failed: %s", job["telegram_id"], exc)
        results.append((job["user_id"], job["tier"], outcome))
        await asyncio.sleep(interval)
    await asyncio.to_thread(_record_sent, results)
    sent = sum(1 for r in results if r[2] == "sent")
    if sent:
        logger.info("Comeback nudges: sent %d", sent)
    return sent

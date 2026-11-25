"""Community XP (CXP) ledger, levels and period maths.

CXP is a wholly separate currency from coins, gems, quest points, ranked rating
and season points. Every credit flows through :func:`award_xp`, which is:

* **idempotent** — a stable ``event_key`` is written to
  :class:`models.CommunityXPLedger` under a unique constraint, so a replayed
  update (double tap, retried handler, re-delivered Telegram update) credits
  nothing the second time;
* **capped** — a per-activity daily limit and an overall daily CXP ceiling are
  enforced inside the awarding transaction against
  :class:`models.CommunityDailyCounter`, and the amount *actually* credited is
  returned so callers can report it honestly;
* **isolated** — by default it runs in its own session/transaction, so a
  Community Arena failure can never roll back a match or a claim.

All day/week/month boundaries use Asia/Kolkata (IST); stored timestamps stay
UTC.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from time import monotonic

logger = logging.getLogger(__name__)

IST_OFFSET = timedelta(hours=5, minutes=30)

# Ledger activity keys
ACT_CHAT = "chat"
ACT_PVP = "pvp_match"
ACT_AI = "ai_match"
ACT_CLAIM = "claim"
ACT_CHECKIN = "checkin"
ACT_ADMIN = "admin_adjust"

# Per-game ledger activities
GAME_ACTIVITY = {
    "mystery": "game_mystery",
    "oddoneout": "game_oddoneout",
    "wordle": "game_wordle",
    "fastestfingers": "game_fastestfingers",
    "rating": "game_rating",
    "cricketer": "game_cricketer",
}


# ── IST time helpers ────────────────────────────────────────────────────

def ist_now(now=None) -> datetime:
    return (now or datetime.utcnow()) + IST_OFFSET


def ist_today_str(now=None) -> str:
    return ist_now(now).strftime("%Y-%m-%d")


def week_start_utc(now=None) -> datetime:
    """UTC instant of Monday 00:00 IST for the IST week containing ``now``."""
    i = ist_now(now)
    monday = (i - timedelta(days=i.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return monday - IST_OFFSET


def month_start_utc(now=None) -> datetime:
    """UTC instant of the 1st 00:00 IST for the IST month containing ``now``."""
    i = ist_now(now)
    first = i.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return first - IST_OFFSET


def day_start_utc(now=None) -> datetime:
    i = ist_now(now)
    midnight = i.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - IST_OFFSET


# ── Level ladder (cached) ────────────────────────────────────────────────

_LADDER_CACHE = {"data": None, "loaded_at": 0.0}
_LADDER_TTL = 30.0


def _ladder(session):
    if (_LADDER_CACHE["data"] is not None
            and (monotonic() - _LADDER_CACHE["loaded_at"]) < _LADDER_TTL):
        return _LADDER_CACHE["data"]
    from models import CommunityLevel
    rows = (session.query(CommunityLevel)
            .order_by(CommunityLevel.required_xp.asc(), CommunityLevel.level.asc())
            .all())
    data = [(r.level, r.title, int(r.required_xp)) for r in rows]
    if not data:
        data = [(1, "Newcomer", 0)]
    _LADDER_CACHE["data"] = data
    _LADDER_CACHE["loaded_at"] = monotonic()
    return data


def invalidate_ladder():
    _LADDER_CACHE["data"] = None
    _LADDER_CACHE["loaded_at"] = 0.0


def level_for_xp(session, xp: int):
    """Return (level, title) for a lifetime XP total."""
    xp = max(0, int(xp or 0))
    best = (1, "Newcomer")
    for level, title, req in _ladder(session):
        if xp >= req:
            best = (level, title)
        else:
            break
    return best


def level_title(session, level: int) -> str:
    for lvl, title, _req in _ladder(session):
        if lvl == level:
            return title
    return "Newcomer"


def level_progress(session, xp: int) -> dict:
    """Progress info toward the next level for a lifetime XP total."""
    xp = max(0, int(xp or 0))
    ladder = _ladder(session)
    level, title = level_for_xp(session, xp)
    cur_req = 0
    nxt = None
    for lvl, _t, req in ladder:
        if lvl == level:
            cur_req = req
        if lvl == level + 1:
            nxt = (lvl, _t, req)
    if nxt is None:
        return {"level": level, "title": title, "next_level": None,
                "next_title": None, "remaining": 0, "pct": 100.0,
                "span_from": cur_req, "span_to": cur_req, "xp": xp}
    span = max(1, nxt[2] - cur_req)
    done = max(0, xp - cur_req)
    pct = max(0.0, min(100.0, 100.0 * done / span))
    return {"level": level, "title": title, "next_level": nxt[0],
            "next_title": nxt[1], "remaining": max(0, nxt[2] - xp),
            "pct": pct, "span_from": cur_req, "span_to": nxt[2], "xp": xp}


# ── Profile ──────────────────────────────────────────────────────────────

def get_or_create_profile(session, user_id: int):
    from models import CommunityProfile
    prof = (session.query(CommunityProfile)
            .filter(CommunityProfile.user_id == user_id).first())
    if prof is None:
        prof = CommunityProfile(user_id=user_id, lifetime_xp=0, level=1)
        session.add(prof)
        session.flush()
    return prof


def get_profile(session, user_id: int):
    from models import CommunityProfile
    return (session.query(CommunityProfile)
            .filter(CommunityProfile.user_id == user_id).first())


# ── Core award ─────────────────────────────────────────────────────────

def _eligible_user(session, user_id: int):
    """Return the User row if registered and not banned, else None."""
    from models import User
    return (session.query(User)
            .filter(User.id == user_id, User.telegram_id > 0,
                    User.is_banned.is_(False))
            .with_for_update().first())


def _day_counter(session, user_id, day, activity, *, lock=False):
    from models import CommunityDailyCounter
    q = (session.query(CommunityDailyCounter)
         .filter(CommunityDailyCounter.user_id == user_id,
                 CommunityDailyCounter.day == day,
                 CommunityDailyCounter.activity == activity))
    if lock:
        q = q.with_for_update()
    return q.first()


def _day_total_xp(session, user_id, day) -> int:
    from sqlalchemy import func
    from models import CommunityDailyCounter
    total = (session.query(func.coalesce(func.sum(CommunityDailyCounter.xp), 0))
             .filter(CommunityDailyCounter.user_id == user_id,
                     CommunityDailyCounter.day == day).scalar())
    return int(total or 0)


def award_xp(user_id: int, activity: str, base_xp: int, event_key: str, *,
             chat_id=None, detail=None, now=None, session=None,
             activity_daily_limit=None, bypass_limits=False):
    """Credit ``base_xp`` CXP for ``activity`` once, subject to caps.

    Returns a dict:
      ``{"credited", "requested", "duplicate", "capped", "lifetime_xp",
         "level", "old_level", "leveled_up", "title", "reason"}``

    ``credited`` is the XP actually added (0 when blocked). Opens its own
    session unless one is passed (tests / an explicit transaction).
    """
    own = False
    if session is None:
        from database import get_session
        session = get_session()
        own = True

    result = {"credited": 0, "requested": int(base_xp or 0), "duplicate": False,
              "capped": False, "lifetime_xp": None, "level": None,
              "old_level": None, "leveled_up": False, "title": None,
              "reason": None}
    try:
        from models import (CommunityXPLedger, CommunityDailyCounter)
        now = now or datetime.utcnow()
        day = ist_today_str(now)
        base_xp = max(0, int(base_xp or 0))

        # Idempotency: has this exact event already been credited?
        if (session.query(CommunityXPLedger.id)
                .filter(CommunityXPLedger.event_key == event_key).first()):
            result["duplicate"] = True
            result["reason"] = "duplicate"
            return result

        user = _eligible_user(session, user_id)
        if user is None:
            result["reason"] = "ineligible_user"
            return result

        prof = get_or_create_profile(session, user_id)
        if prof.rewards_suspended:
            result["reason"] = "suspended"
            return result

        credited = base_xp
        if not bypass_limits:
            # Per-activity daily limit (count of rewarded events).
            if activity_daily_limit is not None and activity_daily_limit > 0:
                existing = _day_counter(session, user_id, day, activity)
                if existing is not None and existing.count >= activity_daily_limit:
                    result["reason"] = "activity_limit"
                    return result

            # Overall daily CXP ceiling.
            from services import community_config_service as C
            cap = C.max_daily_xp(session)
            used = _day_total_xp(session, user_id, day)
            remaining = max(0, cap - used)
            if remaining <= 0:
                result["capped"] = True
                result["reason"] = "daily_cap"
                return result
            if credited > remaining:
                credited = remaining
                result["capped"] = True

        if credited <= 0:
            result["reason"] = "nothing_to_credit"
            return result

        # Ledger (idempotency guard). A race on event_key surfaces as an
        # IntegrityError on flush, which we treat as a duplicate.
        session.add(CommunityXPLedger(
            user_id=user_id, activity=activity, delta=credited,
            event_key=event_key, chat_id=chat_id,
            detail=(detail or "")[:200] or None, created_at=now))

        # Daily counter upsert.
        counter = _day_counter(session, user_id, day, activity, lock=True)
        if counter is None:
            counter = CommunityDailyCounter(
                user_id=user_id, day=day, activity=activity, count=0, xp=0)
            session.add(counter)
        counter.count += 1
        counter.xp += credited

        old_level = prof.level or 1
        prof.lifetime_xp = int(prof.lifetime_xp or 0) + credited
        new_level, new_title = level_for_xp(session, prof.lifetime_xp)
        prof.level = new_level
        prof.updated_at = now

        session.flush()
        if own:
            session.commit()

        result.update({
            "credited": credited, "lifetime_xp": prof.lifetime_xp,
            "level": new_level, "old_level": old_level,
            "leveled_up": new_level > old_level, "title": new_title,
            "reason": "ok",
        })
        return result
    except Exception as exc:
        if own:
            session.rollback()
        # A unique-constraint race means another worker credited it first.
        name = exc.__class__.__name__
        if name in ("IntegrityError", "FlushError"):
            result["duplicate"] = True
            result["reason"] = "duplicate_race"
            return result
        logger.exception("award_xp failed for user=%s activity=%s", user_id, activity)
        result["reason"] = "error"
        return result
    finally:
        if own:
            session.close()


# ── Convenience hooks ────────────────────────────────────────────────────

def bump_profile_counter(session, user_id, field, day_ist=None):
    """Increment a denormalized lifetime counter on the profile, and the
    active-day counter when a new IST day is seen. Best-effort; caller commits."""
    prof = get_or_create_profile(session, user_id)
    if field and hasattr(prof, field):
        setattr(prof, field, int(getattr(prof, field) or 0) + 1)
    if day_ist:
        if prof.last_active_date != day_ist:
            prof.days_active = int(prof.days_active or 0) + 1
            prof.last_active_date = day_ist
    return prof


# ── Period / leaderboard maths ──────────────────────────────────────────

def period_bound_utc(period: str, now=None):
    period = (period or "all").lower()
    if period in ("today", "day", "daily"):
        return day_start_utc(now)
    if period in ("week", "weekly"):
        return week_start_utc(now)
    if period in ("month", "monthly"):
        return month_start_utc(now)
    return None  # all-time


def get_period_xp(session, user_id: int, period: str, now=None) -> int:
    """CXP a user earned within a period (ledger deltas, positive only)."""
    from sqlalchemy import func
    from models import CommunityXPLedger
    bound = period_bound_utc(period, now)
    q = (session.query(func.coalesce(func.sum(CommunityXPLedger.delta), 0))
         .filter(CommunityXPLedger.user_id == user_id,
                 CommunityXPLedger.delta > 0))
    if bound is not None:
        q = q.filter(CommunityXPLedger.created_at >= bound)
    return int(q.scalar() or 0)


# ── Admin ─────────────────────────────────────────────────────────────────

def admin_adjust_xp(session, user_id: int, delta: int, *, reason: str,
                    admin_id=None, now=None):
    """Adjust a manager's lifetime CXP by ``delta`` (may be negative).

    Always writes a ledger row and an audit record. Reason is mandatory.
    Caller commits. Bypasses daily caps (it is a correction, not an earning).
    """
    from models import CommunityXPLedger, CommunityAuditLog
    if not reason or not str(reason).strip():
        raise ValueError("A reason is required for CXP adjustments.")
    now = now or datetime.utcnow()
    prof = get_or_create_profile(session, user_id)
    old_level = prof.level or 1
    new_total = max(0, int(prof.lifetime_xp or 0) + int(delta))
    applied = new_total - int(prof.lifetime_xp or 0)
    prof.lifetime_xp = new_total
    lvl, title = level_for_xp(session, new_total)
    prof.level = lvl
    prof.updated_at = now
    session.add(CommunityXPLedger(
        user_id=user_id, activity=ACT_ADMIN, delta=applied,
        event_key=f"admin:{user_id}:{now.isoformat()}:{applied}",
        detail=(reason or "")[:200], created_at=now))
    session.add(CommunityAuditLog(
        admin_id=admin_id, user_id=user_id, action="adjust_xp",
        reason=(reason or "")[:400],
        detail=f"delta={delta} applied={applied} new_total={new_total}",
        created_at=now))
    session.flush()
    return {"lifetime_xp": new_total, "level": lvl, "title": title,
            "old_level": old_level, "leveled_up": lvl > old_level,
            "applied": applied}

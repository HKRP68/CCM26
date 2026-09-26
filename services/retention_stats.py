"""Cohort retention (D1 / D7 / D30) and the new-player funnel, for the website.

The dashboard's older "User Retention" block answers "how many people were
active today". That can't tell you whether *new* players stick, and new
players are where retention is won or lost. So this module groups players
by the IST day they ran /debut (their cohort) and asks, for each cohort, what
share did anything at all (any ``ActivityLog`` row) exactly 1, 7 and 30 days
later. The classic day-N retention curve.

The funnel follows the same window: /start visitors (``StartVisit``, which
counts people with no account yet) → /debut → joined the Official GC →
played a first match → finished the journey → came back on day 1.

Results are cached in-process for a few minutes: the dashboard is reloaded
often and the numbers move slowly.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from sqlalchemy import func

logger = logging.getLogger(__name__)

IST = timedelta(hours=5, minutes=30)
OFFSETS = (1, 7, 30)
_CACHE = {"at": 0.0, "key": None, "data": None}
CACHE_SECONDS = 300


def _ist_midnight(dt):
    ist = dt + IST
    return ist.replace(hour=0, minute=0, second=0, microsecond=0)


def cohort_rows(user_rows, active_pairs, today_ist, days=45):
    """Pure cohort math, split out for tests.

    ``user_rows``: iterable of (user_id, created_at_utc).
    ``active_pairs``: set of (user_id, ist_date) with any activity that day.
    ``today_ist``: the current IST date.
    Returns a list (newest cohort first) of dicts:
    ``{"label", "date", "size", "d1", "d7", "d30"}`` where each dN is a
    percentage, or None when that day hasn't happened yet.
    """
    cohorts = {}
    for uid, created in user_rows:
        if created is None:
            continue
        d = (created + IST).date()
        if (today_ist - d).days >= days or d > today_ist:
            continue
        cohorts.setdefault(d, []).append(uid)
    rows = []
    for d in sorted(cohorts, reverse=True):
        ids = cohorts[d]
        row = {"label": d.strftime("%d %b"), "date": d.isoformat(), "size": len(ids)}
        for n in OFFSETS:
            target = d + timedelta(days=n)
            if target >= today_ist:  # not over yet (today counts as partial)
                row[f"d{n}"] = None
                continue
            back = sum(1 for uid in ids if (uid, target) in active_pairs)
            row[f"d{n}"] = round(back / len(ids) * 100) if ids else 0
        rows.append(row)
    return rows


def _weighted(rows, key):
    """Size-weighted average of a dN column over cohorts that have it."""
    num = den = 0
    for r in rows:
        v = r.get(key)
        if v is None:
            continue
        num += v * r["size"]
        den += r["size"]
    return round(num / den) if den else None


def compute(db, *, days=45, now=None):
    """The dashboard payload. ``db`` is an open session."""
    from models import ActivityLog, StartVisit, User

    now = now or datetime.utcnow()
    key = (days, now.strftime("%Y%m%d%H%M")[:11])  # 10-minute bucket
    if _CACHE["data"] is not None and _CACHE["key"] == key \
            and time.monotonic() - _CACHE["at"] < CACHE_SECONDS:
        return _CACHE["data"]

    today_ist = (now + IST).date()
    window_start = _ist_midnight(now) - timedelta(days=days) - IST  # UTC instant

    users = (db.query(User.id, User.created_at, User.onboarding_steps,
                      User.onboarding_done_at, User.matches_played)
             .filter(User.created_at >= window_start,
                     User.telegram_id > 0).all())
    ids = [u.id for u in users]

    active_pairs = set()
    if ids:
        # Activity for these users from their cohort day to day 30 after it.
        act_end = now
        for chunk_start in range(0, len(ids), 500):
            chunk = ids[chunk_start:chunk_start + 500]
            q = (db.query(ActivityLog.user_id, ActivityLog.created_at)
                 .filter(ActivityLog.user_id.in_(chunk),
                         ActivityLog.created_at >= window_start,
                         ActivityLog.created_at < act_end))
            for uid, ts in q.yield_per(2000):
                active_pairs.add((uid, (ts + IST).date()))

    rows = cohort_rows([(u.id, u.created_at) for u in users], active_pairs,
                       today_ist, days=days)

    # Funnel over the same window.
    try:
        visitors = (db.query(func.count(func.distinct(StartVisit.telegram_id)))
                    .filter(StartVisit.day >= (today_ist - timedelta(days=days)).isoformat())
                    .scalar() or 0)
    except Exception:
        logger.debug("start visit count failed", exc_info=True)
        visitors = 0
    debuted = len(users)

    def _has(u, k):
        return k in (u.onboarding_steps or "").split(",")

    joined_gc = sum(1 for u in users if _has(u, "gc"))
    first_match = sum(1 for u in users
                      if _has(u, "match") or (u.matches_played or 0) > 0)
    journey_done = sum(1 for u in users if u.onboarding_done_at is not None)
    d1_back = 0
    for u in users:
        d = (u.created_at + IST).date()
        if d + timedelta(days=1) < today_ist and (u.id, d + timedelta(days=1)) in active_pairs:
            d1_back += 1

    top = max(visitors, debuted, 1)
    funnel = []
    for label, count in (("Opened /start", visitors), ("Ran /debut", debuted),
                         ("Joined Official GC", joined_gc),
                         ("Played a first match", first_match),
                         ("Finished the journey", journey_done),
                         ("Came back on day 1", d1_back)):
        funnel.append({"label": label, "count": count,
                       "pct": round(count / top * 100)})

    data = {
        "cohorts": rows,
        "avg_d1": _weighted(rows, "d1"),
        "avg_d7": _weighted(rows, "d7"),
        "avg_d30": _weighted(rows, "d30"),
        "funnel": funnel,
        "days": days,
    }
    _CACHE.update(at=time.monotonic(), key=key, data=data)
    return data

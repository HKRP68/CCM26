"""Spin/daily quota system.

Each user gets:
  - 1 FREE spin per 24h cycle (no ad needed)
  - SPIN_AD_QUOTA additional ad-gated spins per cycle

Same for daily. Cycle starts when the user uses their first slot, resets
24h after that.

The legacy `last_gspin` / `last_daily` columns are kept for compatibility
(bot still writes them on use) but the gate logic now uses the quota fields.
"""

from datetime import datetime, timedelta

# Tunable — number of ad-gated uses per cycle (in addition to 1 free use)
SPIN_AD_QUOTA = 5
DAILY_AD_QUOTA = 5
CYCLE_HOURS = 24  # rolling cycle length


def _reset_if_expired(stats, prefix):
    """Reset the cycle if 24h have passed since it started.

    `prefix` is "spin" or "daily" — selects which set of columns.
    Returns True if a reset happened.
    """
    started_attr = f"{prefix}_cycle_started_at"
    free_attr = f"{prefix}_free_used"
    count_attr = f"{prefix}_ad_count"

    started = getattr(stats, started_attr, None)
    if started is None:
        return False
    elapsed = (datetime.utcnow() - started).total_seconds()
    if elapsed >= CYCLE_HOURS * 3600:
        setattr(stats, started_attr, None)
        setattr(stats, free_attr, False)
        setattr(stats, count_attr, 0)
        return True
    return False


def get_quota_status(stats, kind):
    """Return dict describing current quota state.

    {
      "free_available": bool,
      "ad_used": int,
      "ad_total": int,
      "cycle_reset_in": int seconds until full reset (0 if no active cycle),
      "all_used": bool,
    }
    """
    if kind == "spin":
        ad_total = SPIN_AD_QUOTA
        started_attr = "spin_cycle_started_at"
        free_attr = "spin_free_used"
        count_attr = "spin_ad_count"
    else:
        ad_total = DAILY_AD_QUOTA
        started_attr = "daily_cycle_started_at"
        free_attr = "daily_free_used"
        count_attr = "daily_ad_count"

    if stats is None:
        return {"free_available": True, "ad_used": 0, "ad_total": ad_total,
                "cycle_reset_in": 0, "all_used": False}

    _reset_if_expired(stats, kind)
    started = getattr(stats, started_attr, None)
    free_used = bool(getattr(stats, free_attr, False))
    ad_count = int(getattr(stats, count_attr, 0) or 0)

    free_available = not free_used
    all_used = (not free_available) and (ad_count >= ad_total)

    if started is not None:
        elapsed = (datetime.utcnow() - started).total_seconds()
        reset_in = max(0, int(CYCLE_HOURS * 3600 - elapsed))
    else:
        reset_in = 0

    return {
        "free_available": free_available,
        "ad_used": ad_count,
        "ad_total": ad_total,
        "cycle_reset_in": reset_in,
        "all_used": all_used,
    }


def can_use(stats, kind, ad_provided):
    """Check if user can use a spin/daily slot now.

    `ad_provided` = True means caller verified the user watched an ad.
    Returns (allowed: bool, slot_type: 'free'|'ad'|None, reason: str|None).

    Caller should call consume_slot() after a successful action.
    """
    status = get_quota_status(stats, kind)
    if status["free_available"]:
        return True, "free", None
    if not ad_provided:
        if status["ad_used"] >= status["ad_total"]:
            return False, None, "cycle_exhausted"
        return False, None, "ad_required"
    if status["ad_used"] >= status["ad_total"]:
        return False, None, "cycle_exhausted"
    return True, "ad", None


def consume_slot(stats, kind, slot_type):
    """Mark one slot consumed. Caller must commit() afterwards.

    slot_type must be 'free' or 'ad'.
    """
    if kind == "spin":
        started_attr = "spin_cycle_started_at"
        free_attr = "spin_free_used"
        count_attr = "spin_ad_count"
    else:
        started_attr = "daily_cycle_started_at"
        free_attr = "daily_free_used"
        count_attr = "daily_ad_count"

    # Reset cycle if expired before consuming (defensive)
    _reset_if_expired(stats, kind)

    # Start the cycle if not already started
    if getattr(stats, started_attr, None) is None:
        setattr(stats, started_attr, datetime.utcnow())

    if slot_type == "free":
        setattr(stats, free_attr, True)
    elif slot_type == "ad":
        setattr(stats, count_attr,
                int(getattr(stats, count_attr, 0) or 0) + 1)

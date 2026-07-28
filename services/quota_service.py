"""Spin/daily quota system.

Each user gets, per rolling cycle:
  - 1 FREE use (no ad)
  - N AD-gated uses (N = `spin_ad_quota` or `daily_ad_quota` from GameConfig)

The cycle length is the command's effective, subscription-tier-reduced cooldown
(spin 8h, daily 24h at base; shorter from Silver up) — the same value the
bot and the Mini App's cooldown timer use — so the Mini App becomes available
again on that schedule rather than a fixed 24h window.

Quotas are admin-tunable from /admin/economy — defaults to 5 each.
"""

from datetime import datetime, timedelta

# Fallback when GameConfig isn't reachable (e.g. fresh DB before init).
DEFAULT_SPIN_AD_QUOTA = 5
DEFAULT_DAILY_AD_QUOTA = 5
# Legacy fallback cycle length, used only when the per-command cooldown can't
# be resolved (config/services unreachable).
CYCLE_HOURS = 24


def _cycle_seconds(session, user, kind):
    """Length of the free/ad quota cycle for ``kind``, in seconds.

    Equals the command's effective (tier-reduced) cooldown: ``gspin`` for the
    "spin" kind, ``daily`` otherwise. Falls back to the legacy 24h window if the
    cooldown can't be resolved.
    """
    try:
        from config import GSPIN_COOLDOWN, DAILY_COOLDOWN
        from services.command_config_service import get_user_cooldown
        if kind == "spin":
            secs = get_user_cooldown(session, user, "gspin", GSPIN_COOLDOWN)
        else:
            secs = get_user_cooldown(session, user, "daily", DAILY_COOLDOWN)
        return int(secs) if secs and secs > 0 else CYCLE_HOURS * 3600
    except Exception:
        return CYCLE_HOURS * 3600


def _get_ad_quota(session, kind):
    """Read configured ad quota from GameConfig with fallback."""
    if session is None:
        return DEFAULT_SPIN_AD_QUOTA if kind == "spin" else DEFAULT_DAILY_AD_QUOTA
    try:
        from services.config_service import get_config
        cfg = get_config(session)
        if kind == "spin":
            return int(cfg.get("spin_ad_quota") or DEFAULT_SPIN_AD_QUOTA)
        return int(cfg.get("daily_ad_quota") or DEFAULT_DAILY_AD_QUOTA)
    except Exception:
        return DEFAULT_SPIN_AD_QUOTA if kind == "spin" else DEFAULT_DAILY_AD_QUOTA


def _reset_if_expired(stats, prefix, cycle_secs):
    """Reset the cycle if ``cycle_secs`` have passed since it started.

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
    if elapsed >= cycle_secs:
        setattr(stats, started_attr, None)
        setattr(stats, free_attr, False)
        setattr(stats, count_attr, 0)
        return True
    return False


def get_quota_status(stats, kind, session=None, user=None):
    """Return dict describing current quota state.

    {
      "free_available": bool,
      "ad_used": int,
      "ad_total": int,
      "cycle_reset_in": int seconds until full reset (0 if no active cycle),
      "all_used": bool,
    }

    `session` is optional — used to look up admin-configured ad_quota and the
    per-command cooldown. `user` is optional — used to apply the subscription
    tier's cooldown reduction to the cycle length.
    """
    ad_total = _get_ad_quota(session, kind)
    cycle_secs = _cycle_seconds(session, user, kind)
    if kind == "spin":
        started_attr = "spin_cycle_started_at"
        free_attr = "spin_free_used"
        count_attr = "spin_ad_count"
    else:
        started_attr = "daily_cycle_started_at"
        free_attr = "daily_free_used"
        count_attr = "daily_ad_count"

    if stats is None:
        return {"free_available": True, "ad_used": 0, "ad_total": ad_total,
                "cycle_reset_in": 0, "all_used": False}

    _reset_if_expired(stats, kind, cycle_secs)
    started = getattr(stats, started_attr, None)
    free_used = bool(getattr(stats, free_attr, False))
    ad_count = int(getattr(stats, count_attr, 0) or 0)

    free_available = not free_used
    all_used = (not free_available) and (ad_count >= ad_total)

    if started is not None:
        elapsed = (datetime.utcnow() - started).total_seconds()
        reset_in = max(0, int(cycle_secs - elapsed))
    else:
        reset_in = 0

    return {
        "free_available": free_available,
        "ad_used": ad_count,
        "ad_total": ad_total,
        "cycle_reset_in": reset_in,
        "all_used": all_used,
    }


def can_use(stats, kind, ad_provided, session=None, user=None):
    """Check if user can use a spin/daily slot now.

    `ad_provided` = True means caller verified the user watched an ad.
    `session`/`user` are optional — passed through to read configured quota and
    the tier-reduced cycle length.
    Returns (allowed: bool, slot_type: 'free'|'ad'|None, reason: str|None).
    """
    status = get_quota_status(stats, kind, session=session, user=user)
    if status["free_available"]:
        return True, "free", None
    if not ad_provided:
        if status["ad_used"] >= status["ad_total"]:
            return False, None, "cycle_exhausted"
        return False, None, "ad_required"
    if status["ad_used"] >= status["ad_total"]:
        return False, None, "cycle_exhausted"
    return True, "ad", None


def consume_slot(stats, kind, slot_type, session=None, user=None):
    """Mark one slot consumed. Caller must commit() afterwards.

    slot_type must be 'free' or 'ad'. `session`/`user` are optional — used only
    for the defensive expiry check's cycle length.
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
    _reset_if_expired(stats, kind, _cycle_seconds(session, user, kind))

    # Start the cycle if not already started
    if getattr(stats, started_attr, None) is None:
        setattr(stats, started_attr, datetime.utcnow())

    if slot_type == "free":
        setattr(stats, free_attr, True)
    elif slot_type == "ad":
        setattr(stats, count_attr,
                int(getattr(stats, count_attr, 0) or 0) + 1)

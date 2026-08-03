"""Spin/daily quota system.

Each user gets, per rolling cycle:
  - 1 FREE use (no ad)
  - N AD-gated uses (N = `spin_ad_quota` or `daily_ad_quota` from GameConfig)

Of those N ad-gated uses, up to `spin_nofill_grace` may be taken with a
**no-fill pass** rather than a watched ad — see `services.ad_service`. The
pass is not extra quota: it spends an ad slot like any other, it just doesn't
require an ad the network was never going to serve. Its own per-cycle cap is
what stops a client claiming no-fill on every request.

The cycle length is the command's effective, subscription-tier-reduced cooldown
(spin 8h, daily 24h at base; shorter from Silver up) — the same value the
bot and the Mini App's cooldown timer use — so the Mini App becomes available
again on that schedule rather than a fixed 24h window.

Quotas are admin-tunable from /admin/economy — defaults to 5 each.

────────────────────────────────────────────────────────────────────────
The gap between rewarded ads
────────────────────────────────────────────────────────────────────────
``GameConfig.ad_reward_gap_minutes`` can lock the next ad-gated slot for a
while after one is taken, so a cycle's ad quota is spread out rather than
drained in two minutes.

**It ships disabled (0).** It was briefly defaulted to an hour, and that one
number produced the single loudest complaint this feature has ever had:
players watched a full rewarded ad and were answered "next ad spin in 59m".
The ad was banked rather than lost, but nobody reads a banked credit as a
reward — from the other side of the screen it is simply "I watched an ad and
got nothing", for the spin, the daily claim and the free pack alike. Rationing
is not worth being indistinguishable from being broken, so watching an ad now
pays out on the spot and the quota alone decides how many ads are worth
watching.

The mechanism stays, admin-tunable from /admin/economy, for anyone who wants
the rationing back. When it is switched on, three rules keep it honest:

  • it never touches the FREE use — that one is not an ad reward, so a player
    who opens the app to a free spin is never told to wait;
  • it covers no-fill passes too. A pass stands in for a watched ad, so letting
    it skip the gap would make "no ad available" the fast route past it;
  • it is enforced *before* the ad is shown, and any ad that still reaches the
    server inside the gap is banked as an ad credit rather than burned — see
    ``services.ad_service.bank_credit``. The gap delays a reward; it must never
    cost the player one they already watched an ad for.
"""

from datetime import datetime, timedelta

# Fallback when GameConfig isn't reachable (e.g. fresh DB before init).
DEFAULT_SPIN_AD_QUOTA = 5
DEFAULT_DAILY_AD_QUOTA = 5
# How many of the ad-gated slots may be taken without an ad when the network
# has none to serve. Small on purpose: it is a rescue, not a second free tier.
DEFAULT_NOFILL_GRACE = 2
# Minimum gap between two ad-gated uses of the same feature, in minutes.
# 0 = watching an ad pays out immediately, which is the shipped behaviour. See
# the module docstring for why this is not an hour any more.
DEFAULT_AD_GAP_MINUTES = 0
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


def _get_nofill_grace(session):
    """Read the configured no-fill grace from GameConfig with fallback.

    One figure covers spin and daily: the thing being limited is how often a
    client may claim "the network had nothing", which is a property of the ad
    network rather than of the feature the ad was gating.
    """
    if session is None:
        return DEFAULT_NOFILL_GRACE
    try:
        from services.config_service import get_config
        cfg = get_config(session)
        value = cfg.get("spin_nofill_grace")
        return DEFAULT_NOFILL_GRACE if value is None else max(0, int(value))
    except Exception:
        return DEFAULT_NOFILL_GRACE


def _get_ad_gap_seconds(session):
    """Configured gap between two ad-gated uses, in seconds (0 = disabled).

    One figure covers spin and daily: what is being spaced out is how often a
    player watches a rewarded ad, which is a property of the ads rather than of
    the feature behind them. Each feature still tracks its own last-ad time, so
    a spin never blocks the daily claim.
    """
    if session is None:
        return DEFAULT_AD_GAP_MINUTES * 60
    try:
        from services.config_service import get_config
        cfg = get_config(session)
        value = cfg.get("ad_reward_gap_minutes")
        minutes = DEFAULT_AD_GAP_MINUTES if value is None else int(value)
        return max(0, minutes) * 60
    except Exception:
        return DEFAULT_AD_GAP_MINUTES * 60


def _ad_ready_in(stats, kind, gap_secs):
    """Seconds until the next ad-gated use of ``kind`` is allowed (0 = now).

    Reads ``spin_last_ad_at`` / ``daily_last_ad_at``; a database that predates
    those columns simply has no gap, which is the right way to fail — an
    unknown last-ad time must not lock a player out.
    """
    if gap_secs <= 0:
        return 0
    last = getattr(stats, f"{'spin' if kind == 'spin' else 'daily'}_last_ad_at",
                   None)
    if last is None:
        return 0
    elapsed = (datetime.utcnow() - last).total_seconds()
    # A clock skew (or a restored backup) that puts the stamp in the future
    # would otherwise lock the feature for as long as the skew lasts.
    if elapsed < 0:
        return gap_secs
    return max(0, int(gap_secs - elapsed))


def _reset_if_expired(stats, prefix, cycle_secs):
    """Reset the cycle if ``cycle_secs`` have passed since it started.

    `prefix` is "spin" or "daily" — selects which set of columns.
    Returns True if a reset happened.
    """
    started_attr = f"{prefix}_cycle_started_at"
    free_attr = f"{prefix}_free_used"
    count_attr = f"{prefix}_ad_count"
    nofill_attr = f"{prefix}_nofill_used"

    started = getattr(stats, started_attr, None)
    if started is None:
        return False
    elapsed = (datetime.utcnow() - started).total_seconds()
    if elapsed >= cycle_secs:
        setattr(stats, started_attr, None)
        setattr(stats, free_attr, False)
        setattr(stats, count_attr, 0)
        # The grace is per cycle, so it resets with everything else. Guarded
        # because a DB that predates the column still serves rows without it.
        if hasattr(stats, nofill_attr):
            setattr(stats, nofill_attr, 0)
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
      "nofill_left": int no-fill passes still available this cycle,
      "nofill_total": int configured passes per cycle,
      "ad_gap_seconds": int configured gap between rewarded ads,
      "ad_ready_in": int seconds until the next ad-gated use is allowed,
      "ad_available": bool — an ad-gated use can be taken right now,
    }

    `session` is optional — used to look up admin-configured ad_quota and the
    per-command cooldown. `user` is optional — used to apply the subscription
    tier's cooldown reduction to the cycle length.
    """
    ad_total = _get_ad_quota(session, kind)
    nofill_total = _get_nofill_grace(session)
    gap_secs = _get_ad_gap_seconds(session)
    cycle_secs = _cycle_seconds(session, user, kind)
    prefix = "spin" if kind == "spin" else "daily"
    started_attr = f"{prefix}_cycle_started_at"
    free_attr = f"{prefix}_free_used"
    count_attr = f"{prefix}_ad_count"
    nofill_attr = f"{prefix}_nofill_used"

    if stats is None:
        return {"free_available": True, "ad_used": 0, "ad_total": ad_total,
                "cycle_reset_in": 0, "all_used": False,
                "nofill_left": nofill_total, "nofill_total": nofill_total,
                "ad_gap_seconds": gap_secs, "ad_ready_in": 0,
                "ad_available": ad_total > 0}

    _reset_if_expired(stats, kind, cycle_secs)
    started = getattr(stats, started_attr, None)
    free_used = bool(getattr(stats, free_attr, False))
    ad_count = int(getattr(stats, count_attr, 0) or 0)
    nofill_used = int(getattr(stats, nofill_attr, 0) or 0)

    free_available = not free_used
    all_used = (not free_available) and (ad_count >= ad_total)

    if started is not None:
        elapsed = (datetime.utcnow() - started).total_seconds()
        reset_in = max(0, int(cycle_secs - elapsed))
    else:
        reset_in = 0

    ad_ready_in = _ad_ready_in(stats, kind, gap_secs)

    return {
        "free_available": free_available,
        "ad_used": ad_count,
        "ad_total": ad_total,
        "cycle_reset_in": reset_in,
        "all_used": all_used,
        "nofill_left": max(0, nofill_total - nofill_used),
        "nofill_total": nofill_total,
        "ad_gap_seconds": gap_secs,
        "ad_ready_in": ad_ready_in,
        # What the client needs to decide whether showing an ad is worth it:
        # slots left AND the gap elapsed. Reported separately from the slot
        # count so a waiting player can be told when, not just "no".
        "ad_available": ad_count < ad_total and ad_ready_in <= 0,
    }


def can_use(stats, kind, ad_provided, session=None, user=None):
    """Check if user can use a spin/daily slot now.

    `ad_provided` = True means caller verified the user watched an ad.
    `session`/`user` are optional — passed through to read configured quota and
    the tier-reduced cycle length.
    Returns (allowed: bool, slot_type: 'free'|'ad'|None, reason: str|None).

    Reasons: ``ad_required`` (an ad would unlock this), ``cycle_exhausted``
    (nothing left until the cycle resets), ``ad_cooldown`` (slots remain but the
    gap between rewarded ads has not elapsed). A caller that already consumed
    ad evidence and gets ``ad_cooldown`` or ``cycle_exhausted`` back owes the
    player an ad credit — the ad was watched, only the timing was wrong.
    """
    status = get_quota_status(stats, kind, session=session, user=user)
    if status["free_available"]:
        return True, "free", None
    if status["ad_used"] >= status["ad_total"]:
        return False, None, "cycle_exhausted"
    # The gap is checked before "did you bring an ad": inside it the answer is
    # the same either way, and reporting `ad_required` there would send the
    # client off to watch an ad it is about to be told it cannot spend.
    if status["ad_ready_in"] > 0:
        return False, None, "ad_cooldown"
    if not ad_provided:
        return False, None, "ad_required"
    return True, "ad", None


def claim_nofill_pass(stats, kind, session=None, user=None):
    """Debit one no-fill pass. Returns ``(granted, status)``; caller commits.

    Granted only when the user has both an ad slot to spend it on and grace
    left in this cycle — a pass is permission to skip the *ad*, never permission
    to exceed the quota. Debiting here rather than at spin time is deliberate:
    the pass is spent the moment it is handed out, so a client that asks
    repeatedly burns its grace instead of farming tokens it can replay later.

    The gap between rewarded ads applies here too: a pass stands in for a
    watched ad, so handing one out inside the gap would make "no ad available"
    the cheap way around it.
    """
    status = get_quota_status(stats, kind, session=session, user=user)
    if status["free_available"]:
        # Nothing to rescue — the free slot needs no ad at all.
        return False, status
    if status["ad_used"] >= status["ad_total"]:
        return False, status
    if status["ad_ready_in"] > 0:
        return False, status
    if status["nofill_left"] <= 0:
        return False, status

    nofill_attr = f"{'spin' if kind == 'spin' else 'daily'}_nofill_used"
    setattr(stats, nofill_attr, int(getattr(stats, nofill_attr, 0) or 0) + 1)
    return True, get_quota_status(stats, kind, session=session, user=user)


def consume_slot(stats, kind, slot_type, session=None, user=None):
    """Mark one slot consumed. Caller must commit() afterwards.

    slot_type must be 'free' or 'ad'. `session`/`user` are optional — used only
    for the defensive expiry check's cycle length.
    """
    if kind == "spin":
        started_attr = "spin_cycle_started_at"
        free_attr = "spin_free_used"
        count_attr = "spin_ad_count"
        last_ad_attr = "spin_last_ad_at"
    else:
        started_attr = "daily_cycle_started_at"
        free_attr = "daily_free_used"
        count_attr = "daily_ad_count"
        last_ad_attr = "daily_last_ad_at"

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
        # Start the gap now. Stamped on the slot rather than on the ad watch so
        # an ad that was banked and redeemed later still spaces out the *reward*
        # — which is the thing being limited. hasattr-guarded because a DB that
        # predates the column still serves rows without it.
        if hasattr(stats, last_ad_attr):
            setattr(stats, last_ad_attr, datetime.utcnow())

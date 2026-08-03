"""Login streak ladder — a 7-day reward calendar that loops.

Distinct from the /daily milestone streak (services/streak_service.py). This
tracks consecutive *calendar days* the user opens the app, and offers an
escalating daily reward they claim once per day.

Flow:
  - On app open, call touch_login_streak() to advance/reset the streak.
  - The Mini App shows the 7-day ladder + a "Claim today" button.
  - claim_login_reward() pays the current day's reward, once per UTC day.

The ladder loops: after day 7, the next day is day 1 again (but login_streak
keeps counting up for the all-time best).
"""

import logging
from datetime import datetime, timedelta

from models import UserStats

logger = logging.getLogger(__name__)

# 7-day reward ladder. Day 7 is the jackpot (a free pack grant + big coins).
# kind: 'coins' | 'gems' | 'coins_gems' | 'pack'
LADDER = [
    {"day": 1, "kind": "coins",      "coins": 150,  "gems": 0,  "label": "150 coins"},
    {"day": 2, "kind": "coins",      "coins": 300,  "gems": 0,  "label": "300 coins"},
    {"day": 3, "kind": "coins_gems", "coins": 500,  "gems": 1,  "label": "500 coins + 1 gem"},
    {"day": 4, "kind": "coins",      "coins": 750,  "gems": 0,  "label": "750 coins"},
    {"day": 5, "kind": "coins_gems", "coins": 1000, "gems": 1,  "label": "1,000 coins + 1 gem"},
    {"day": 6, "kind": "coins",      "coins": 1500, "gems": 0,  "label": "1,500 coins"},
    {"day": 7, "kind": "coins_gems", "coins": 3000, "gems": 2,  "label": "3,000 coins + 2 gems"},
]

LADDER_LENGTH = len(LADDER)


def _today():
    return datetime.utcnow().strftime("%Y-%m-%d")


def _yesterday():
    return (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")


def touch_login_streak(session, stats, user=None):
    """Advance the login streak on app open. Idempotent within a calendar day.

    Returns dict {streak, best, ladder_day, claimable_today,
    already_claimed_today, tier_multiplier}. ``user`` is optional and only used
    to report the subscription multiplier. Caller commits.
    """
    today = _today()
    last = stats.last_login_date

    if last == today:
        # Already counted today — no change to streak
        pass
    elif last == _yesterday():
        # Consecutive day → advance
        stats.login_streak = (stats.login_streak or 0) + 1
        stats.last_login_date = today
    else:
        # Missed a day (or first ever) → reset to 1
        stats.login_streak = 1
        stats.last_login_date = today

    if (stats.login_streak or 0) > (stats.login_best_streak or 0):
        stats.login_best_streak = stats.login_streak

    return get_login_status(session, stats, user=user)


def _tier_multiplier(user):
    """The active subscription tier's daily-login multiplier (Diamond = 2×).

    Best-effort: any failure falls back to 1× so a login reward is never lost
    to a subscription lookup problem.
    """
    if user is None:
        return 1
    try:
        from services import subscription_service
        return subscription_service.daily_login_multiplier(user)
    except Exception:
        logger.exception("login reward tier multiplier failed (non-fatal)")
        return 1


def get_login_status(session, stats, user=None):
    """Return the current login-streak status for display.

    ``user`` is optional; pass it so the Mini App can show the member's tier
    multiplier (Diamond doubles every ladder payout) next to the ladder.
    """
    streak = stats.login_streak or 0
    # Ladder day is 1-based position within the looping 7-day cycle.
    # streak 1 -> day 1, ... streak 7 -> day 7, streak 8 -> day 1, etc.
    if streak <= 0:
        ladder_day = 0
    else:
        ladder_day = ((streak - 1) % LADDER_LENGTH) + 1

    already_claimed_today = (stats.login_reward_claimed_date == _today())
    # Claimable only if we've logged in today (streak counted) and not yet claimed
    claimable_today = (stats.last_login_date == _today()
                       and not already_claimed_today
                       and ladder_day >= 1)

    return {
        "streak": streak,
        "best": stats.login_best_streak or 0,
        "ladder_day": ladder_day,
        "ladder": LADDER,
        "ladder_length": LADDER_LENGTH,
        "claimable_today": claimable_today,
        "already_claimed_today": already_claimed_today,
        # Subscription multiplier applied to the day's coins/gems on claim.
        "tier_multiplier": _tier_multiplier(user),
    }


def _claim_today(session, user, today):
    """Stamp today onto the user's row, once. Returns whether this caller won.

    One UPDATE that only matches a row not already stamped with ``today``, so
    two concurrent claims can never both pass it. ``synchronize_session=False``
    because nothing in this session is querying on that column; the in-memory
    ``stats`` is brought in line by the caller.

    Sessionless callers (unit tests exercising the ladder maths against a
    stand-in stats object) have no row to update — they fall back to the
    in-memory check the caller already made, which is all a single-threaded
    test needs.
    """
    if session is None or user is None:
        return True
    try:
        won = (session.query(UserStats)
               .filter(UserStats.user_id == user.id,
                       (UserStats.login_reward_claimed_date.is_(None))
                       | (UserStats.login_reward_claimed_date != today))
               .update({UserStats.login_reward_claimed_date: today},
                       synchronize_session=False))
        return bool(won)
    except Exception:
        # A backend that can't run the conditional update must not block a
        # legitimate claim — the caller's read check and row lock still stand.
        logger.exception("login reward compare-and-set failed; falling back")
        return True


def claim_login_reward(session, user, stats):
    """Pay today's ladder reward. Returns dict {ok, error?, reward?, ...}.
    Caller commits.

    Claiming the day is a compare-and-set, not a read-then-write. Reading
    ``login_reward_claimed_date``, paying out, and stamping the date at the end
    left the whole payout between the check and the write: three taps on the
    Mini App's claim button raced through that gap and were paid three times.
    Stamping the date first, in one conditional UPDATE, means exactly one
    request can win the day — the loser sees rowcount 0 and is refused with the
    reward untouched. Atomic on every backend, and it does not depend on the
    caller holding a row lock (though the Mini App takes one anyway).
    """
    today = _today()

    # Must have logged in today
    if stats.last_login_date != today:
        # Advance the streak first (covers direct-claim without a prior touch)
        touch_login_streak(session, stats)

    if stats.login_reward_claimed_date == today:
        return {"ok": False, "error": "already_claimed",
                "message": "You've already claimed today's reward. Come back tomorrow!"}

    if not _claim_today(session, user, today):
        return {"ok": False, "error": "already_claimed",
                "message": "You've already claimed today's reward. Come back tomorrow!"}

    streak = stats.login_streak or 1
    ladder_day = ((streak - 1) % LADDER_LENGTH) + 1
    reward = LADDER[ladder_day - 1]

    coins = reward.get("coins", 0)
    gems = reward.get("gems", 0)
    granted_pack = False

    # Apply active event coin multiplier
    mult_used = 1.0
    try:
        from services.event_service import apply_coin_multiplier
        coins, mult_used = apply_coin_multiplier(session, coins)
    except Exception:
        pass

    # Subscription perk: the tier's daily-login multiplier (Diamond = 2×)
    # multiplies BOTH currencies, and stacks on top of any active event
    # multiplier — the perk is advertised as "2X Daily Login Reward".
    tier_mult = _tier_multiplier(user)
    if tier_mult > 1:
        coins = int(coins * tier_mult)
        gems = int(gems * tier_mult)

    if coins:
        user.total_coins = (user.total_coins or 0) + coins
    if gems:
        user.total_gems = (user.total_gems or 0) + gems

    # Contribute to the monthly season (engagement points)
    try:
        from services.season_service import safe_add_season_points
        safe_add_season_points(session, user, points=5)
    except Exception:
        pass

    # Day-7 bonus: also grant a free pack if a pack named "Bronze Pack" exists
    if reward["kind"] == "pack" or ladder_day == LADDER_LENGTH:
        try:
            from models import Pack
            from services.pack_service import grant_pack
            pack = (session.query(Pack)
                    .filter(Pack.is_active == True)
                    .order_by(Pack.slot_number.asc()).first())
            if pack:
                grant_pack(session, user.id, pack.id, source="login_streak")
                granted_pack = True
        except Exception:
            logger.exception("Login streak pack grant failed (non-fatal)")

    # The row was already stamped by the compare-and-set above; this keeps the
    # in-memory copy the caller reads (and returns to the client) in step.
    stats.login_reward_claimed_date = today

    # Activity log
    try:
        from services.activity_service import log_activity
        log_activity(session, user.id, "login_reward",
                     f"Login streak day {ladder_day} (streak {streak}): "
                     f"+{coins} coins, +{gems} gems"
                     + (f" ({tier_mult}× tier)" if tier_mult > 1 else "")
                     + (" + free pack" if granted_pack else ""),
                     coins_change=coins)
    except Exception:
        pass

    return {
        "ok": True,
        "reward": {
            "day": ladder_day,
            "coins": coins,
            "gems": gems,
            "granted_pack": granted_pack,
            "label": reward["label"],
            "multiplier": mult_used,
            "tier_multiplier": tier_mult,
        },
        "streak": streak,
        "balance": {
            "coins": user.total_coins or 0,
            "gems": user.total_gems or 0,
        },
    }

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


def touch_login_streak(session, stats):
    """Advance the login streak on app open. Idempotent within a calendar day.

    Returns dict {streak, best, ladder_day, claimable_today, already_claimed_today}.
    Caller commits.
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

    return get_login_status(session, stats)


def get_login_status(session, stats):
    """Return the current login-streak status for display."""
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
    }


def claim_login_reward(session, user, stats):
    """Pay today's ladder reward. Returns dict {ok, error?, reward?, ...}.
    Caller commits."""
    today = _today()

    # Must have logged in today
    if stats.last_login_date != today:
        # Advance the streak first (covers direct-claim without a prior touch)
        touch_login_streak(session, stats)

    if stats.login_reward_claimed_date == today:
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

    stats.login_reward_claimed_date = today

    # Activity log
    try:
        from services.activity_service import log_activity
        log_activity(session, user.id, "login_reward",
                     f"Login streak day {ladder_day} (streak {streak}): "
                     f"+{coins} coins, +{gems} gems"
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
        },
        "streak": streak,
        "balance": {
            "coins": user.total_coins or 0,
            "gems": user.total_gems or 0,
        },
    }

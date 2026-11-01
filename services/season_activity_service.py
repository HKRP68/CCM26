"""Small season bonuses for completed activities, capped per UTC day.

The activity log calls this only after a successful action. These bonuses
never change ranked rating or season wins. Caller owns the transaction.
"""

from collections import Counter
from datetime import datetime

ACTIVITY_DAILY_CAP = 40
# action: (display label, points per completion, rewarded completions per day)
ACTIVITY_RULES = {
    "claim": ("Claim a player", 3, 3),
    "daily": ("Claim daily rewards", 5, 1),
    "login_reward": ("Claim login reward", 3, 1),
    "open_pack": ("Open a pack", 2, 3),
    "free_pack": ("Claim a free pack", 2, 1),
    "quest_claim": ("Claim a quest reward", 3, 3),
    "upgrade": ("Upgrade a player", 2, 1),
    "career_upgrade": ("Upgrade your Career Player", 2, 1),
    "trade": ("Complete a player or trait trade", 2, 1),
    "trait_apply": ("Apply a trait", 2, 1),
    "achievement": ("Unlock an achievement", 3, 2),
    "gspin": ("Claim a Lucky Card reward", 1, 1),
    "cmuweekly": ("Claim weekly rewards", 5, 1),
    "cmuchest": ("Open a reward chest", 3, 1),
}
ACTIVITY_ALIASES = {"trait_trade": "trade"}


def activity_key(action):
    return ACTIVITY_ALIASES.get(action, action)


def award_activity_points(session, user_id, action, *, event_key, now=None):
    """Credit one successful action once, subject to category and shared caps.

    The existing user row serializes awards in PostgreSQL. The unique event
    constraint is an additional replay guard. The ledger and points write are
    part of the caller's transaction, so a rollback consumes no allowance.
    """
    action = activity_key(action)
    if action not in ACTIVITY_RULES or not event_key:
        return 0
    from models import User, SeasonActivityAward
    from services.season_service import ensure_current_season, _credit_season_points

    live_key = ensure_current_season(session).season_key
    user = (session.query(User)
            .filter(User.id == user_id, User.telegram_id > 0,
                    User.is_banned.is_(False))
            .with_for_update().first())
    if user is None:
        return 0
    if (session.query(SeasonActivityAward.id)
            .filter(SeasonActivityAward.user_id == user_id,
                    SeasonActivityAward.event_key == event_key).first()):
        return 0

    now = now or datetime.utcnow()
    today = now.strftime("%Y-%m-%d")
    awards = (session.query(SeasonActivityAward.activity, SeasonActivityAward.points)
              .filter(SeasonActivityAward.user_id == user_id,
                      SeasonActivityAward.activity_date == today).all())
    _label, per_action, limit = ACTIVITY_RULES[action]
    if sum(1 for a, _p in awards if a == action) >= limit:
        return 0
    points = min(per_action, ACTIVITY_DAILY_CAP - sum(p for _a, p in awards))
    if points <= 0:
        return 0

    session.add(SeasonActivityAward(
        user_id=user_id, season_key=live_key, activity_date=today,
        activity=action, event_key=event_key, points=points, created_at=now))
    _credit_season_points(session, user, live_key, points=points)
    session.flush()
    return points


def get_activity_progress(session, user_id=None, *, now=None):
    """Rules and today's actual credited bonuses for the season screen."""
    from models import SeasonActivityAward
    today = (now or datetime.utcnow()).strftime("%Y-%m-%d")
    awards = []
    if user_id is not None:
        awards = (session.query(SeasonActivityAward.activity, SeasonActivityAward.points)
                  .filter(SeasonActivityAward.user_id == user_id,
                          SeasonActivityAward.activity_date == today).all())
    counts = Counter(a for a, _p in awards)
    earned = sum(p for _a, p in awards)
    return {
        "date": today, "earned": earned, "daily_cap": ACTIVITY_DAILY_CAP,
        "remaining": max(0, ACTIVITY_DAILY_CAP - earned),
        "activities": [
            {"key": key, "label": label, "points": points,
             "daily_limit": limit, "completed": counts[key]}
            for key, (label, points, limit) in ACTIVITY_RULES.items()],
    }

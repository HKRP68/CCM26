"""Player version upgrade.

Lets a user upgrade a roster card to a HIGHER version of the same player by
paying coins, instead of having to find/buy the higher version outright.

Example: you own 93 (Base) of a player and a 96 (IPL) version exists in the
catalog. You can upgrade your 93 entry into a 96 entry for:

    upgrade_value = buy_value(96) - buy_value(93)
    cost          = upgrade_value + fee_percent% of upgrade_value

The service fee (default 1%) makes it a genuine coin sink while still being
cheaper/easier than acquiring the higher version through packs or the market.

This works on the Player-version model: OVR lives on the Player row, so an
"upgrade" swaps the roster entry's player_id to the higher-version Player,
preserving the squad slot (order_position).
"""

import logging

from models import User, UserRoster, Player
from services.version_service import get_all_versions, get_base_id
from config import get_buy_value

logger = logging.getLogger(__name__)

# Service fee applied on top of the raw upgrade value, as a percent.
# 1 = 1% extra (the coin sink). Was 10% previously.
UPGRADE_FEE_PERCENT = 1
# Minimum cost so trivial +1 upgrades still cost something meaningful.
MIN_UPGRADE_COST = 1000


def _cost_breakdown(current_rating, target_rating):
    """Transparent pricing for a version upgrade.

    upgrade_value = buy_value(target) - buy_value(current)
    fee           = fee_percent% of upgrade_value
    cost          = upgrade_value + fee   (floored at MIN_UPGRADE_COST)

    Returns dict: {upgrade_value, fee, fee_percent, cost}.
    """
    upgrade_value = get_buy_value(target_rating) - get_buy_value(current_rating)
    if upgrade_value < 0:
        upgrade_value = 0
    fee = int(round(upgrade_value * UPGRADE_FEE_PERCENT / 100.0))
    cost = max(MIN_UPGRADE_COST, upgrade_value + fee)
    return {
        "upgrade_value": upgrade_value,
        "fee": fee,
        "fee_percent": UPGRADE_FEE_PERCENT,
        "cost": cost,
    }


def _upgrade_cost(current_rating, target_rating):
    return _cost_breakdown(current_rating, target_rating)["cost"]


def get_upgrade_options(session, user_id, roster_id):
    """For a given roster entry, return the higher versions it can upgrade to.

    Returns dict:
      {ok, current: {...}, options: [{player_id, version, rating, cost, affordable}], ...}
    """
    entry = (session.query(UserRoster)
             .filter(UserRoster.id == roster_id,
                     UserRoster.user_id == user_id).first())
    if not entry:
        return {"ok": False, "error": "not_found",
                "message": "That player isn't in your roster."}

    current = session.query(Player).get(entry.player_id)
    if not current:
        return {"ok": False, "error": "player_missing"}

    user = session.query(User).get(user_id)
    coins = user.total_coins or 0

    base_id = get_base_id(current.id, session)
    versions = get_all_versions(session, base_id)

    options = []
    for v in versions:
        if v.id == current.id:
            continue
        if v.rating <= current.rating:
            continue  # only higher-rated versions
        breakdown = _cost_breakdown(current.rating, v.rating)
        cost = breakdown["cost"]
        options.append({
            "player_id": v.id,
            "version": v.version or "Base",
            "rating": v.rating,
            "category": v.category,
            "cost": cost,
            "upgrade_value": breakdown["upgrade_value"],
            "fee": breakdown["fee"],
            "fee_percent": breakdown["fee_percent"],
            "affordable": coins >= cost,
        })

    # Cheapest / lowest-rating first so the "next step up" is on top
    options.sort(key=lambda o: o["rating"])

    return {
        "ok": True,
        "current": {
            "roster_id": entry.id,
            "player_id": current.id,
            "name": current.name,
            "version": current.version or "Base",
            "rating": current.rating,
            "category": current.category,
            "country": current.country,
        },
        "coins": coins,
        "options": options,
    }


def upgrade_player(session, user, roster_id, target_player_id):
    """Upgrade a roster entry to a higher version. Returns result dict.
    Caller commits."""
    entry = (session.query(UserRoster)
             .filter(UserRoster.id == roster_id,
                     UserRoster.user_id == user.id).first())
    if not entry:
        return {"ok": False, "error": "not_found",
                "message": "That player isn't in your roster."}

    current = session.query(Player).get(entry.player_id)
    target = session.query(Player).get(target_player_id)
    if not current or not target:
        return {"ok": False, "error": "player_missing",
                "message": "Player version not found."}

    # Must be the same base player
    if get_base_id(current.id, session) != get_base_id(target.id, session):
        return {"ok": False, "error": "different_player",
                "message": "You can only upgrade to a version of the same player."}

    if not target.is_active:
        return {"ok": False, "error": "inactive",
                "message": "That version isn't available."}

    if target.rating <= current.rating:
        return {"ok": False, "error": "not_higher",
                "message": "You can only upgrade to a higher-rated version."}

    breakdown = _cost_breakdown(current.rating, target.rating)
    cost = breakdown["cost"]
    if (user.total_coins or 0) < cost:
        return {"ok": False, "error": "insufficient_coins",
                "message": f"Need {cost:,} coins (you have {user.total_coins or 0:,}).",
                "cost": cost}

    # Apply: deduct coins, swap the roster entry's player to the target version
    old_name = f"{current.name} {current.version or 'Base'} ({current.rating})"
    new_name = f"{target.name} {target.version or 'Base'} ({target.rating})"

    user.total_coins = (user.total_coins or 0) - cost
    entry.player_id = target.id
    session.flush()

    # If this was the captain and we changed the player, captain stays on the
    # same roster entry (captain_roster_id unchanged) — no action needed.

    # Activity log
    try:
        from services.activity_service import log_activity
        log_activity(session, user.id, "upgrade",
                     f"Upgraded {old_name} → {new_name} for {cost} coins",
                     coins_change=-cost)
    except Exception:
        pass

    # Achievement check (may unlock high-OVR achievements)
    try:
        from services.achievement_service import safe_check_and_award
        safe_check_and_award(session, user.id)
    except Exception:
        pass

    return {
        "ok": True,
        "cost": cost,
        "upgrade_value": breakdown["upgrade_value"],
        "fee": breakdown["fee"],
        "fee_percent": breakdown["fee_percent"],
        "from": {"name": current.name, "version": current.version or "Base",
                 "rating": current.rating},
        "to": {"player_id": target.id, "name": target.name,
               "version": target.version or "Base", "rating": target.rating,
               "category": target.category, "country": target.country},
        "balance": {"coins": user.total_coins or 0},
    }

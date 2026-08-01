"""GSpin reward service — DB-backed weighted random reward picker.

The /gspin handler calls `pick_reward(session)` to get one row from the
GSpinReward table according to weighted probability. If the table is empty,
returns None so the handler can fall back to the legacy config-based outcomes.
"""

import random
import logging

from services.player_service import not_career

logger = logging.getLogger(__name__)


def pick_reward(session):
    """Pick one enabled GSpinReward row using weighted random selection.

    Returns the row, or None if no enabled rows exist (caller falls back to
    legacy GSPIN_OUTCOMES config).
    """
    from models import GSpinReward
    rows = (session.query(GSpinReward)
                   .filter(GSpinReward.enabled == True)
                   .order_by(GSpinReward.sort_order, GSpinReward.id)
                   .all())
    if not rows:
        return None

    total_weight = sum(max(1, r.weight or 1) for r in rows)
    if total_weight <= 0:
        return None

    r = random.random() * total_weight
    cumulative = 0
    for row in rows:
        cumulative += max(1, row.weight or 1)
        if r < cumulative:
            return row
    return rows[-1]  # safety fallback


def reward_probability(row, all_enabled_rows):
    """Helper for admin UI — compute the actual probability of a row firing."""
    total = sum(max(1, r.weight or 1) for r in all_enabled_rows
                if r.enabled)
    if total <= 0:
        return 0.0
    return max(1, row.weight or 1) / total


def apply_reward(session, user, reward, hold_overflow=False):
    """Apply a GSpinReward row to a user. Reusable from /gspin and Mini App.

    When ``hold_overflow`` is True and a rolled player can't fit a full roster,
    it's parked as a pending RosterOverflowClaim (Mini App Replace flow) and the
    claim is returned under ``overflow_claim`` instead of being discarded.
    """
    import random
    from datetime import datetime
    from models import UserRoster, Player

    out = {
        "type": reward.reward_type,
        "label": reward.label,
        "emoji": reward.emoji or "🎁",
        "color": reward.color or "888888",
        "amount": 0,
        "player_id": None,
        "player_name": None,
        "player_rating": None,
        "squad_full": False,
        "overflow_claim": None,
        "pack_id": None,
    }

    t = reward.reward_type

    if t == "coins":
        lo, hi = reward.amount_min or 0, reward.amount_max or 0
        if hi < lo: hi = lo
        amt = random.randint(lo, hi) if hi > 0 else 0
        user.total_coins = (user.total_coins or 0) + amt
        out["amount"] = amt

    elif t == "gems":
        lo, hi = reward.amount_min or 0, reward.amount_max or 0
        if hi < lo: hi = lo
        amt = random.randint(lo, hi) if hi > 0 else 0
        user.total_gems = (user.total_gems or 0) + amt
        out["amount"] = amt

    elif t == "quest_points":
        lo, hi = reward.amount_min or 0, reward.amount_max or 0
        if hi < lo: hi = lo
        amt = random.randint(lo, hi) if hi > 0 else 0
        user.quest_points = (user.quest_points or 0) + amt
        out["amount"] = amt

    elif t == "player":
        lo = reward.player_rating_min or 50
        hi = reward.player_rating_max or 100
        if hi < lo: hi = lo
        candidates = (not_career(session.query(Player))
                      .filter(Player.is_active == True,
                              Player.rating >= lo,
                              Player.rating <= hi)
                      .filter((Player.version == "Base") |
                              (Player.version.is_(None))))
        from services.version_service import user_owns_any_version
        all_in_range = candidates.all()
        unowned = [p for p in all_in_range
                   if not user_owns_any_version(session, user.id, p.id)]
        pool = unowned if unowned else all_in_range
        if not pool:
            amt = random.randint(5000, 10000)
            user.total_coins = (user.total_coins or 0) + amt
            out["type"] = "coins"
            out["label"] = "No players in range — coins instead"
            out["amount"] = amt
            return out
        player = random.choice(pool)
        out["player_id"] = player.id
        out["player_name"] = player.name
        out["player_rating"] = player.rating

        MAX_ROSTER = 25
        if (user.roster_count or 0) < MAX_ROSTER:
            entry = UserRoster(
                user_id=user.id, player_id=player.id,
                order_position=(user.roster_count or 0) + 1,
                acquired_date=datetime.utcnow(),
            )
            session.add(entry)
            user.roster_count = (user.roster_count or 0) + 1
            out["squad_full"] = False
        else:
            out["squad_full"] = True
            if hold_overflow:
                # Persist the pending claim; if it fails, propagate so the caller
                # rolls back the spin (and quota) instead of silently losing the
                # rolled player.
                from services.overflow_service import record_overflow
                out["overflow_claim"] = record_overflow(
                    session, user, player, source="gspin")

    elif t == "pack":
        out["pack_id"] = reward.pack_id

    return out

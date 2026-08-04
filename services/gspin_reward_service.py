"""GSpin reward service — DB-backed weighted random reward picker.

The /gspin handler calls `pick_reward(session)` to get one row from the
GSpinReward table according to weighted probability. If the table is empty,
returns None so the handler can fall back to the legacy config-based outcomes.

Mini App callers use `guaranteed_reward(session)` instead. There the spin has
usually just cost the player a rewarded ad, and "no rewards configured" —
whether that is an empty table, a disabled last row, or a bad weight — cannot be
allowed to end the request with nothing: the ad is already watched. So the
picker falls back to a plain coin reward built from the legacy config range,
and the spin always has something to land on.
"""

import random
import logging

from services.player_service import not_career

logger = logging.getLogger(__name__)

# Coin range for the fallback reward when nothing is configured. Read from
# config.GSPIN_OUTCOMES' coin band so the safety net pays what the legacy
# bot spin pays, rather than a number invented here.
FALLBACK_COINS = (1500, 3000)


class _FallbackReward:
    """Stand-in for a GSpinReward row, shaped for ``apply_reward``.

    Not persisted and never shown in the admin reward table — it exists only
    so a spin that has been paid for can still be honoured.
    """

    id = None
    reward_type = "coins"
    label = "Consolation Coins"
    emoji = "💰"
    color = "4facfe"
    pack_id = None
    player_rating_min = None
    player_rating_max = None
    weight = 1
    enabled = True

    def __init__(self, amount_min, amount_max):
        self.amount_min = amount_min
        self.amount_max = amount_max


def _weight_of(row):
    """A row's weight as a non-negative float.

    Weights are percentages set on the website and can be fractional, so they
    are never rounded or floored here — a reward configured at 0.00001% has to
    stay at 0.00001%. Zero is honoured as "parked": the reward keeps its row and
    its settings but never lands.
    """
    try:
        return max(0.0, float(row.weight or 0))
    except (TypeError, ValueError):
        return 0.0


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

    total_weight = sum(_weight_of(r) for r in rows)
    if total_weight <= 0:
        # Every enabled reward is parked at 0 — there is nothing to land on, so
        # let the caller fall back rather than picking one at random anyway.
        return None

    r = random.random() * total_weight
    cumulative = 0.0
    for row in rows:
        cumulative += _weight_of(row)
        if r < cumulative:
            return row
    return rows[-1]  # safety fallback


def fallback_reward():
    """The reward used when nothing is configured. Coins, always grantable."""
    lo, hi = FALLBACK_COINS
    try:
        from config import GSPIN_OUTCOMES
        for _cum, _colour, kind, amount_range in GSPIN_OUTCOMES:
            if kind == "coins" and amount_range:
                band_lo, band_hi = int(amount_range[0]), int(amount_range[1])
                # A band of (0, 0) is truthy and would hand back a reward worth
                # nothing — the one outcome this function exists to prevent, and
                # the player has already watched an ad for it. Keep the built-in
                # range instead and carry on looking.
                if max(band_lo, band_hi) > 0:
                    lo, hi = band_lo, band_hi
                    break
    except Exception:
        logger.exception("could not read the configured coin band")
    return _FallbackReward(max(1, lo), max(1, lo, hi))


def guaranteed_reward(session):
    """Pick a reward that is never None — for spins that have already been paid
    for with an ad. Falls back to coins when the reward table can't answer."""
    try:
        reward = pick_reward(session)
    except Exception:
        logger.exception("pick_reward failed — falling back to coins")
        reward = None
    if reward is not None:
        return reward
    logger.warning("No enabled GSpinReward rows — granting the fallback reward")
    return fallback_reward()


def reward_probability(row, all_enabled_rows):
    """Helper for admin UI — the actual chance of a row firing, as a fraction.

    Weights are relative, so a row's real probability only exists once the whole
    enabled column is normalised. Set weights that sum to 100 and each one reads
    back as its own percentage.
    """
    total = sum(_weight_of(r) for r in all_enabled_rows if r.enabled)
    if total <= 0:
        return 0.0
    return _weight_of(row) / total


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
        # Ratings the website has blocked for the spin never reach the wheel —
        # if that empties the band the reward pays coins instead, below.
        from services import rating_block_service
        block_clause = rating_block_service.rating_filter("gspin", session, Player)
        if block_clause is not None:
            candidates = candidates.filter(block_clause)
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

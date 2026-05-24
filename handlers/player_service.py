"""Player selection and value services.

Uses player_cache for random picks (zero egress for selection logic), then
fetches the single chosen ORM row by ID. Result: massive egress reduction.
"""

import random
from sqlalchemy import and_
from sqlalchemy.orm import Session

from models import Player
from config import CLAIM_RARITY, get_buy_value, get_sell_value


def get_random_player_by_rating_range(session: Session, low: int, high: int) -> Player | None:
    """Return a random active player within [low, high] rating.

    Implementation: uses in-memory cache to pick the ID, then fetches just that
    one row. Was previously fetching every player in range — wasteful.
    """
    from services import player_cache
    pick = player_cache.get_random_in_rating_range(low, high)
    if not pick:
        return None
    # Fetch single ORM row (cheap — single row, indexed lookup)
    return session.query(Player).get(pick["id"])


def _get_rarity_distribution(session: Session):
    """Return list of (cumulative_threshold, low, high) tuples.

    Tries the admin-configurable ClaimRarityTier table first.
    Falls back to CLAIM_RARITY from config.py if no rows exist.
    """
    try:
        from models import ClaimRarityTier
        rows = (session.query(ClaimRarityTier)
                .filter(ClaimRarityTier.is_active == True)
                .order_by(ClaimRarityTier.sort_order, ClaimRarityTier.id).all())
        if rows:
            # Build cumulative thresholds
            total = sum(r.probability for r in rows)
            if total <= 0:
                return CLAIM_RARITY
            # Normalize probabilities so they sum to 1.0 (allows admin to use percentages)
            cumulative = 0.0
            out = []
            for r in rows:
                cumulative += (r.probability / total)
                out.append((cumulative, r.rating_min, r.rating_max))
            return out
    except Exception:
        pass
    return CLAIM_RARITY


def get_random_player_by_rarity(session: Session) -> Player | None:
    """Pick a random player using the claim rarity distribution.
    Uses admin-configured tiers if any exist; otherwise falls back to config."""
    dist = _get_rarity_distribution(session)
    roll = random.random()
    for threshold, low, high in dist:
        if roll <= threshold:
            return get_random_player_by_rating_range(session, low, high)
    # If we somehow fall off the end, use the last tier's range
    if dist:
        _, low, high = dist[-1]
        return get_random_player_by_rating_range(session, low, high)
    return get_random_player_by_rating_range(session, 50, 58)


def get_player_values(rating: int) -> tuple[int, int]:
    """Return (buy_value, sell_value) for a rating."""
    return get_buy_value(rating), get_sell_value(rating)


def get_players_for_debut(session: Session) -> list[Player]:
    """Return 8 players for debut: 1x83-85, 3x75-80, 4x50-74."""
    result: list[Player] = []
    seen_ids: set[int] = set()

    def pick(low, high, count):
        pool = (
            session.query(Player)
            .filter(and_(Player.rating >= low, Player.rating <= high, Player.is_active == True))
            .all()
        )
        pool = [p for p in pool if p.id not in seen_ids]
        random.shuffle(pool)
        chosen = pool[:count]
        for p in chosen:
            seen_ids.add(p.id)
        return chosen

    result.extend(pick(83, 85, 1))
    result.extend(pick(75, 80, 3))
    result.extend(pick(50, 74, 4))
    return result

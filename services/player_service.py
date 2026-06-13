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


def _owned_player_ids(session: Session, user_id) -> set:
    """player_ids already in the user's roster (cheap — max 25 rows)."""
    from models import UserRoster
    return {r[0] for r in session.query(UserRoster.player_id)
            .filter(UserRoster.user_id == user_id).all()}


def get_random_unowned_player_by_rating_range(
    session: Session, user_id, low: int, high: int,
    *, exclude_ids=None, attempts: int = 12) -> Player | None:
    """Like get_random_player_by_rating_range but never returns a player the
    user already owns (or any id in exclude_ids).

    A user can hold at most one row per player (unique constraint), so reward
    flows must not try to grant a duplicate. Returns None if no unowned player
    is found within `attempts` tries — callers fall back to coins.
    """
    from services import player_cache
    skip = set(exclude_ids or ())
    skip |= _owned_player_ids(session, user_id)
    for _ in range(attempts):
        pick = player_cache.get_random_in_rating_range(low, high)
        if not pick:
            return None
        if pick["id"] in skip:
            continue
        return session.query(Player).get(pick["id"])
    return None


def get_random_unowned_player_by_rarity(
    session: Session, user_id, *, exclude_ids=None, attempts: int = 12) -> Player | None:
    """Rarity-weighted pick that skips players the user already owns."""
    dist = _get_rarity_distribution(session)
    roll = random.random()
    low, high = 50, 58
    matched = False
    for threshold, lo, hi in dist:
        if roll <= threshold:
            low, high, matched = lo, hi, True
            break
    if not matched and dist:
        _, low, high = dist[-1]
    return get_random_unowned_player_by_rating_range(
        session, user_id, low, high, exclude_ids=exclude_ids, attempts=attempts)


def get_player_values(rating: int) -> tuple[int, int]:
    """Return (buy_value, sell_value) for a rating."""
    return get_buy_value(rating), get_sell_value(rating)


def get_players_for_debut(session: Session) -> list[Player]:
    """Return a balanced 11-player starter squad.

    The squad always targets 5 batsmen, 3 bowlers, 2 all-rounders and one
    wicket keeper. One randomly selected role receives an 83-85 OVR card;
    the other ten cards are 72-80 OVR. If a role-specific pool is short, no
    partial squad is returned: an admin should fix the player seed instead.
    """
    role_slots = [
        "Batsman", "Batsman", "Batsman", "Batsman", "Batsman",
        "Bowler", "Bowler", "Bowler",
        "All-rounder", "All-rounder",
        "Wicket Keeper",
    ]
    star_slot = random.randrange(len(role_slots))
    result: list[Player] = []
    seen_ids: set[int] = set()

    def pick_one(low: int, high: int, category: str | None = None) -> Player | None:
        query = session.query(Player).filter(
            and_(Player.rating >= low, Player.rating <= high,
                 Player.is_active == True, ~Player.id.in_(seen_ids))
        )
        if category:
            query = query.filter(Player.category == category)
        pool = query.all()
        if not pool:
            return None
        player = random.choice(pool)
        seen_ids.add(player.id)
        return player

    for index, category in enumerate(role_slots):
        low, high = ((83, 85) if index == star_slot else (72, 80))
        player = pick_one(low, high, category)
        if not player:
            # Do not grant an unbalanced squad. A partial seed should be fixed
            # by an admin instead of silently giving a new user the wrong XI.
            return []
        result.append(player)

    return result

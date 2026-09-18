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
    return session.get(Player, pick["id"])


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


def _pick_in_range_strict(session: Session, low: int, high: int) -> Player | None:
    """Pick a random active player strictly inside [low, high] — no widening.

    Unlike get_random_player_by_rating_range (which progressively widens the
    band and ultimately returns ANY active player when the band is empty), this
    helper never leaks outside the requested band. That keeps the website-set
    rarity weights authoritative for /claim and /daily.
    """
    from services import player_cache
    pool = []
    for r in range(low, high + 1):
        pool.extend(player_cache.get_by_rating(r))
    if not pool:
        return None
    pick = random.choice(pool)
    return session.get(Player, pick["id"])


def get_random_player_by_rarity(session: Session) -> Player | None:
    """Pick a random player using the website Claim Rarity distribution.

    Uses the admin-configured ClaimRarityTier rows when present (otherwise the
    CLAIM_RARITY fallback). The chosen band is honoured strictly: if it has no
    eligible players we re-roll among the OTHER configured bands (weighted by
    their probabilities) rather than widening into arbitrary ratings, so the
    rarity set on the website is the rarity players actually receive.
    """
    dist = _get_rarity_distribution(session)
    if not dist:
        return get_random_player_by_rating_range(session, 50, 58)

    # Convert cumulative thresholds back to per-band weights.
    bands = []
    prev = 0.0
    for threshold, low, high in dist:
        bands.append([max(0.0, threshold - prev), low, high])
        prev = threshold

    # Weighted draw without replacement: respect the configured rarity, but if a
    # band is empty fall through to the next-most-likely configured band.
    remaining = [b for b in bands if b[0] > 0] or [list(b) for b in bands]
    while remaining:
        total = sum(b[0] for b in remaining)
        if total <= 0:
            break
        roll = random.random() * total
        acc = 0.0
        chosen = remaining[-1]
        for band in remaining:
            acc += band[0]
            if roll <= acc:
                chosen = band
                break
        remaining.remove(chosen)
        player = _pick_in_range_strict(session, chosen[1], chosen[2])
        if player:
            return player

    # Nothing in any configured band (shouldn't happen) — last-resort widening
    # so /claim and /daily never fail outright.
    _, low, high = dist[-1]
    return get_random_player_by_rating_range(session, low, high)


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

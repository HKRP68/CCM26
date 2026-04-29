"""Player selection and value services."""

import random
from sqlalchemy import and_
from sqlalchemy.orm import Session

from models import Player
from config import CLAIM_RARITY, get_buy_value, get_sell_value


def get_random_player_by_rating_range(session: Session, low: int, high: int) -> Player | None:
    """Return a random active player within [low, high] rating.
    If the exact range is empty, gradually widen until players are found."""
    players = (
        session.query(Player)
        .filter(and_(Player.rating >= low, Player.rating <= high, Player.is_active == True))
        .all()
    )
    if players:
        return random.choice(players)

    # Widen range up to ±10 to find the nearest players
    for expand in range(1, 11):
        players = (
            session.query(Player)
            .filter(and_(
                Player.rating >= max(50, low - expand),
                Player.rating <= min(100, high + expand),
                Player.is_active == True,
            ))
            .all()
        )
        if players:
            return random.choice(players)

    # Absolute fallback: any active player
    all_players = session.query(Player).filter(Player.is_active == True).all()
    return random.choice(all_players) if all_players else None


def get_random_player_by_rarity(session: Session) -> Player | None:
    """Pick a random player using the claim rarity distribution."""
    roll = random.random()
    for threshold, low, high in CLAIM_RARITY:
        if roll <= threshold:
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

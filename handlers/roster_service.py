"""Roster management: stats, release, duplicates."""

import logging
from collections import Counter
from sqlalchemy import func
from sqlalchemy.orm import Session

from models import User, Player, UserRoster
from config import get_sell_value, get_buy_value, MAX_ROSTER

logger = logging.getLogger(__name__)


def get_user_roster(session: Session, user_id: int, page: int = 1, page_size: int = 10):
    """Return paginated roster sorted by order_position (as added).
    Returns (entries_with_player, total_count, total_pages).
    """
    total = (
        session.query(UserRoster)
        .filter(UserRoster.user_id == user_id)
        .count()
    )
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))

    entries = (
        session.query(UserRoster, Player)
        .join(Player, UserRoster.player_id == Player.id)
        .filter(UserRoster.user_id == user_id)
        .order_by(UserRoster.order_position.asc(), UserRoster.acquired_date.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return entries, total, total_pages


def get_roster_stats(session: Session, user_id: int) -> dict:
    """Calculate roster summary stats."""
    entries = (
        session.query(UserRoster, Player)
        .join(Player, UserRoster.player_id == Player.id)
        .filter(UserRoster.user_id == user_id)
        .all()
    )
    if not entries:
        return {"total_value": 0, "avg_rating": 0, "duplicates": 0, "count": 0}

    ratings = [p.rating for _, p in entries]
    total_value = sum(get_sell_value(r) for r in ratings)
    avg_rating = round(sum(ratings) / len(ratings), 1)

    # Count duplicate player_ids
    pid_counts = Counter(e.player_id for e, _ in entries)
    duplicates = sum(1 for c in pid_counts.values() if c > 1)

    return {
        "total_value": total_value,
        "avg_rating": avg_rating,
        "duplicates": duplicates,
        "count": len(entries),
    }


def get_duplicate_entries(session: Session, user_id: int):
    """Return list of (player, quantity) for players owned more than once."""
    subq = (
        session.query(
            UserRoster.player_id,
            func.count(UserRoster.id).label("qty"),
        )
        .filter(UserRoster.user_id == user_id)
        .group_by(UserRoster.player_id)
        .having(func.count(UserRoster.id) > 1)
        .subquery()
    )
    rows = (
        session.query(Player, subq.c.qty)
        .join(subq, Player.id == subq.c.player_id)
        .order_by(Player.rating.desc())
        .all()
    )
    return rows  # list of (Player, qty)


def release_player(session: Session, user: User, roster_entry_id: int) -> dict:
    """Release one roster entry. Returns {success, name, sell_value, new_balance}."""
    entry = session.query(UserRoster).filter(
        UserRoster.id == roster_entry_id,
        UserRoster.user_id == user.id,
    ).first()

    if not entry:
        return {"success": False, "error": "You don't own this player"}

    player = session.query(Player).get(entry.player_id)
    sell_val = get_sell_value(player.rating)

    session.delete(entry)
    user.total_coins += sell_val
    user.roster_count = max(0, user.roster_count - 1)
    session.flush()

    logger.info(f"Release: user {user.telegram_id} released {player.name} ({player.rating}) for {sell_val}")
    return {
        "success": True,
        "name": player.name,
        "rating": player.rating,
        "sell_value": sell_val,
        "new_balance": user.total_coins,
        "new_count": user.roster_count,
    }


def release_player_by_name(session: Session, user: User, player_name: str) -> dict:
    """Release one instance of a player by name search."""
    entry = (
        session.query(UserRoster)
        .join(Player, UserRoster.player_id == Player.id)
        .filter(UserRoster.user_id == user.id, Player.name.ilike(f"%{player_name}%"))
        .first()
    )
    if not entry:
        return {"success": False, "error": f"No player named '{player_name}' in your roster"}
    return release_player(session, user, entry.id)


def release_duplicates(session: Session, user: User, player_id: int, count: int) -> dict:
    """Release `count` instances of a duplicate player."""
    entries = (
        session.query(UserRoster)
        .filter(UserRoster.user_id == user.id, UserRoster.player_id == player_id)
        .order_by(UserRoster.acquired_date.desc())
        .limit(count)
        .all()
    )
    if not entries:
        return {"success": False, "error": "Player not found in roster"}

    player = session.query(Player).get(player_id)
    sell_each = get_sell_value(player.rating)
    total_sell = sell_each * len(entries)

    for e in entries:
        session.delete(e)
    user.total_coins += total_sell
    user.roster_count = max(0, user.roster_count - len(entries))
    session.flush()

    logger.info(f"Release duplicates: user {user.telegram_id} released {len(entries)}x {player.name} for {total_sell}")
    return {
        "success": True,
        "name": player.name,
        "rating": player.rating,
        "released_count": len(entries),
        "total_value": total_sell,
        "new_balance": user.total_coins,
        "new_count": user.roster_count,
    }


def find_roster_entry(session: Session, user_id: int, player_name: str):
    """Find a specific roster entry by player name. Returns (UserRoster, Player) or (None, None)."""
    result = (
        session.query(UserRoster, Player)
        .join(Player, UserRoster.player_id == Player.id)
        .filter(UserRoster.user_id == user_id, Player.name.ilike(f"%{player_name}%"))
        .first()
    )
    return result if result else (None, None)

"""Find same-rating players between users for trading."""

import logging
from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from models import User, Player, UserRoster, Trade
from config import TRADE_MIN_RATING, TRADE_FEE_PERCENT, get_buy_value

logger = logging.getLogger(__name__)


def get_tradeable_ratings(session: Session, user_id: int) -> list[int]:
    """Get unique ratings >= TRADE_MIN_RATING in user's roster, sorted desc."""
    rows = (
        session.query(Player.rating)
        .join(UserRoster, UserRoster.player_id == Player.id)
        .filter(UserRoster.user_id == user_id, Player.rating >= TRADE_MIN_RATING)
        .distinct()
        .order_by(Player.rating.desc())
        .all()
    )
    return [r[0] for r in rows]


def get_players_at_rating(session: Session, user_id: int, rating: int):
    """Return list of (UserRoster, Player) at exact rating for a user."""
    return (
        session.query(UserRoster, Player)
        .join(Player, UserRoster.player_id == Player.id)
        .filter(UserRoster.user_id == user_id, Player.rating == rating)
        .order_by(Player.name)
        .all()
    )


def find_matching_ratings(session: Session, user1_id: int, user2_id: int) -> list[int]:
    """Find ratings that both users have tradeable players for."""
    r1 = set(get_tradeable_ratings(session, user1_id))
    r2 = set(get_tradeable_ratings(session, user2_id))
    return sorted(r1 & r2, reverse=True)


def get_trade_fee(rating: int) -> int:
    """Calculate 5% trade fee based on buy value."""
    return int(get_buy_value(rating) * TRADE_FEE_PERCENT / 100)


def can_trade_with_user(session: Session, initiator: User, receiver: User, rating: int) -> dict:
    """Validate all trading rules. Returns {can_trade, reason}."""
    if initiator.id == receiver.id:
        return {"can_trade": False, "reason": "You cannot trade with yourself"}

    if rating < TRADE_MIN_RATING:
        return {"can_trade": False, "reason": f"Only players rated {TRADE_MIN_RATING}+ OVR can be traded"}

    # Check initiator has player at this rating
    init_count = (
        session.query(UserRoster)
        .join(Player, UserRoster.player_id == Player.id)
        .filter(UserRoster.user_id == initiator.id, Player.rating == rating)
        .count()
    )
    if init_count == 0:
        return {"can_trade": False, "reason": f"You have no players rated {rating} OVR"}

    # Check receiver has player at this rating
    recv_count = (
        session.query(UserRoster)
        .join(Player, UserRoster.player_id == Player.id)
        .filter(UserRoster.user_id == receiver.id, Player.rating == rating)
        .count()
    )
    if recv_count == 0:
        return {"can_trade": False, "reason": f"@{receiver.username} has no players rated {rating} OVR"}

    # Check no pending trades
    pending = (
        session.query(Trade)
        .filter(
            Trade.status == "pending",
            ((Trade.initiator_id == initiator.id) | (Trade.receiver_id == initiator.id)),
        )
        .count()
    )
    if pending > 0:
        return {"can_trade": False, "reason": "You already have a pending trade. Cancel it first"}

    # Check both can afford fee
    fee = get_trade_fee(rating)
    if initiator.total_coins < fee:
        return {"can_trade": False, "reason": f"You need {fee:,} coins for the trade fee"}
    if receiver.total_coins < fee:
        return {"can_trade": False, "reason": f"@{receiver.username} can't afford the {fee:,} coin trade fee"}

    return {"can_trade": True, "reason": "OK"}

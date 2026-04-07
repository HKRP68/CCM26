"""Player trading: initiate, accept, reject, expire."""

import logging
from datetime import datetime, timedelta
from sqlalchemy.orm import Session

from models import User, Player, UserRoster, Trade
from config import TRADE_EXPIRES_SECONDS, MAX_ACTIVE_TRADES
from services.rating_matcher_service import get_trade_fee
from services.activity_service import log_activity

logger = logging.getLogger(__name__)


def expire_stale_trades(session: Session):
    """Auto-expire any pending trades past their expiry time."""
    now = datetime.utcnow()
    stale = (
        session.query(Trade)
        .filter(Trade.status == "pending", Trade.expires_at < now)
        .all()
    )
    for t in stale:
        t.status = "expired"
        t.updated_at = now
    if stale:
        session.flush()
        logger.info(f"Auto-expired {len(stale)} stale trades")


def initiate_trade(
    session: Session,
    initiator: User,
    receiver: User,
    initiator_roster_id: int,
    receiver_roster_id: int,
) -> dict:
    """Create a pending trade. Returns {success, trade_id, fee, message}."""
    expire_stale_trades(session)

    # Re-check no active trade for initiator
    pending = (
        session.query(Trade)
        .filter(
            Trade.status == "pending",
            ((Trade.initiator_id == initiator.id) | (Trade.receiver_id == initiator.id)),
        )
        .count()
    )
    if pending >= MAX_ACTIVE_TRADES:
        return {"success": False, "message": "You already have a pending trade"}

    # Validate roster entries still exist
    init_entry = session.query(UserRoster).filter(
        UserRoster.id == initiator_roster_id, UserRoster.user_id == initiator.id
    ).first()
    recv_entry = session.query(UserRoster).filter(
        UserRoster.id == receiver_roster_id, UserRoster.user_id == receiver.id
    ).first()

    if not init_entry:
        return {"success": False, "message": "You no longer own this player"}
    if not recv_entry:
        return {"success": False, "message": f"@{receiver.username} no longer owns that player"}

    init_player = session.query(Player).get(init_entry.player_id)
    recv_player = session.query(Player).get(recv_entry.player_id)

    if init_player.rating != recv_player.rating:
        return {"success": False, "message": "Players must have the same rating to trade"}

    fee = get_trade_fee(init_player.rating)
    if initiator.total_coins < fee:
        return {"success": False, "message": f"You need {fee:,} coins for the trade fee"}
    if receiver.total_coins < fee:
        return {"success": False, "message": f"@{receiver.username} can't afford the {fee:,} coin fee"}

    now = datetime.utcnow()
    trade = Trade(
        initiator_id=initiator.id,
        receiver_id=receiver.id,
        initiator_player_id=init_player.id,
        receiver_player_id=recv_player.id,
        initiator_roster_id=initiator_roster_id,
        receiver_roster_id=receiver_roster_id,
        status="pending",
        trade_fee=fee,
        created_at=now,
        expires_at=now + timedelta(seconds=TRADE_EXPIRES_SECONDS),
    )
    session.add(trade)
    session.flush()

    logger.info(
        f"Trade #{trade.id} initiated: {initiator.telegram_id} offers "
        f"{init_player.name}({init_player.rating}) ↔ {recv_player.name}({recv_player.rating}) "
        f"to {receiver.telegram_id}, fee={fee}"
    )
    return {
        "success": True,
        "trade_id": trade.id,
        "fee": fee,
        "init_player": init_player,
        "recv_player": recv_player,
        "message": "Trade offer sent",
    }


def accept_trade(session: Session, trade_id: int, user: User) -> dict:
    """Accept a pending trade. Swap players, deduct fees."""
    expire_stale_trades(session)

    trade = session.query(Trade).get(trade_id)
    if not trade:
        return {"success": False, "message": "Trade not found"}
    if trade.status != "pending":
        return {"success": False, "message": f"Trade is already {trade.status}"}
    if trade.receiver_id != user.id:
        return {"success": False, "message": "Only the receiver can accept this trade"}

    now = datetime.utcnow()
    if trade.expires_at < now:
        trade.status = "expired"
        session.flush()
        return {"success": False, "message": "Trade offer has expired"}

    initiator = session.query(User).get(trade.initiator_id)
    receiver = user

    # Verify both roster entries still exist
    init_entry = session.query(UserRoster).filter(
        UserRoster.id == trade.initiator_roster_id,
        UserRoster.user_id == initiator.id,
    ).first()
    recv_entry = session.query(UserRoster).filter(
        UserRoster.id == trade.receiver_roster_id,
        UserRoster.user_id == receiver.id,
    ).first()

    if not init_entry:
        trade.status = "expired"
        session.flush()
        return {"success": False, "message": "Trade failed: initiator no longer owns the player"}
    if not recv_entry:
        trade.status = "expired"
        session.flush()
        return {"success": False, "message": "Trade failed: you no longer own the player"}

    fee = trade.trade_fee
    if initiator.total_coins < fee:
        trade.status = "expired"
        session.flush()
        return {"success": False, "message": "Trade failed: initiator can't afford the fee"}
    if receiver.total_coins < fee:
        return {"success": False, "message": f"You need {fee:,} coins for the trade fee"}

    # Swap ownership
    init_entry.user_id = receiver.id
    recv_entry.user_id = initiator.id

    # Deduct fees
    initiator.total_coins -= fee
    receiver.total_coins -= fee

    trade.status = "completed"
    trade.completed_at = now
    session.flush()

    init_player = session.query(Player).get(trade.initiator_player_id)
    recv_player = session.query(Player).get(trade.receiver_player_id)

    log_activity(session, initiator.id, 'trade', f'Traded {init_player.name} → got {recv_player.name}', coins_change=-fee, player_name=init_player.name, player_rating=init_player.rating)
    log_activity(session, receiver.id, 'trade', f'Traded {recv_player.name} → got {init_player.name}', coins_change=-fee, player_name=recv_player.name, player_rating=recv_player.rating)
    session.flush()

    logger.info(
        f"Trade #{trade.id} completed: {initiator.telegram_id} gave {init_player.name}, "
        f"got {recv_player.name}. Fee {fee} each."
    )
    return {
        "success": True,
        "trade": trade,
        "initiator": initiator,
        "receiver": receiver,
        "init_player": init_player,
        "recv_player": recv_player,
        "fee": fee,
        "message": "Trade completed",
    }


def reject_trade(session: Session, trade_id: int, user: User) -> dict:
    """Reject or cancel a pending trade."""
    trade = session.query(Trade).get(trade_id)
    if not trade:
        return {"success": False, "message": "Trade not found"}
    if trade.status != "pending":
        return {"success": False, "message": f"Trade is already {trade.status}"}
    if trade.receiver_id != user.id and trade.initiator_id != user.id:
        return {"success": False, "message": "You are not part of this trade"}

    trade.status = "rejected"
    trade.updated_at = datetime.utcnow()
    session.flush()

    logger.info(f"Trade #{trade.id} rejected/cancelled by {user.telegram_id}")
    return {"success": True, "trade": trade, "message": "Trade cancelled"}


def get_pending_trade_for_user(session: Session, user_id: int):
    """Return the pending Trade for a user, or None."""
    expire_stale_trades(session)
    return (
        session.query(Trade)
        .filter(
            Trade.status == "pending",
            ((Trade.initiator_id == user_id) | (Trade.receiver_id == user_id)),
        )
        .first()
    )

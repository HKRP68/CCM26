"""Player trading services for exact same-OVR swaps."""

import logging
from datetime import datetime, timedelta
from sqlalchemy.orm import Session

from config import TRADE_EXPIRES_SECONDS
from models import Player, Trade, User, UserRoster
from services.activity_service import log_activity
from services.rating_matcher_service import (
    get_players_at_rating,
    is_player_locked,
    is_player_non_tradable,
)

logger = logging.getLogger(__name__)


def expire_stale_trades(session: Session):
    """Auto-expire DB pending trades past their expiry time."""
    now = datetime.utcnow()
    stale = session.query(Trade).filter(Trade.status == "pending", Trade.expires_at < now).all()
    for trade in stale:
        trade.status = "expired"
        trade.updated_at = now
    if stale:
        session.flush()
        logger.info("Auto-expired %d stale trades", len(stale))


def _get_owned_available_entry(session: Session, user_id: int, roster_id: int):
    entry = (
        session.query(UserRoster)
        .filter(UserRoster.id == roster_id, UserRoster.user_id == user_id)
        .first()
    )
    if not entry:
        return None, None
    player = session.query(Player).get(entry.player_id)
    if not player or is_player_locked(entry, player) or is_player_non_tradable(player):
        return None, None
    if not any(e.id == entry.id for e, _ in get_players_at_rating(session, user_id, player.rating)):
        return None, None
    return entry, player


def _validate_same_ovr_swap(
    session: Session,
    initiator: User,
    receiver: User,
    initiator_roster_id: int,
    receiver_roster_id: int,
):
    init_entry, init_player = _get_owned_available_entry(session, initiator.id, initiator_roster_id)
    recv_entry, recv_player = _get_owned_available_entry(session, receiver.id, receiver_roster_id)
    if not init_entry or not recv_entry:
        return None, None, None, None, "Trade cancelled because one player is no longer available."
    if init_player.rating != recv_player.rating:
        return None, None, None, None, "Trade cancelled because both players no longer have the same OVR."
    return init_entry, init_player, recv_entry, recv_player, None


def create_pending_trade(
    session: Session,
    initiator: User,
    receiver: User,
    initiator_roster_id: int,
    receiver_roster_id: int,
) -> dict:
    """Persist a pending trade after both users have selected same-OVR cards."""
    expire_stale_trades(session)
    init_entry, init_player, recv_entry, recv_player, error = _validate_same_ovr_swap(
        session, initiator, receiver, initiator_roster_id, receiver_roster_id
    )
    if error:
        return {"success": False, "message": error}

    now = datetime.utcnow()
    trade = Trade(
        initiator_id=initiator.id,
        receiver_id=receiver.id,
        initiator_player_id=init_player.id,
        receiver_player_id=recv_player.id,
        initiator_roster_id=init_entry.id,
        receiver_roster_id=recv_entry.id,
        status="pending",
        trade_fee=0,
        created_at=now,
        expires_at=now + timedelta(seconds=TRADE_EXPIRES_SECONDS),
        updated_at=now,
    )
    session.add(trade)
    session.flush()
    return {
        "success": True,
        "trade": trade,
        "trade_id": trade.id,
        "init_player": init_player,
        "recv_player": recv_player,
        "message": "Trade confirmation started",
    }


def complete_trade(session: Session, trade_id: int) -> dict:
    """Final safety check and same-OVR player swap."""
    expire_stale_trades(session)
    trade = session.query(Trade).get(trade_id)
    if not trade:
        return {"success": False, "message": "Trade not found"}
    if trade.status != "pending":
        return {"success": False, "message": "Trade cancelled because it was already completed or cancelled."}
    now = datetime.utcnow()
    if trade.expires_at < now:
        trade.status = "expired"
        trade.updated_at = now
        session.flush()
        return {"success": False, "message": "Trade expired."}

    initiator = session.query(User).get(trade.initiator_id)
    receiver = session.query(User).get(trade.receiver_id)
    init_entry, init_player, recv_entry, recv_player, error = _validate_same_ovr_swap(
        session, initiator, receiver, trade.initiator_roster_id, trade.receiver_roster_id
    )
    if error:
        trade.status = "cancelled"
        trade.updated_at = now
        session.flush()
        return {"success": False, "message": error}

    # Traits do NOT travel with a traded player. Each side's equipped traits go
    # back to the inventory of the captain who owned them (at their current
    # level) before the roster rows change hands — otherwise the trait would
    # keep boosting a player its buyer never paid gems for, and PlayerTrait rows
    # would be left pointing at another user's squad.
    from services.trait_service import return_traits_to_inventory
    traits_returned = return_traits_to_inventory(
        session, [init_entry.id, recv_entry.id])

    init_entry.user_id = receiver.id
    recv_entry.user_id = initiator.id
    trade.status = "completed"
    trade.completed_at = now
    trade.updated_at = now

    log_activity(
        session,
        initiator.id,
        "trade",
        f"Received {recv_player.name} | OVR {recv_player.rating}; gave {init_player.name} | OVR {init_player.rating}",
        player_name=recv_player.name,
        player_rating=recv_player.rating,
    )
    log_activity(
        session,
        receiver.id,
        "trade",
        f"Received {init_player.name} | OVR {init_player.rating}; gave {recv_player.name} | OVR {recv_player.rating}",
        player_name=init_player.name,
        player_rating=init_player.rating,
    )
    session.flush()
    logger.info(
        "Trade #%s completed: %s gave %s, %s gave %s",
        trade.id,
        initiator.telegram_id,
        init_player.name,
        receiver.telegram_id,
        recv_player.name,
    )
    return {
        "success": True,
        "trade": trade,
        "initiator": initiator,
        "receiver": receiver,
        "init_player": init_player,
        "recv_player": recv_player,
        "traits_returned": traits_returned,
        "message": "Trade completed",
    }


# Legacy API wrappers kept for existing imports/tests.
def initiate_trade(session: Session, initiator: User, receiver: User, initiator_roster_id: int, receiver_roster_id: int) -> dict:
    return create_pending_trade(session, initiator, receiver, initiator_roster_id, receiver_roster_id)


def accept_trade(session: Session, trade_id: int, user: User) -> dict:
    trade = session.query(Trade).get(trade_id)
    if not trade:
        return {"success": False, "message": "Trade not found"}
    if trade.receiver_id != user.id:
        return {"success": False, "message": "Only the receiver can accept this trade"}
    return complete_trade(session, trade_id)


def reject_trade(session: Session, trade_id: int, user: User) -> dict:
    trade = session.query(Trade).get(trade_id)
    if not trade:
        return {"success": False, "message": "Trade not found"}
    if trade.status != "pending":
        return {"success": False, "message": f"Trade is already {trade.status}"}
    if trade.receiver_id != user.id and trade.initiator_id != user.id:
        return {"success": False, "message": "You are not part of this trade"}
    trade.status = "cancelled"
    trade.updated_at = datetime.utcnow()
    session.flush()
    return {"success": True, "trade": trade, "message": "Trade cancelled"}


def get_pending_trade_for_user(session: Session, user_id: int):
    expire_stale_trades(session)
    return (
        session.query(Trade)
        .filter(
            Trade.status == "pending",
            Trade.expires_at > datetime.utcnow(),
            ((Trade.initiator_id == user_id) | (Trade.receiver_id == user_id)),
        )
        .first()
    )

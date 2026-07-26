"""Player trading services for exact same-OVR swaps.

Trades are not free: each side pays a fee of ``TRADE_FEE_PERCENT`` of the traded
player's buy value (1%). Both cards have the same OVR — that is the rule the
whole flow is built on — so both captains pay the same amount, and the coins
leave the economy rather than moving between the two.
"""

import logging
from datetime import datetime, timedelta
from sqlalchemy.orm import Session

from config import TRADE_EXPIRES_SECONDS, TRADE_FEE_PERCENT, get_buy_value
from models import Player, Trade, User, UserRoster
from services.activity_service import log_activity
from services.rating_matcher_service import (
    get_players_at_rating,
    is_player_locked,
    is_player_non_tradable,
)

logger = logging.getLogger(__name__)


def trade_fee_for(rating) -> int:
    """Coins each side pays to trade a card of this OVR.

    ``TRADE_FEE_PERCENT`` of the player's buy value, rounded up so a cheap card
    still costs something rather than being free by rounding.
    """
    try:
        buy_value = int(get_buy_value(int(rating)))
    except (TypeError, ValueError):
        return 0
    if buy_value <= 0:
        return 0
    return max(1, -(-buy_value * TRADE_FEE_PERCENT // 100))   # ceil division


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

    # Both cards are the same OVR, so one fee figure covers both captains. It is
    # quoted on the confirmation card and charged again (re-checked) on
    # completion — the balance can move while the prompt is on screen.
    fee = trade_fee_for(init_player.rating)
    short = _cannot_afford(initiator, receiver, fee)
    if short:
        return {"success": False, "message": short, "fee": fee}

    now = datetime.utcnow()
    trade = Trade(
        initiator_id=initiator.id,
        receiver_id=receiver.id,
        initiator_player_id=init_player.id,
        receiver_player_id=recv_player.id,
        initiator_roster_id=init_entry.id,
        receiver_roster_id=recv_entry.id,
        status="pending",
        trade_fee=fee,
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
        "fee": fee,
        "message": "Trade confirmation started",
    }


def _user_label(user) -> str:
    if not user:
        return "A player"
    return f"@{user.username}" if getattr(user, "username", None) else (
        getattr(user, "first_name", None) or "A player")


def _cannot_afford(initiator: User, receiver: User, fee: int):
    """Message naming whoever can't cover the trade fee, or ``None``."""
    if fee <= 0:
        return None
    broke = [u for u in (initiator, receiver) if (u.total_coins or 0) < fee]
    if not broke:
        return None
    who = " and ".join(_user_label(u) for u in broke)
    return (f"Trade cancelled — {who} can't cover the {fee:,} 🪙 trade fee "
            f"({TRADE_FEE_PERCENT}% of the card's buy value).")


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

    # Charge the fee before the cards change hands — re-priced and re-checked
    # here because a balance can be spent while the confirmation is on screen.
    fee = trade_fee_for(init_player.rating)
    short = _cannot_afford(initiator, receiver, fee)
    if short:
        trade.status = "cancelled"
        trade.updated_at = now
        session.flush()
        return {"success": False, "message": short, "fee": fee}
    if fee > 0:
        for side in (initiator, receiver):
            side.total_coins = (side.total_coins or 0) - fee
    trade.trade_fee = fee

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

    fee_note = f" (fee {fee:,} coins)" if fee else ""
    log_activity(
        session,
        initiator.id,
        "trade",
        f"Received {recv_player.name} | OVR {recv_player.rating}; gave {init_player.name} | OVR {init_player.rating}{fee_note}",
        coins_change=-fee if fee else 0,
        player_name=recv_player.name,
        player_rating=recv_player.rating,
    )
    log_activity(
        session,
        receiver.id,
        "trade",
        f"Received {init_player.name} | OVR {init_player.rating}; gave {recv_player.name} | OVR {recv_player.rating}{fee_note}",
        coins_change=-fee if fee else 0,
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
        "fee": fee,
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

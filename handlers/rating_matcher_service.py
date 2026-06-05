"""Find same-rating players between users for trading."""

import logging
from datetime import datetime
from sqlalchemy.orm import Session

from models import Match, MatchState, Player, Trade, User, UserRoster

logger = logging.getLogger(__name__)

ACTIVE_MATCH_STATUSES = ("pending", "accepted", "toss", "selecting", "playing", "active")


def _truthy_attr(obj, names):
    for name in names:
        if hasattr(obj, name) and bool(getattr(obj, name)):
            return True
    return False


def _falsey_attr(obj, names):
    for name in names:
        if hasattr(obj, name) and not bool(getattr(obj, name)):
            return True
    return False


def _active_match_roster_ids(session: Session, user_id: int | None = None) -> set[int]:
    """Roster IDs currently referenced by active matches."""
    query = session.query(MatchState.state_json).join(Match, Match.id == MatchState.match_id)
    query = query.filter(Match.status.in_(ACTIVE_MATCH_STATUSES))
    if user_id is not None:
        query = query.filter((Match.user1_id == user_id) | (Match.user2_id == user_id))

    roster_ids: set[int] = set()
    for (state_json,) in query.all():
        if not state_json:
            continue
        try:
            import json
            state = json.loads(state_json)
        except Exception:
            continue
        for key in ("bat_xi", "bowl_xi", "inn1_bat_xi", "inn1_bowl_xi", "batting_order"):
            for player in state.get(key) or []:
                try:
                    rid = int(player.get("roster_id"))
                except (TypeError, ValueError, AttributeError):
                    continue
                if rid > 0:
                    roster_ids.add(rid)
    return roster_ids


def is_player_locked(entry: UserRoster, player: Player) -> bool:
    """Return True if a roster entry/player has a lock-like flag.

    The production schema may add flags without requiring this module to be
    migrated in lockstep, so this deliberately checks common names dynamically.
    """
    return _truthy_attr(entry, ("is_locked", "locked", "trade_locked")) or _truthy_attr(
        player, ("is_locked", "locked", "trade_locked")
    )


def is_player_non_tradable(player: Player) -> bool:
    """Return True for inactive, event-only, or explicitly non-tradable cards."""
    if hasattr(player, "is_active") and not player.is_active:
        return True
    return _truthy_attr(player, ("event_only", "is_event_only", "non_tradable")) or _falsey_attr(
        player, ("is_tradable", "tradable", "tradeable")
    )


def get_tradable_players(session: Session, user_id: int, rating: int | None = None):
    """Return (UserRoster, Player) rows available for trade.

    Excludes locked cards, cards already in active matches, inactive/event-only
    cards, and explicitly non-tradable cards. If ``rating`` is supplied, only
    exact OVR matches are returned.
    """
    active_roster_ids = _active_match_roster_ids(session, user_id)
    rows = (
        session.query(UserRoster, Player)
        .join(Player, UserRoster.player_id == Player.id)
        .filter(UserRoster.user_id == user_id)
        .order_by(Player.rating.desc(), Player.name.asc(), UserRoster.id.asc())
        .all()
    )
    tradable = []
    for entry, player in rows:
        if rating is not None and player.rating != rating:
            continue
        if entry.id in active_roster_ids:
            continue
        if is_player_locked(entry, player):
            continue
        if is_player_non_tradable(player):
            continue
        tradable.append((entry, player))
    return tradable


def get_tradeable_ratings(session: Session, user_id: int) -> list[int]:
    """Get unique exact OVR ratings for currently tradable cards."""
    return sorted({player.rating for _, player in get_tradable_players(session, user_id)}, reverse=True)


def get_players_at_rating(session: Session, user_id: int, rating: int):
    """Return tradable (UserRoster, Player) rows at exact OVR rating."""
    return get_tradable_players(session, user_id, rating)


def find_matching_ratings(session: Session, user1_id: int, user2_id: int) -> list[int]:
    """Find exact OVR ratings both users can trade."""
    r1 = set(get_tradeable_ratings(session, user1_id))
    r2 = set(get_tradeable_ratings(session, user2_id))
    return sorted(r1 & r2, reverse=True)


def get_trade_fee(rating: int) -> int:
    """Trades no longer charge coins; keep legacy API returning zero."""
    return 0


def can_trade_with_user(session: Session, initiator: User, receiver: User, rating: int | None = None) -> dict:
    """Validate broad trade availability without charging a fee."""
    if initiator.id == receiver.id:
        return {"can_trade": False, "reason": "You cannot trade with yourself"}
    pending = (
        session.query(Trade)
        .filter(
            Trade.status == "pending",
            ((Trade.initiator_id == initiator.id) | (Trade.receiver_id == initiator.id)),
            Trade.expires_at > datetime.utcnow(),
        )
        .count()
    )
    if pending:
        return {"can_trade": False, "reason": "You already have a pending trade. Cancel it first"}
    if rating is not None:
        if not get_players_at_rating(session, initiator.id, rating):
            return {"can_trade": False, "reason": f"You have no tradable players rated {rating} OVR"}
        if not get_players_at_rating(session, receiver.id, rating):
            return {"can_trade": False, "reason": f"@{receiver.username} has no tradable players rated {rating} OVR"}
    return {"can_trade": True, "reason": "OK"}

"""Roster-overflow "pending claim" logic for the Mini App.

When a Mini App reward (daily / free pack / pack / gspin) rolls a player but the
user's roster is already full (25/25), the player is parked in a
``RosterOverflowClaim`` row instead of being discarded. The Mini App can then
offer a Replace flow — mirroring the bot's /claim replace — where the user picks
a roster player to drop so the pending player takes its slot, or releases the
pending player for coins.

Security: the replace/release endpoints only ever act on a pending claim that
belongs to the calling user, so a client can never grant itself an arbitrary
player id. Claims are single-use (deleted once resolved) and pruned after a
short TTL so stale rows can't pile up.
"""

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# How long a pending overflow claim stays valid before it's pruned.
OVERFLOW_TTL_SECONDS = 3600  # 1 hour


def _sell_value(rating):
    try:
        from config import get_sell_value
        return int(get_sell_value(rating))
    except Exception:
        return 0


def _is_expired(claim):
    """True if a pending claim is older than the TTL."""
    return bool(
        claim.created_at
        and (datetime.utcnow() - claim.created_at).total_seconds() > OVERFLOW_TTL_SECONDS
    )


def _lock_claim(session, user, claim_id):
    """Fetch a user's pending claim, row-locking it so two concurrent
    replace/release requests can't both redeem the same claim_id.

    ``with_for_update`` is a no-op on SQLite (dev/tests) and a real ``SELECT …
    FOR UPDATE`` on Postgres: the second request blocks until the first commits,
    then re-reads and finds the row gone (returns None → "claim_not_found").
    """
    from models import RosterOverflowClaim
    q = (session.query(RosterOverflowClaim)
         .filter(RosterOverflowClaim.id == claim_id,
                 RosterOverflowClaim.user_id == user.id))
    try:
        return q.with_for_update().first()
    except Exception:
        # Backend/dialect without row-locking support — fall back to a plain read.
        return q.first()


def prune_overflow(session, user_id):
    """Delete expired pending claims for a user. Caller commits."""
    from models import RosterOverflowClaim
    cutoff = datetime.utcnow() - timedelta(seconds=OVERFLOW_TTL_SECONDS)
    try:
        (session.query(RosterOverflowClaim)
         .filter(RosterOverflowClaim.user_id == user_id,
                 RosterOverflowClaim.created_at < cutoff)
         .delete(synchronize_session=False))
    except Exception:
        logger.exception("prune_overflow failed")


def record_overflow(session, user, player, source="reward"):
    """Park an overflow player as a pending claim. Caller commits.

    Returns a dict describing the pending claim (including ``claim_id``), safe
    to embed in an API response.
    """
    from models import RosterOverflowClaim
    sell_value = _sell_value(player.rating)
    row = RosterOverflowClaim(
        user_id=user.id, player_id=player.id,
        source=source, sell_value=sell_value,
        created_at=datetime.utcnow(),
    )
    session.add(row)
    session.flush()  # populate row.id
    return {
        "claim_id": row.id,
        "id": player.id,
        "name": player.name,
        "rating": player.rating,
        "sell_value": sell_value,
        "source": source,
    }


def list_overflow(session, user):
    """Return the user's active pending claims (prunes expired ones first)."""
    from models import RosterOverflowClaim, Player
    prune_overflow(session, user.id)
    rows = (session.query(RosterOverflowClaim, Player)
            .join(Player, RosterOverflowClaim.player_id == Player.id)
            .filter(RosterOverflowClaim.user_id == user.id)
            .order_by(RosterOverflowClaim.created_at.asc())
            .all())
    out = []
    for claim, player in rows:
        out.append({
            "claim_id": claim.id,
            "id": player.id,
            "name": player.name,
            "rating": player.rating,
            "sell_value": claim.sell_value,
            "source": claim.source,
        })
    return out


def resolve_replace(session, user, claim_id, old_roster_id):
    """Swap a roster player for a pending overflow player. Caller commits.

    Drops the roster entry ``old_roster_id`` and re-uses its slot for the
    pending player (keeping the order_position, like the bot's /claim replace).

    Returns a result dict; on failure sets ``ok=False`` + ``error``/``message``.
    """
    from models import RosterOverflowClaim, UserRoster, Player

    claim = _lock_claim(session, user, claim_id)
    if not claim:
        return {"ok": False, "error": "claim_not_found",
                "message": "That reward player is no longer pending."}

    # Expired?
    if _is_expired(claim):
        session.delete(claim)
        return {"ok": False, "error": "expired",
                "message": "This reward expired. Try the reward again."}

    new_player = session.query(Player).get(claim.player_id)
    if not new_player:
        session.delete(claim)
        return {"ok": False, "error": "player_missing",
                "message": "That player is no longer available."}

    old_entry = (session.query(UserRoster)
                 .filter(UserRoster.id == old_roster_id,
                         UserRoster.user_id == user.id).first())
    if not old_entry:
        return {"ok": False, "error": "roster_not_found",
                "message": "That squad player isn't in your roster."}

    # Don't let the captain be dropped without re-assigning first.
    if user.captain_roster_id == old_entry.id:
        return {"ok": False, "error": "is_captain",
                "message": "Can't replace your captain. Set a new captain first."}

    # Prevent ending up with two copies of the same cricketer.
    from services.version_service import user_owns_any_version
    if user_owns_any_version(session, user.id, new_player.id):
        # Still consume the claim so it doesn't linger.
        session.delete(claim)
        return {"ok": False, "error": "already_owned",
                "message": f"You already own a version of {new_player.name}."}

    old_player = session.query(Player).get(old_entry.player_id)
    old_name = old_player.name if old_player else "Unknown"
    old_rating = old_player.rating if old_player else 0
    source = claim.source

    # The dropped player's traits come off before the slot is re-used, so they
    # return to the owner's inventory instead of silently moving onto the
    # incoming player (the roster row itself survives the swap).
    from services.trait_service import return_traits_to_inventory
    traits_returned = return_traits_to_inventory(session, old_entry.id)

    # Re-use the dropped slot for the pending player.
    old_entry.player_id = new_player.id
    old_entry.acquired_date = datetime.utcnow()
    session.delete(claim)

    try:
        from services.activity_service import log_activity
        log_activity(session, user.id, "replace",
                     f"[MiniApp/{source}] Replaced {old_name} with {new_player.name}",
                     player_name=new_player.name, player_rating=new_player.rating)
    except Exception:
        logger.exception("overflow replace log_activity failed")

    return {
        "ok": True,
        "old_player": {"name": old_name, "rating": old_rating},
        "new_player": {"id": new_player.id, "name": new_player.name,
                       "rating": new_player.rating},
        "traits_returned": traits_returned,
        "roster_count": user.roster_count or 0,
    }


def resolve_release(session, user, claim_id):
    """Release a pending overflow player for its sell value. Caller commits."""
    from models import RosterOverflowClaim, Player

    claim = _lock_claim(session, user, claim_id)
    if not claim:
        return {"ok": False, "error": "claim_not_found",
                "message": "That reward player is no longer pending."}

    # Reject stale claims (same TTL guard as resolve_replace) so a full-roster
    # release can't credit coins for an expired reward.
    if _is_expired(claim):
        session.delete(claim)
        return {"ok": False, "error": "expired",
                "message": "This reward expired. Try the reward again."}

    player = session.query(Player).get(claim.player_id)
    name = player.name if player else "Player"
    rating = player.rating if player else 0
    sell_value = claim.sell_value or _sell_value(rating)
    source = claim.source

    user.total_coins = (user.total_coins or 0) + sell_value
    session.delete(claim)

    try:
        from services.activity_service import log_activity
        log_activity(session, user.id, "release",
                     f"[MiniApp/{source}] Released pending {name} ({rating}) for {sell_value:,}",
                     coins_change=sell_value, player_name=name, player_rating=rating)
    except Exception:
        logger.exception("overflow release log_activity failed")

    return {
        "ok": True,
        "sell_value": sell_value,
        "player_name": name,
        "player_rating": rating,
        "coins": user.total_coins or 0,
    }

"""Undo service for /cmuundo command.

Records the most-recent buy/release per user. The /cmuundo handler reads
this row and reverses the action, provided it's within the 60-second
window AND the reversal is actually possible (player still in roster /
roster has space, etc.).
"""

import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# How long the undo window stays open
UNDO_WINDOW_SECONDS = 60


def _valid_payload(action, payload):
    """Reject corrupt reversal amounts instead of turning them into credits."""
    def nonnegative(value):
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    def player_id(value):
        return nonnegative(value) and value > 0

    if not isinstance(payload, dict):
        return False
    if action == "buy":
        return (player_id(payload.get("roster_id"))
                and player_id(payload.get("player_id"))
                and nonnegative(payload.get("price"))
                and nonnegative(payload.get("gem_bonus", 0)))
    if action == "release":
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            return False
        if not all(isinstance(item, dict)
                   and player_id(item.get("player_id"))
                   and nonnegative(item.get("price")) for item in items):
            return False
        ids = [item["player_id"] for item in items]
        refund = payload.get("total_refund")
        return (len(set(ids)) == len(ids) and nonnegative(refund)
                and refund == sum(item["price"] for item in items))
    return True  # the handler reports and clears an unknown action


def record_buy(session, user_id, *, roster_id, player_id, player_name,
                rating, price, gem_bonus=0, activity_id=None):
    """Record a buy that can be undone within the window.

    ``gem_bonus`` is the elite-signing rebate paid on this buy (0 for most
    cards). The undo has to take those gems back too, or a buy-and-undo loop
    prints gems for free.
    """
    from models import PendingUndo
    now = datetime.utcnow()
    payload = json.dumps({
        "roster_id": int(roster_id),
        "player_id": int(player_id),
        "player_name": str(player_name),
        "rating": int(rating),
        "price": int(price),
        "gem_bonus": int(gem_bonus or 0),
        "activity_id": int(activity_id) if activity_id is not None else None,
    })
    # Upsert — one row per user
    row = (session.query(PendingUndo)
           .filter(PendingUndo.user_id == user_id)
           .first())
    if row:
        row.action_type = "buy"
        row.payload = payload
        row.created_at = now
        row.expires_at = now + timedelta(seconds=UNDO_WINDOW_SECONDS)
    else:
        row = PendingUndo(
            user_id=user_id,
            action_type="buy",
            payload=payload,
            created_at=now,
            expires_at=now + timedelta(seconds=UNDO_WINDOW_SECONDS),
        )
        session.add(row)


def record_release(session, user_id, *, items, total_refund):
    """Record a release (single or multi) that can be undone within the window.

    items: list of dicts {player_id, player_name, rating, price}
    """
    from models import PendingUndo
    now = datetime.utcnow()
    payload = json.dumps({
        "items": [
            {
                "player_id": int(it["player_id"]),
                "player_name": str(it["player_name"]),
                "rating": int(it["rating"]),
                "price": int(it["price"]),
            }
            for it in items
        ],
        "total_refund": int(total_refund),
    })
    row = (session.query(PendingUndo)
           .filter(PendingUndo.user_id == user_id)
           .first())
    if row:
        row.action_type = "release"
        row.payload = payload
        row.created_at = now
        row.expires_at = now + timedelta(seconds=UNDO_WINDOW_SECONDS)
    else:
        row = PendingUndo(
            user_id=user_id,
            action_type="release",
            payload=payload,
            created_at=now,
            expires_at=now + timedelta(seconds=UNDO_WINDOW_SECONDS),
        )
        session.add(row)


def get_pending(session, user_id):
    """Get the current undo row if still valid. Returns (row, payload) or
    (None, None) if no row or expired."""
    from models import PendingUndo
    row = (session.query(PendingUndo)
           .filter(PendingUndo.user_id == user_id)
           .populate_existing()
           .with_for_update()
           .first())
    if not row:
        return None, None
    if row.expires_at and row.expires_at <= datetime.utcnow():
        # Expired — clean it up
        consume_pending(session, row, require_unexpired=False)
        return None, None
    try:
        payload = json.loads(row.payload)
        if not _valid_payload(row.action_type, payload):
            raise ValueError("invalid undo payload")
        return row, payload
    except (ValueError, TypeError):
        consume_pending(session, row, require_unexpired=False)
        return None, None


def consume_pending(session, row, *, require_unexpired=True):
    """Claim this exact undo once, in the reversal's transaction.

    Checking that a row exists and deleting it after paying is unsafe: two
    requests can both read the same row and both refund. Match the complete
    snapshot so a stale request also cannot consume a newer buy/release that
    replaced the user's single pending row. A later failure rolls this delete
    back along with the reversal, leaving the undo available for a retry.
    """
    from models import PendingUndo
    query = session.query(PendingUndo).filter(
        PendingUndo.id == row.id,
        PendingUndo.user_id == row.user_id,
        PendingUndo.action_type == row.action_type,
        PendingUndo.payload == row.payload,
        PendingUndo.created_at == row.created_at,
        PendingUndo.expires_at == row.expires_at,
    )
    if require_unexpired:
        query = query.filter(PendingUndo.expires_at > datetime.utcnow())
    return query.delete(synchronize_session=False) == 1


def clear_pending(session, user_id):
    """Delete the user's undo row (after a successful undo)."""
    from models import PendingUndo
    (session.query(PendingUndo)
            .filter(PendingUndo.user_id == user_id)
            .delete(synchronize_session=False))


def seconds_remaining(row):
    """Helper for display."""
    if not row or not row.expires_at:
        return 0
    delta = (row.expires_at - datetime.utcnow()).total_seconds()
    return max(0, int(delta))

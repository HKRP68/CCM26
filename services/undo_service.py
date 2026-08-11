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


def record_buy(session, user_id, *, roster_id, player_id, player_name,
                rating, price, gem_bonus=0):
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
           .first())
    if not row:
        return None, None
    if row.expires_at and row.expires_at < datetime.utcnow():
        # Expired — clean it up
        try:
            session.delete(row)
            session.flush()
        except Exception:
            pass
        return None, None
    try:
        return row, json.loads(row.payload)
    except (ValueError, TypeError):
        return None, None


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

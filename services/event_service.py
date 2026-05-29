"""Time-limited events.

Currently supports a coin-multiplier effect: while a 'coin_multiplier' event is
active, coin rewards across the game can be boosted by calling
apply_coin_multiplier(). Other event types are display-only for now.
"""

import logging
from datetime import datetime

from models import Event

logger = logging.getLogger(__name__)


def get_active_events(session):
    """Return all currently-active events (within their window)."""
    now = datetime.utcnow()
    try:
        return (session.query(Event)
                .filter(Event.is_active == True,
                        Event.start_at <= now,
                        Event.end_at >= now)
                .order_by(Event.end_at.asc())
                .all())
    except Exception:
        logger.exception("get_active_events failed")
        return []


def get_coin_multiplier(session):
    """Return the highest active coin multiplier (1.0 if none)."""
    mult = 1.0
    for e in get_active_events(session):
        if e.event_type == "coin_multiplier" and e.multiplier and e.multiplier > mult:
            mult = float(e.multiplier)
    return mult


def apply_coin_multiplier(session, base_coins):
    """Apply the active coin multiplier to a base coin amount.
    Returns (final_coins, multiplier_used)."""
    if not base_coins:
        return base_coins, 1.0
    mult = get_coin_multiplier(session)
    if mult <= 1.0:
        return base_coins, 1.0
    return int(round(base_coins * mult)), mult


def get_events_for_display(session):
    """Return active events as client-friendly dicts."""
    out = []
    now = datetime.utcnow()
    for e in get_active_events(session):
        remaining = int((e.end_at - now).total_seconds()) if e.end_at else 0
        out.append({
            "id": e.id,
            "name": e.name,
            "description": e.description or "",
            "emoji": e.emoji or "🎉",
            "event_type": e.event_type,
            "multiplier": e.multiplier,
            "ends_at": e.end_at.isoformat() if e.end_at else None,
            "remaining_seconds": max(0, remaining),
        })
    return out

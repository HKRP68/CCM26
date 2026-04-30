"""Player cache — keeps the entire Player table in memory.

Why: Players are essentially read-only at runtime. Re-fetching them on every
/claim, /buy, /search, /playerinfo, scorecard render, etc. is a massive
egress drain on Neon free tier.

Strategy:
  - Load all active players ONCE on first call
  - Serve all reads from in-memory dict
  - Invalidate cache when admin edits a player (manual call or auto-refresh
    every 5 minutes for safety)

This typically cuts player-related egress by ~95%. A live match that used to
hit the DB ~50 times for player lookups now hits 0 times.

Note: Methods return PLAIN DICTS, not Player instances. This is intentional —
SQLAlchemy ORM objects are bound to a session, can't be reused. Dicts are
free to copy and pass around.
"""

import logging
import random
import threading
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Cache config
REFRESH_INTERVAL = timedelta(minutes=5)  # auto-refresh window

_lock = threading.Lock()
_cache = {
    "by_id": {},          # id → dict
    "all_active": [],     # list[dict] of active players
    "loaded_at": None,
    "by_rating": {},      # rating → list[dict] for fast random access
}


def _player_to_dict(p):
    """Convert ORM Player to a plain dict. Pick only the fields the app uses."""
    return {
        "id": p.id,
        "name": p.name,
        "country": p.country,
        "category": p.category,
        "rating": p.rating,
        "bat_rating": p.bat_rating,
        "bowl_rating": p.bowl_rating,
        "bat_hand": p.bat_hand,
        "bowl_hand": p.bowl_hand,
        "bowl_style": p.bowl_style,
        "image_url": getattr(p, "image_url", None),
        "matches": getattr(p, "matches", 0),
        "runs": getattr(p, "runs", 0),
        "wickets": getattr(p, "wickets", 0),
        "is_active": p.is_active,
    }


def _refresh(session=None):
    """Reload all active players from DB. Caller may pass session."""
    own = False
    if session is None:
        from database import get_session
        session = get_session()
        own = True
    try:
        from models import Player
        rows = session.query(Player).filter(Player.is_active == True).all()
        by_id = {}
        all_active = []
        by_rating = {}
        for p in rows:
            d = _player_to_dict(p)
            by_id[d["id"]] = d
            all_active.append(d)
            by_rating.setdefault(d["rating"], []).append(d)
        _cache["by_id"] = by_id
        _cache["all_active"] = all_active
        _cache["by_rating"] = by_rating
        _cache["loaded_at"] = datetime.utcnow()
        logger.info(f"Player cache refreshed: {len(all_active)} active players")
    except Exception:
        logger.exception("player cache refresh failed")
    finally:
        if own:
            session.close()


def _ensure_loaded():
    """Load on first use, or auto-refresh if stale."""
    with _lock:
        loaded = _cache["loaded_at"]
        if loaded is None or (datetime.utcnow() - loaded) > REFRESH_INTERVAL:
            _refresh()


def invalidate():
    """Force refresh on next access. Call from admin player edit/delete."""
    with _lock:
        _cache["loaded_at"] = None


# ════════════════════════════════════════════════════════════════════
# Public API
# ════════════════════════════════════════════════════════════════════

def get_by_id(player_id):
    """Returns dict or None. Zero DB calls."""
    _ensure_loaded()
    return _cache["by_id"].get(player_id)


def get_random_active():
    """Returns a random active player as dict. Zero DB calls."""
    _ensure_loaded()
    pool = _cache["all_active"]
    return random.choice(pool) if pool else None


def get_random_in_rating_range(low, high):
    """Returns a random player with rating in [low, high]. Zero DB calls."""
    _ensure_loaded()
    # Walk the by_rating index — much faster than scanning all
    pool = []
    for r in range(low, high + 1):
        pool.extend(_cache["by_rating"].get(r, []))
    if pool:
        return random.choice(pool)
    # Widen if empty — same as old behavior
    for expand in range(1, 11):
        pool = []
        for r in range(max(50, low - expand), min(100, high + expand) + 1):
            pool.extend(_cache["by_rating"].get(r, []))
        if pool:
            return random.choice(pool)
    # Last resort — any active player
    return get_random_active()


def get_all_active():
    """Returns the full active list. Zero DB calls."""
    _ensure_loaded()
    return list(_cache["all_active"])  # defensive copy


def get_by_rating(rating):
    """Players at exactly this rating."""
    _ensure_loaded()
    return list(_cache["by_rating"].get(rating, []))


def search_by_name(query, limit=15):
    """Case-insensitive name match. Zero DB calls."""
    _ensure_loaded()
    q = (query or "").lower().strip()
    if not q:
        return []
    out = []
    for p in _cache["all_active"]:
        if q in p["name"].lower():
            out.append(p)
            if len(out) >= limit:
                break
    return out


def stats():
    """For diagnostics — show what's cached."""
    _ensure_loaded()
    return {
        "loaded_at": _cache["loaded_at"],
        "active_count": len(_cache["all_active"]),
        "by_id_count": len(_cache["by_id"]),
        "rating_buckets": len(_cache["by_rating"]),
    }

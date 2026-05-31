"""Game config service — admin-tunable economy values.

Reads from the GameConfig table (single-row). If no row exists, returns
the baked-in defaults. Cache invalidated by save_config().
"""

import logging
from datetime import datetime

from models import GameConfig

logger = logging.getLogger(__name__)


# Default values — used if no row in DB
DEFAULTS = {
    "match_win_coins_per_over": 300,
    "match_win_gems_per_over": 1.0,
    "match_loss_coins_per_over": 150,
    "match_loss_gems_per_over": 0.5,
    "gspin_gem_min": 5,
    "gspin_gem_max": 50,
    "daily_coins": 1000,
    "daily_gems": 0,
    "daily_streak_bonus_coins": 200,
    "daily_streak_bonus_gems": 0,
    "spin_ad_quota": 5,
    "daily_ad_quota": 5,
    "debut_coins": 100000,
    "debut_gems": 20,
    # Simulation tuning — applied as additive % points after all modifiers.
    # The default values fix the "too many dots, not enough singles" feedback.
    "sim_dot_adjust": -8.0,
    "sim_one_adjust": 5.0,
    "sim_two_adjust": 2.0,
    "sim_four_adjust": 0.0,
    "sim_six_adjust": 0.0,
    "sim_wicket_adjust": 0.0,
    "sim_extras_adjust": 0.0,
    # Player market
    "market_min_rating": 87,
    "market_default_slots": 6,
    "market_refresh_hour_ist": 0,
    "trait_market_default_slots": 5,
    # Scorecard accent colors (hex strings)
    "scorecard_color_inn1": "#c41e3a",
    "scorecard_color_inn2": "#00c9a7",
    # Maintenance mode
    "is_maintenance": False,
    "maintenance_message": None,
    "maintenance_until": None,
    "maintenance_started_at": None,
    "maintenance_bypass_ids": None,
    # Match gameplay style. Telegram restores the original in-chat buttons;
    # webapp opts every newly started match into the Mini App board.
    "match_style": "telegram",
}


MATCH_STYLES = {"telegram", "webapp"}


def get_match_style(session=None):
    """Return the global gameplay style for newly started matches.

    Unknown or missing values safely fall back to the original Telegram flow.
    """
    # Refresh instead of using the process-local cache: the admin website and
    # Telegram bot commonly run as separate processes, and a website save must
    # affect the next match without requiring a bot restart.
    style = str(_refresh(session).get("match_style") or "telegram").lower()
    return style if style in MATCH_STYLES else "telegram"


# Module-level cache (refreshed when admin saves)
_CACHE = {"data": None, "loaded_at": None}


def get_config(session=None):
    """Return current config as a dict. Cheap — uses cache.

    If session is None, opens its own short-lived session.
    """
    if _CACHE["data"] is not None:
        return _CACHE["data"]
    return _refresh(session)


def _refresh(session=None):
    """Load fresh from DB into cache."""
    own = False
    if session is None:
        from database import get_session
        session = get_session()
        own = True
    try:
        row = session.query(GameConfig).first()
        if row:
            data = {k: getattr(row, k) for k in DEFAULTS.keys()}
        else:
            data = dict(DEFAULTS)
        _CACHE["data"] = data
        _CACHE["loaded_at"] = datetime.utcnow()
        return data
    except Exception:
        logger.exception("config refresh failed; falling back to defaults")
        return dict(DEFAULTS)
    finally:
        if own:
            session.close()


def save_config(session, updates, updated_by=None):
    """Update config values. updates is a dict of column→value pairs."""
    row = session.query(GameConfig).first()
    if not row:
        row = GameConfig()
        session.add(row)
        session.flush()
    for k, v in updates.items():
        if k in DEFAULTS and v is not None:
            setattr(row, k, v)
    row.updated_at = datetime.utcnow()
    if updated_by:
        row.updated_by = updated_by
    session.flush()
    # Invalidate cache so next get_config re-reads
    _CACHE["data"] = None
    return row


def reset_to_defaults(session, updated_by=None):
    """Reset all config to defaults."""
    return save_config(session, DEFAULTS, updated_by=updated_by)

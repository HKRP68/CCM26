"""Community Arena configuration — admin-tunable, single-row.

Mirrors ``services/config_service.py`` and ``services/giveaway_auto`` in spirit:
a single :class:`models.CommunityConfig` row holds two master switches and a
``settings_json`` blob. The blob is deep-merged over the baked-in ``DEFAULTS``
below, so a fresh install (or any key an admin has never touched) uses the
spec's default, and every value remains editable from the admin panel.

Everything funnels through :func:`get_config`, which caches the merged dict for
a few seconds to keep Neon usage low — a busy group can field thousands of
messages without re-reading this row per message.
"""

from __future__ import annotations

import copy
import json
import logging
from time import monotonic

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 5

GAME_TYPES = ("mystery", "oddoneout", "wordle", "fastestfingers", "rating", "cricketer")

# ── Baked-in defaults (the spec's default configuration) ────────────────
DEFAULTS = {
    # Overall daily CXP ceiling across every activity.
    "max_daily_xp": 400,

    # XP awarded + per-day reward limit per earning activity.
    "xp": {
        "chat":           {"xp": 1,  "daily_limit": 50},
        "pvp_match":      {"xp": 15, "daily_limit": 10},
        "ai_match":       {"xp": 5,  "daily_limit": 5},
        "claim":          {"xp": 5,  "daily_limit": 5},
        "checkin":        {"xp": 20, "daily_limit": 1},
    },

    # Chat XP anti-spam rules.
    "chat": {
        "min_chars": 8,
        "cooldown_seconds": 45,
        "daily_limit": 50,
    },

    # Per-game settings. ``reward`` is the winner's CXP; ``daily_win_limit`` 0
    # means unlimited rewarded wins of that type per IST day.
    "games": {
        "mystery":        {"enabled": True, "duration": 60,  "reward": 30, "blur": 30,
                           "daily_win_limit": 0, "progressive_clues": True},
        "oddoneout":      {"enabled": True, "duration": 30,  "reward": 20, "daily_win_limit": 0},
        "wordle":         {"enabled": True, "duration": 120, "reward": 40, "max_attempts": 6,
                           "min_len": 4, "max_len": 8, "daily_win_limit": 0},
        "fastestfingers": {"enabled": True, "duration": 30,  "reward": 20, "daily_win_limit": 0},
        "rating":         {"enabled": True, "duration": 45,  "reward": 25, "max_attempts": 3,
                           "daily_win_limit": 0},
        "cricketer":      {"enabled": True, "duration": 60,  "reward": 30, "difficulty": "medium",
                           "daily_win_limit": 0},
    },

    # Automatic engagement scheduler.
    "scheduler": {
        "active_start_hour": 8,      # IST hour games may start (inclusive)
        "active_end_hour": 24,       # IST hour games stop (exclusive; 24 = midnight)
        "max_games_per_day": 8,
        "min_gap_minutes": 45,
        "inactivity_minutes": 30,
        # Launch a random game every N minutes regardless of chat activity
        # (still bounded by active hours, max/day, min gap and match protection).
        # 0 disables the interval trigger and leaves only the inactivity one.
        "interval_minutes": 60,
        "match_protection": True,    # don't launch while a protected match is live
        "recent_player_exclusion": 15,   # players to keep out of rotation
        "recent_type_exclusion": 1,      # game types to avoid repeating
        "weights": {
            "mystery": 20, "oddoneout": 15, "wordle": 10,
            "fastestfingers": 20, "rating": 15, "cricketer": 20,
        },
    },

    # Which players the games may draw from (empty list = no restriction).
    "eligible": {
        "rating_min": 0,
        "rating_max": 100,
        "countries": [],
        "categories": [],
        "versions": [],
        "exclude_career": True,
    },

    # Answer validation.
    "fuzzy_matching": False,
}


# ── Cache ───────────────────────────────────────────────────────────────
_CACHE = {"data": None, "loaded_at": 0.0}


def _deep_merge(base: dict, override: dict) -> dict:
    """Return a deep copy of ``base`` with ``override`` merged in (override
    wins). Only dicts recurse; lists and scalars replace wholesale."""
    out = copy.deepcopy(base)
    for key, val in (override or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def _load_row(session):
    from models import CommunityConfig
    return session.query(CommunityConfig).order_by(CommunityConfig.id).first()


def _refresh(session=None):
    own = False
    if session is None:
        from database import get_session
        session = get_session()
        own = True
    try:
        row = _load_row(session)
        stored = {}
        enabled = False
        auto_enabled = False
        if row is not None:
            enabled = bool(row.enabled)
            auto_enabled = bool(row.auto_enabled)
            if row.settings_json:
                try:
                    stored = json.loads(row.settings_json) or {}
                except (ValueError, TypeError):
                    logger.warning("community settings_json invalid; using defaults")
                    stored = {}
        merged = _deep_merge(DEFAULTS, stored)
        merged["enabled"] = enabled
        merged["auto_enabled"] = auto_enabled
        _CACHE["data"] = merged
        _CACHE["loaded_at"] = monotonic()
        return merged
    except Exception:
        logger.exception("community config refresh failed; using defaults")
        merged = _deep_merge(DEFAULTS, {})
        merged.setdefault("enabled", False)
        merged.setdefault("auto_enabled", False)
        return merged
    finally:
        if own:
            session.close()


def get_config(session=None, *, fresh=False) -> dict:
    """The merged Community Arena config dict (defaults ⊕ stored overrides)."""
    if (not fresh and _CACHE["data"] is not None
            and (monotonic() - _CACHE["loaded_at"]) < CACHE_TTL_SECONDS):
        return _CACHE["data"]
    return _refresh(session)


def invalidate():
    _CACHE["data"] = None
    _CACHE["loaded_at"] = 0.0


def get(path: str, default=None, *, session=None):
    """Dot-path lookup into the merged config, e.g. ``get('games.wordle.reward')``."""
    node = get_config(session)
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
    return node


# ── Convenience accessors used across the feature ───────────────────────

def is_enabled(session=None) -> bool:
    return bool(get_config(session).get("enabled"))


def is_auto_enabled(session=None) -> bool:
    cfg = get_config(session)
    return bool(cfg.get("enabled") and cfg.get("auto_enabled"))


def game_enabled(game_type: str, session=None) -> bool:
    cfg = get_config(session)
    if not cfg.get("enabled"):
        return False
    return bool(cfg.get("games", {}).get(game_type, {}).get("enabled"))


def game_cfg(game_type: str, session=None) -> dict:
    return dict(get_config(session).get("games", {}).get(game_type, {}))


def xp_for(activity: str, session=None) -> int:
    try:
        return int(get_config(session).get("xp", {}).get(activity, {}).get("xp", 0))
    except (TypeError, ValueError):
        return 0


def daily_limit_for(activity: str, session=None):
    """Per-activity rewarded-events-per-day limit, or None for unlimited."""
    node = get_config(session).get("xp", {}).get(activity, {})
    lim = node.get("daily_limit")
    try:
        lim = int(lim)
    except (TypeError, ValueError):
        return None
    return lim if lim > 0 else None


def max_daily_xp(session=None) -> int:
    try:
        return int(get_config(session).get("max_daily_xp", 400))
    except (TypeError, ValueError):
        return 400


# ── Writes ───────────────────────────────────────────────────────────────

def save(session, *, enabled=None, auto_enabled=None, settings_updates=None,
         replace_settings=None, updated_by=None):
    """Persist config changes. Caller does not need to commit — this commits.

    * ``enabled`` / ``auto_enabled`` — set the master switches when not None.
    * ``settings_updates`` — a (possibly nested) dict deep-merged over the
      currently-stored overrides.
    * ``replace_settings`` — replace the entire stored override blob.
    """
    from datetime import datetime
    from models import CommunityConfig

    row = _load_row(session)
    if row is None:
        row = CommunityConfig(enabled=False, auto_enabled=False)
        session.add(row)
        session.flush()

    if enabled is not None:
        row.enabled = bool(enabled)
    if auto_enabled is not None:
        row.auto_enabled = bool(auto_enabled)

    if replace_settings is not None:
        stored = dict(replace_settings)
    else:
        stored = {}
        if row.settings_json:
            try:
                stored = json.loads(row.settings_json) or {}
            except (ValueError, TypeError):
                stored = {}
        if settings_updates:
            stored = _deep_merge(stored, settings_updates)

    row.settings_json = json.dumps(stored)
    row.updated_at = datetime.utcnow()
    if updated_by is not None:
        row.updated_by = int(updated_by)
    session.commit()
    invalidate()
    return row

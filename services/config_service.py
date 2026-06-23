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
    "gspin_gem_min": 2,
    "gspin_gem_max": 15,
    "daily_coins": 300,
    "daily_gems": 0,
    "daily_streak_bonus_coins": 60,
    "daily_streak_bonus_gems": 0,
    "spin_ad_quota": 5,
    "daily_ad_quota": 5,
    "debut_coins": 30000,
    "debut_gems": 6,
    # Simulation tuning — applied as additive % points after all modifiers.
    # The default values fix the "too many dots, not enough singles" feedback.
    "sim_dot_adjust": -10.0,
    "sim_one_adjust": 6.0,
    "sim_two_adjust": 3.0,
    "sim_four_adjust": 0.5,
    "sim_six_adjust": 0.3,
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
    "scorecard_text_settings": None,
    # Which cards /wpm and /cm post to the lobby chat on completion.
    "wpm_result_cards": "summary",
    # Maintenance mode
    "is_maintenance": False,
    "maintenance_message": None,
    "maintenance_until": None,
    "maintenance_started_at": None,
    "maintenance_bypass_ids": None,
    # Match gameplay style. Telegram restores the original in-chat buttons;
    # webapp opts every newly started match into the Mini App board.
    "match_style": "telegram",
    "challenge_max_overs": 2,
    "allow_same_team_challenge": False,
    # Player card rendering style + admin-uploaded template settings
    "card_style": "tier",
    "card_template_image_path": None,
    "card_template_area_code": None,
    "card_template_show_portrait": True,
    "card_template_font_path": None,
    "card_template_settings": None,
}


MATCH_STYLES = {"telegram", "webapp"}

CARD_STYLES = {"tier", "template"}

# Valid completion-card tokens for /wpm and /cm, in the order they should be
# posted to the lobby chat (innings reading order, summary last).
WPM_RESULT_CARD_TOKENS = ("bat1", "bowl1", "bat2", "bowl2", "summary")


def get_wpm_result_cards(session=None):
    """Return the ordered list of completion cards to post for /wpm and /cm.

    Reads fresh so a website save is picked up by a separate bot/admin process
    without a restart. Unknown tokens are dropped. The Match Summary card is
    always appended, even when the admin selected only innings cards, because
    players expect a final summary after every completed Mini-App match.
    """
    raw = _refresh(session).get("wpm_result_cards") or "summary"
    chosen = {t.strip().lower() for t in str(raw).split(",") if t.strip()}
    ordered = [t for t in WPM_RESULT_CARD_TOKENS if t in chosen]
    if "summary" not in ordered:
        ordered.append("summary")
    return ordered


def get_challenge_max_overs(session=None):
    """Return the website-configured /cm innings limit for newly started matches."""
    try:
        return max(1, min(20, int(_refresh(session).get("challenge_max_overs", 2))))
    except (TypeError, ValueError):
        return 2


def get_allow_same_team_challenge(session=None):
    """Return whether league challenges may use the same team for both players."""
    return bool(_refresh(session).get("allow_same_team_challenge", False))


def get_card_style(session=None):
    """Return the global player-card design for all generated cards.

    Reads fresh (like get_match_style) so a website change is picked up by a
    separate bot process without a restart. Unknown/missing values fall back to
    the built-in tier card.
    """
    style = str(_refresh(session).get("card_style") or "tier").lower()
    return style if style in CARD_STYLES else "tier"


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

"""Free Pack — ad-gated reward in the Mini App.

A Free Pack gives ONE base-card player drawn by an OVR-band probability table.
Cooldown is 1 hour by default (admin-tunable via GameConfig).

Default probability table (admin-editable):
    OVR 74 or below : fills remainder (~13%)
    OVR 75-80       : 79%
    OVR 81-83       : 7%
    OVR 84-90       : ~1%

The "floor" band (<=74) catches the remaining probability so the table always
sums to 100%.
"""

import json
import logging
import random
from datetime import datetime, timedelta

from models import User, UserStats, UserRoster, Player
from services.pack_service import _pick_base_at_rating

logger = logging.getLogger(__name__)

MAX_ROSTER = 25
DEFAULT_COOLDOWN_MINUTES = 60

# Default probability bands. weight is relative; we normalize at draw time.
# The floor band (min very low) absorbs the remaining probability.
DEFAULT_BANDS = [
    {"min": 1, "max": 74, "weight": 13, "label": "Common"},
    {"min": 75, "max": 80, "weight": 79, "label": "Standard"},
    {"min": 81, "max": 83, "weight": 7, "label": "Rare"},
    {"min": 84, "max": 90, "weight": 1, "label": "Elite"},
]


def get_cooldown_minutes(session):
    """Read the free pack cooldown from GameConfig (fallback to default)."""
    try:
        from models import GameConfig
        cfg = session.query(GameConfig).first()
        if cfg and cfg.free_pack_cooldown_minutes:
            return max(1, int(cfg.free_pack_cooldown_minutes))
    except Exception:
        pass
    return DEFAULT_COOLDOWN_MINUTES


def get_bands(session):
    """Return the probability bands (admin-configured or default)."""
    try:
        from models import GameConfig
        cfg = session.query(GameConfig).first()
        if cfg and cfg.free_pack_bands_json:
            bands = json.loads(cfg.free_pack_bands_json)
            if isinstance(bands, list) and bands:
                # Validate shape
                clean = []
                for b in bands:
                    if "min" in b and "max" in b and "weight" in b:
                        clean.append({
                            "min": int(b["min"]),
                            "max": int(b["max"]),
                            "weight": float(b["weight"]),
                            "label": b.get("label", ""),
                        })
                if clean:
                    return clean
    except Exception:
        logger.exception("Bad free_pack_bands_json, using default")
    return [dict(b) for b in DEFAULT_BANDS]


def get_band_percentages(session):
    """Return bands with normalized percentage for display."""
    bands = get_bands(session)
    total = sum(b["weight"] for b in bands) or 1.0
    out = []
    for b in bands:
        out.append({
            "min": b["min"], "max": b["max"],
            "label": b.get("label", ""),
            "percent": round(b["weight"] * 100 / total, 2),
        })
    return out


def check_free_pack_cooldown(session, stats):
    """Return (ready: bool, remaining_seconds: int)."""
    cooldown_min = get_cooldown_minutes(session)
    last = stats.last_free_pack
    if last is None:
        return True, 0
    elapsed = (datetime.utcnow() - last).total_seconds()
    cooldown_sec = cooldown_min * 60
    if elapsed >= cooldown_sec:
        return True, 0
    return False, int(cooldown_sec - elapsed)


def _roll_band(bands):
    """Weighted-pick a band."""
    total = sum(b["weight"] for b in bands) or 1.0
    roll = random.uniform(0, total)
    acc = 0
    for b in bands:
        acc += b["weight"]
        if roll <= acc:
            return b
    return bands[-1]


def _pick_rating_in_band(band):
    """Pick a specific OVR rating uniformly within the band."""
    lo = max(1, int(band["min"]))
    hi = max(lo, int(band["max"]))
    return random.randint(lo, hi)


def open_free_pack(session, user):
    """Draw a player and add to roster. Returns result dict.

    Caller must have already verified the ad was watched + cooldown is ready.
    This function re-checks the cooldown defensively.

    Returns:
      {ok, error?, message?, player?, remaining_seconds?, band?}
    """
    stats = session.query(UserStats).filter(UserStats.user_id == user.id).first()
    if not stats:
        stats = UserStats(user_id=user.id)
        session.add(stats)
        session.flush()

    ready, remaining = check_free_pack_cooldown(session, stats)
    if not ready:
        return {"ok": False, "error": "cooldown",
                "message": "Free Pack is on cooldown.",
                "remaining_seconds": remaining}

    # Roster full check
    if (user.roster_count or 0) >= MAX_ROSTER:
        return {"ok": False, "error": "roster_full",
                "message": f"Your roster is full ({MAX_ROSTER}). Release players first."}

    bands = get_bands(session)
    band = _roll_band(bands)

    # Try to find a player; widen across the band if a specific rating is empty
    player = None
    tried_ratings = set()
    # Build a candidate rating list within the band, in random order
    ratings = list(range(int(band["min"]), int(band["max"]) + 1))
    random.shuffle(ratings)
    # Owned base ids so we prefer new players
    owned_base_ids = set(
        r.player_id for r in
        session.query(UserRoster.player_id).filter(UserRoster.user_id == user.id).all()
    )
    for rating in ratings:
        if rating in tried_ratings:
            continue
        tried_ratings.add(rating)
        player = _pick_base_at_rating(session, rating,
                                       user_owned_base_ids=owned_base_ids)
        if player:
            break

    if not player:
        # Last resort — any active base player in the whole band range
        pool = (session.query(Player)
                .filter(Player.is_active == True,
                        Player.parent_player_id.is_(None),
                        Player.rating.between(int(band["min"]), int(band["max"])))
                .all())
        if pool:
            player = random.choice(pool)

    if not player:
        return {"ok": False, "error": "no_player",
                "message": "Couldn't find a player to award. Try again."}

    # Add to roster
    entry = UserRoster(
        user_id=user.id, player_id=player.id,
        order_position=(user.roster_count or 0) + 1,
        acquired_date=datetime.utcnow(),
    )
    session.add(entry)
    session.flush()
    user.roster_count = (user.roster_count or 0) + 1

    # Set cooldown + reset the "ready" notification flag
    stats.last_free_pack = datetime.utcnow()
    stats.notified_free_pack_ready = False

    # Sell value for display
    try:
        from config import get_sell_value
        sell_value = get_sell_value(player.rating)
    except Exception:
        sell_value = 0

    # Activity log
    try:
        from services.activity_service import log_activity
        log_activity(session, user.id, "free_pack",
                     f"Free Pack: {player.name} ({player.rating} OVR)",
                     player_name=player.name, player_rating=player.rating)
    except Exception:
        pass

    # Quest tracking
    try:
        from services.quest_service import safe_track
        safe_track(session, user.id, "free_pack_opened", 1)
    except Exception:
        pass

    cooldown_min = get_cooldown_minutes(session)
    return {
        "ok": True,
        "player": {
            "id": player.id,
            "roster_id": entry.id,
            "name": player.name,
            "rating": player.rating,
            "category": player.category,
            "country": player.country,
            "version": player.version or "Base",
            "sell_value": sell_value,
        },
        "band": {
            "min": band["min"], "max": band["max"],
            "label": band.get("label", ""),
        },
        "cooldown_seconds": cooldown_min * 60,
    }

"""Probability engine — loads the comprehensive CSV matrix and calculates outcomes."""

import csv
import os
import random
import logging

logger = logging.getLogger(__name__)

# ── Load probability matrix from CSV ─────────────────────────────────
# Key: (bowler_type, variation, length, pitch, phase, shot)
# Value: dict {Dot, 1, 2, 3, 4, 6, Wicket, Extra}

_MATRIX = {}
_LOADED = False

CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "probability_matrix.csv")

# Map game bowler types → CSV bowler types
BOWLER_TYPE_MAP = {
    "Fast": "Fast",
    "Medium Pacer": "Medium",
    "Off Spinner": {"Right": "Right-Arm Off Spinner", "Left": "Left-Arm Off Spinner"},
    "Leg Spinner": {"Right": "Right-Arm Leg Spinner", "Left": "Left-Arm Leg Spinner"},
}

# Map game variation names → CSV variation names (handle minor differences)
VAR_MAP = {
    "Reverse Swing": "ReverseSwing",
    "Seam Up": "SeamUP",
    "Cross Seam": "Cross-Seam",
    "Short of Length": "short of Length",
    "Backspinner": "Backspinner (Slider)",
    "Wrong'un": "Wrong'un",
}


def _load_matrix():
    global _MATRIX, _LOADED
    if _LOADED:
        return

    path = os.path.abspath(CSV_PATH)
    if not os.path.exists(path):
        logger.warning(f"Probability matrix not found at {path}")
        _LOADED = True
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                bt = row.get("Bowler Type", "").strip()
                var = row.get("Variation", "").strip()
                length = row.get("Length", "N/A").strip()
                pitch = row.get("Pitch", "Flat").strip()
                phase = row.get("Phase", "Middle Phase").strip()
                shot = row.get("Shot", "Drive").strip()

                key = (bt, var, length, pitch, phase, shot)

                try:
                    probs = {
                        0: float(row.get("Dot", 0)),
                        1: float(row.get("1", 0)),
                        2: float(row.get("2", 0)),
                        3: float(row.get("3", 0)),
                        4: float(row.get("4", 0)),
                        6: float(row.get("6", 0)),
                        "W": float(row.get("Wicket", 0)),
                        "E": float(row.get("Extra", 0)),
                    }
                    _MATRIX[key] = probs
                except (ValueError, TypeError):
                    pass

        logger.info(f"Loaded {len(_MATRIX)} probability entries from CSV")
        _LOADED = True
    except Exception:
        logger.exception("Failed to load probability matrix")
        _LOADED = True


def _map_bowler_type(bowl_style, bowl_hand):
    """Map game bowler style/hand to CSV bowler type."""
    from services.bowling_service import get_bowler_profile_key
    key = get_bowler_profile_key(bowl_style)
    mapped = BOWLER_TYPE_MAP.get(key, "Medium")
    if isinstance(mapped, dict):
        hand = "Right" if not bowl_hand or bowl_hand.startswith("R") else "Left"
        return mapped.get(hand, list(mapped.values())[0])
    return mapped


def _map_variation(variation):
    """Map game variation name to CSV variation name."""
    return VAR_MAP.get(variation, variation)


def _map_shot(shot):
    """Map game shot name to CSV shot name."""
    # Handle "Switch Hit" vs "Switch hit"
    if shot == "Switch Hit":
        return "Switch hit"
    return shot


def _get_phase_name(over, total_overs):
    """Map over number to CSV phase name."""
    if total_overs <= 5:
        return "PowerPlay"
    if over <= 6:
        return "PowerPlay"
    elif over <= total_overs - 4:
        return "Middle Phase"
    else:
        return "Death"


def _apply_rating_diff(probs, bat_rating, bowl_rating):
    """Modify probabilities based on rating differential.
    Positive diff = batsman advantage → more runs, fewer wickets.
    """
    diff = bat_rating - bowl_rating  # -100 to +100 range
    # Scale factor: diff of +20 means ~20% shift toward batsman
    factor = diff / 100.0  # -1.0 to +1.0

    modified = dict(probs)

    # Shift: positive factor → less dots/wickets, more runs
    modified[0] += factor * -8       # fewer dots if batsman better
    modified[1] += factor * 2
    modified[4] += factor * 4        # more boundaries
    modified[6] += factor * 3        # more sixes
    modified["W"] += factor * -5     # fewer wickets
    modified["E"] += factor * 0.5

    # Clamp all to >= 0
    for k in modified:
        if modified[k] < 0:
            modified[k] = 0.0

    # Normalize to 100%
    total = sum(modified.values())
    if total > 0:
        for k in modified:
            modified[k] = (modified[k] / total) * 100.0

    return modified


def calculate_outcome(bowl_style, bowl_hand, variation, length, pitch_type,
                      over, total_overs, shot, bat_rating, bowl_rating):
    """Calculate delivery outcome using the probability matrix.

    Returns dict: {"type": "runs"|"wicket"|"wide"|"noball"|"legbye", "runs": int, "how": str}
    """
    _load_matrix()

    # Map to CSV keys
    bt = _map_bowler_type(bowl_style, bowl_hand)
    var = _map_variation(variation)
    lng = length if length else "N/A"
    pitch = pitch_type if pitch_type in ("Green", "Dry", "Dusty", "Hard", "Flat") else "Flat"
    phase = _get_phase_name(over, total_overs)
    shot_mapped = _map_shot(shot)

    key = (bt, var, lng, pitch, phase, shot_mapped)
    probs = _MATRIX.get(key)

    if not probs:
        # Try without length for spinners
        key2 = (bt, var, "N/A", pitch, phase, shot_mapped)
        probs = _MATRIX.get(key2)

    if not probs:
        # Try with just bowler type and shot (broadest fallback)
        for k, v in _MATRIX.items():
            if k[0] == bt and k[5] == shot_mapped:
                probs = v
                break

    if not probs:
        # Ultimate fallback
        probs = {0: 35, 1: 25, 2: 5, 3: 1, 4: 15, 6: 5, "W": 10, "E": 4}

    # Apply rating differential
    adj = _apply_rating_diff(probs, bat_rating, bowl_rating)

    # Roll
    r = random.random() * 100.0
    cumulative = 0.0

    # Extra first (wide/noball/legbye)
    cumulative += adj["E"]
    if r < cumulative:
        extra_type = random.choices(
            ["wide", "noball", "legbye"],
            weights=[45, 30, 25]
        )[0]
        if extra_type == "wide":
            return {"type": "wide"}
        elif extra_type == "noball":
            return {"type": "noball", "runs": random.choice([0, 1, 1, 4, 6])}
        else:
            return {"type": "legbye", "runs": random.choice([1, 1, 2])}

    # Wicket
    cumulative += adj["W"]
    if r < cumulative:
        hows = ["Bowled", "Caught", "LBW", "Caught Behind", "Caught & Bowled", "Stumped"]
        return {"type": "wicket", "runs": 0, "how": random.choice(hows)}

    # Dot
    cumulative += adj[0]
    if r < cumulative:
        return {"type": "runs", "runs": 0}

    # 1 run
    cumulative += adj[1]
    if r < cumulative:
        return {"type": "runs", "runs": 1}

    # 2 runs
    cumulative += adj[2]
    if r < cumulative:
        return {"type": "runs", "runs": 2}

    # 3 runs
    cumulative += adj[3]
    if r < cumulative:
        return {"type": "runs", "runs": 3}

    # 4 runs
    cumulative += adj[4]
    if r < cumulative:
        return {"type": "runs", "runs": 4}

    # 6 runs
    return {"type": "runs", "runs": 6}

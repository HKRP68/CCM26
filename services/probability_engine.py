"""Balanced probability engine for T20 cricket simulation.

Target stats (equal ratings, flat pitch):
- Run rate: ~9-11 per over
- Dot%: 25-30%, 1s: 25%, 2s: 8%, 3s: 2%, 4s: 16%, 6s: 10%
- Wicket: 4-5% per ball (~1 per 3-4 overs)
- Extras: 3-4%
"""

import random
import logging

logger = logging.getLogger(__name__)

# ── Base probabilities (%) — for "neutral" conditions ────────────────
# These are the starting point before any modifiers
BASE = {
    "dot": 32.0,
    "1": 25.0,
    "2": 8.0,
    "3": 2.0,
    "4": 12.0,
    "6": 7.5,
    "W": 3.5,
    "wide": 2.5,
    "noball": 1.2,
    "legbye": 1.5,
    "bye": 0.5,
}

# ── Shot modifiers ───────────────────────────────────────────────────
SHOT_MODS = {
    "Drive":      {"dot": -3, "1": 2, "2": 1, "4": 2, "6": -1, "W": -0.5},
    "Cut":        {"dot": -1, "1": 2, "2": 2, "4": 1, "6": -2, "W": 0},
    "Pull":       {"dot": -2, "1": 0, "2": 1, "4": 2, "6": 2, "W": 0.5},
    "Leg Glance": {"dot": -1, "1": 3, "2": 3, "4": 0, "6": -3, "W": -1},
    "Flick":      {"dot": -2, "1": 2, "2": 2, "4": 1, "6": -1, "W": -0.5},
    "Sweep":      {"dot": -1, "1": 1, "2": 1, "4": 1, "6": 1, "W": 0.5},
    "Switch Hit": {"dot": -3, "1": -1, "2": 0, "4": 2, "6": 4, "W": 1.5},
    "Slog":       {"dot": -6, "1": -3, "2": -1, "4": 3, "6": 8, "W": 2.5},
    "Loft":       {"dot": -4, "1": -2, "2": 0, "4": 2, "6": 6, "W": 1.5},
}

# ── Bowler type modifiers ────────────────────────────────────────────
BOWLER_MODS = {
    "Fast": {"dot": 1, "1": 0, "4": -0.5, "6": -0.5, "W": 0.5},
    "Medium Pacer": {"dot": 0, "1": 0.5, "4": 0, "6": 0, "W": 0},
    "Off Spinner": {"dot": 2, "1": 0.5, "4": -1, "6": -0.5, "W": 0.5},
    "Leg Spinner": {"dot": 1.5, "1": 0, "4": -0.5, "6": -0.5, "W": 1},
}

# ── Delivery variation modifiers ─────────────────────────────────────
VARIATION_MODS = {
    # Fast
    "Outswing":       {"dot": 1, "W": 0.5, "4": -0.5},
    "Inswing":        {"dot": 0.5, "W": 0.5, "4": -0.5},
    "Reverse Swing":  {"dot": 2, "W": 1, "4": -1, "6": -0.5},
    "Seam Up":        {"dot": 0.5, "1": 0.5, "W": 0},
    "Slower":         {"dot": -1, "4": 0.5, "6": 1, "W": 0.5},
    # Medium
    "Leg Cutter":     {"dot": 1, "W": 0.5, "4": -0.5},
    "Off Cutter":     {"dot": 1, "W": 0.5, "4": -0.5},
    "Knuckle":        {"dot": 0.5, "W": 0.5, "6": -0.5},
    "Cross Seam":     {"dot": 0, "1": 0.5},
    # Spinners
    "Off Break":      {"dot": 1, "W": 0.5, "4": -0.5},
    "Doosra":         {"dot": 1.5, "W": 1, "4": -1},
    "Arm Ball":       {"dot": 0, "4": 0.5},
    "Top Spinner":    {"dot": 0.5, "W": 0.5},
    "Carrom Ball":    {"dot": 1, "W": 1, "4": -0.5},
    "Leg Break":      {"dot": 1, "W": 0.5, "4": -0.5},
    "Googly":         {"dot": 2, "W": 1.5, "4": -1, "6": -0.5},
    "Flipper":        {"dot": 1, "W": 1, "6": -0.5},
    "Slider":         {"dot": 0.5, "W": 0.5},
    "Orthodox":       {"dot": 1, "W": 0.5},
    "Backspinner":    {"dot": 0.5, "4": 0.5},
    "Chinaman":       {"dot": 1, "W": 1, "4": -0.5},
    "Wrong'un":       {"dot": 1.5, "W": 1, "4": -1},
    "Teesra":         {"dot": 1, "W": 0.5},
}

# ── Length modifiers (pacers only) ───────────────────────────────────
LENGTH_MODS = {
    "Yorker":          {"dot": 5, "1": -2, "4": -3, "6": -2, "W": 2},
    "Good":            {"dot": 2, "1": 1, "4": -1, "6": -1, "W": 1},
    "Good Length":     {"dot": 2, "1": 1, "4": -1, "6": -1, "W": 1},
    "Full":            {"dot": -3, "1": 0, "4": 3, "6": 1, "W": 0},
    "Full Length":     {"dot": -3, "1": 0, "4": 3, "6": 1, "W": 0},
    "Hard":            {"dot": 3, "1": -1, "4": -1, "6": 1, "W": 1},
    "Bouncer":         {"dot": 4, "1": -3, "4": 1, "6": 3, "W": 2},
    "Hit the Deck":    {"dot": 2, "1": 0, "4": 0, "6": 1, "W": 0},
    "Short of Length":  {"dot": 2, "1": 0, "4": 1, "6": 1, "W": 0},
    "Back of Length":   {"dot": 3, "1": 0, "4": -1, "6": 0, "W": 1},
}

# ── Pitch modifiers ──────────────────────────────────────────────────
PITCH_MODS = {
    "Green":  {"dot": 2, "4": -1, "6": -1, "W": 1},
    "Dry":    {"dot": 1, "4": -0.5, "6": -0.5, "W": 0.5},
    "Dusty":  {"dot": 2, "4": -1, "6": -1, "W": 1},
    "Hard":   {"dot": -2, "4": 2, "6": 1, "W": -0.5},
    "Flat":   {"dot": -2, "4": 2, "6": 1.5, "W": -1},
    "Bouncy": {"dot": 0.5, "6": 1, "W": 0.5},
}

# ── Phase modifiers ──────────────────────────────────────────────────
PHASE_MODS = {
    "PowerPlay":    {"dot": -3, "1": -1, "4": 2, "6": 2, "W": 0},
    "Middle Phase": {"dot": 1, "1": 1, "4": -0.5, "6": -1, "W": 0},
    "Death":        {"dot": -4, "1": -2, "4": 2, "6": 5, "W": 0.5},
}

# ── Pitch + Bowler type synergy ──────────────────────────────────────
PITCH_BOWLER_SYNERGY = {
    ("Green", "Fast"):        {"W": 1, "dot": 1, "4": -1},
    ("Green", "Medium Pacer"): {"W": 0.5, "dot": 0.5, "4": -0.5},
    ("Dry", "Off Spinner"):    {"W": 1, "dot": 1, "4": -1},
    ("Dry", "Leg Spinner"):    {"W": 1, "dot": 1, "4": -1},
    ("Dusty", "Off Spinner"):  {"W": 1.5, "dot": 1.5, "4": -1.5},
    ("Dusty", "Leg Spinner"):  {"W": 1.5, "dot": 1.5, "4": -1.5},
    ("Hard", "Fast"):          {"6": 0.5, "4": 0.5},
    ("Flat", "Fast"):          {"4": 1, "6": 0.5, "W": -0.5},
    ("Flat", "Medium Pacer"):  {"4": 1, "6": 0.5, "W": -0.5},
}


def _get_bowler_key(bowl_style):
    from services.bowling_service import get_bowler_profile_key
    return get_bowler_profile_key(bowl_style)


def _get_phase(over, total_overs):
    if total_overs <= 5:
        return "PowerPlay"
    if over <= 6:
        return "PowerPlay"
    elif over <= total_overs - 4:
        return "Middle Phase"
    return "Death"


def _apply_mods(probs, mods):
    for k, v in mods.items():
        if k in probs:
            probs[k] += v


def _apply_rating_diff(probs, bat_rating, bowl_rating):
    """Rating differential: +1 diff = slight shift toward batsman."""
    diff = bat_rating - bowl_rating  # -100 to +100
    factor = diff / 100.0

    probs["dot"] += factor * -5
    probs["1"] += factor * 1
    probs["2"] += factor * 0.5
    probs["4"] += factor * 2.5
    probs["6"] += factor * 2
    probs["W"] += factor * -3

    # Even against much better bowler, keep minimum scoring chance
    if diff < -10:
        probs["4"] = max(probs["4"], 10.0)
        probs["6"] = max(probs["6"], 5.0)
        probs["W"] = min(probs["W"], 8.0)  # cap wicket chance


def _normalize(probs):
    """Clamp to >=0 and normalize to 100%."""
    for k in probs:
        if probs[k] < 0:
            probs[k] = 0.0
    total = sum(probs.values())
    if total > 0:
        for k in probs:
            probs[k] = (probs[k] / total) * 100.0
    return probs


def calculate_outcome(bowl_style, bowl_hand, variation, length, pitch_type,
                      over, total_overs, shot, bat_rating, bowl_rating):
    """Calculate delivery outcome.

    Returns: {"type": "runs"|"wicket"|"wide"|"noball"|"legbye", "runs": int, "how": str}
    """
    # Start with base
    probs = {
        "dot": BASE["dot"], "1": BASE["1"], "2": BASE["2"], "3": BASE["3"],
        "4": BASE["4"], "6": BASE["6"], "W": BASE["W"],
        "wide": BASE["wide"], "noball": BASE["noball"],
        "legbye": BASE["legbye"],
    }

    # Apply modifiers
    bowler_key = _get_bowler_key(bowl_style)
    phase = _get_phase(over, total_overs)

    # Bowler type
    _apply_mods(probs, BOWLER_MODS.get(bowler_key, {}))

    # Variation
    clean_var = variation.replace(" (Surprise)", "").strip() if variation else ""
    _apply_mods(probs, VARIATION_MODS.get(clean_var, {}))

    # Length (pacers only)
    if length and length != "N/A":
        _apply_mods(probs, LENGTH_MODS.get(length, {}))

    # Pitch
    pitch = pitch_type if pitch_type in PITCH_MODS else "Flat"
    _apply_mods(probs, PITCH_MODS.get(pitch, {}))

    # Pitch + bowler synergy
    _apply_mods(probs, PITCH_BOWLER_SYNERGY.get((pitch, bowler_key), {}))

    # Phase
    _apply_mods(probs, PHASE_MODS.get(phase, {}))

    # Shot
    _apply_mods(probs, SHOT_MODS.get(shot, {}))

    # Rating differential
    _apply_rating_diff(probs, bat_rating, bowl_rating)

    # Normalize
    _normalize(probs)

    # Roll the dice
    r = random.random() * 100.0
    cumul = 0.0

    # Wide
    cumul += probs["wide"]
    if r < cumul:
        return {"type": "wide"}

    # No ball
    cumul += probs["noball"]
    if r < cumul:
        return {"type": "noball", "runs": random.choice([0, 1, 1, 2, 4, 6])}

    # Leg bye
    cumul += probs["legbye"]
    if r < cumul:
        return {"type": "legbye", "runs": random.choice([1, 1, 1, 2])}

    # Wicket
    cumul += probs["W"]
    if r < cumul:
        hows = ["Bowled", "Caught", "LBW", "Caught Behind", "Caught & Bowled"]
        if bowler_key in ("Off Spinner", "Leg Spinner"):
            hows.append("Stumped")
        return {"type": "wicket", "runs": 0, "how": random.choice(hows)}

    # Dot
    cumul += probs["dot"]
    if r < cumul:
        return {"type": "runs", "runs": 0}

    # 1 run
    cumul += probs["1"]
    if r < cumul:
        return {"type": "runs", "runs": 1}

    # 2 runs
    cumul += probs["2"]
    if r < cumul:
        return {"type": "runs", "runs": 2}

    # 3 runs
    cumul += probs["3"]
    if r < cumul:
        return {"type": "runs", "runs": 3}

    # 4 runs
    cumul += probs["4"]
    if r < cumul:
        return {"type": "runs", "runs": 4}

    # 6 runs
    return {"type": "runs", "runs": 6}

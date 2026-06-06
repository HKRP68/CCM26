"""Probability Engine — balanced simulation for ball-by-ball outcomes.

Design targets:
  - Average 1st innings score: 170-240 runs in 20 overs (8.5-12 RPO)
  - Equal-rating teams converge near 195 (median)
  - Higher-rated team consistently wins (rating diff dominates)
  - All outcomes possible from any combination, with sensible weights

Outcome types: dot, 1, 2, 3, 4, 6, W, wide, noball, legbye

Pipeline:
  base → bowler-type mod → variation/delivery mod → length mod (pacers only)
  → pitch mod → phase mod → shot mod → rating-diff mod → traits → normalize → roll
"""

import random
import logging

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# BASE PROBABILITIES (percent points — must sum near 100)
# ═══════════════════════════════════════════════════════════════════════
# Tuned so equal-rated teams average ~9.5 RPO over 20 overs.
# Expected runs/ball ≈ 0.30(1) + 0.18(2) + 0.06(3) + 0.56(4) + 0.48(6) = 1.58 → 9.5 RPO

BASE = {
    "dot":    20.0,   # fewer dots keeps /wpm and /cm moving
    "1":      33.0,   # more singles for constant strike rotation
    "2":      10.5,   # extra twos make running between wickets matter
    "3":       3.5,   # more threes for aggressive running and strike changes
    "4":      16.8,   # slight boundary lift for excitement
    "6":      10.0,   # slight six-hitting lift without turning arcade-only
    "W":       4.0,   # wicket
    "wide":    1.3,
    "noball":  0.4,
    "legbye":  1.5,
}


# ═══════════════════════════════════════════════════════════════════════
# BOWLER TYPE MODIFIERS
# ═══════════════════════════════════════════════════════════════════════
# How each bowler type shifts base probabilities (additive % points).

BOWLER_MODS = {
    "Fast Pacer": {
        # Threatening — more wickets, more dots, more extras, fewer easy 1s
        "W": +1.5, "dot": +3, "1": -3, "wide": +0.5, "noball": +0.5,
    },
    "Medium Pacer": {
        # Predictable — slightly easier to score
        "1": +2, "2": +1, "4": +0.5, "wide": -0.3,
    },
    "Off Spinner": {
        # Tight, more dots, lower wide chance
        "dot": +2, "W": +0.8, "6": -1, "wide": -0.8,
    },
    "Leg Spinner": {
        # Wicket-taking, leaks 4s sometimes
        "dot": +1.5, "W": +1.2, "4": +0.5, "wide": -0.5,
    },
}


# ═══════════════════════════════════════════════════════════════════════
# PACER VARIATION MODIFIERS
# ═══════════════════════════════════════════════════════════════════════

VARIATION_MODS = {
    # ── Fast pacer variations
    "Outswing":     {"W": +0.8, "4": +0.5, "dot": +1, "1": -1},
    "Inswing":      {"W": +1.0, "dot": +0.5, "1": -0.5},
    "Reverse Swing": {"W": +1.5, "dot": +1, "6": -0.5, "1": -1},
    "Seam Up":      {"W": +0.8, "dot": +1.5, "1": -1.5},
    "Slower":       {"W": +0.8, "dot": +2, "6": -1, "1": -0.5, "4": -0.5},

    # ── Medium pacer variations
    "Leg Cutter":   {"W": +0.7, "dot": +1.5, "4": -0.5, "1": -0.5},
    "Off Cutter":   {"W": +0.7, "dot": +1.5, "4": -0.5, "1": -0.5},
    "Knuckle":      {"W": +0.5, "dot": +2, "1": -1, "4": -0.5},
    "Cross Seam":   {"dot": +1, "1": +0.5, "4": -0.5},

    # ── Off spinner variations
    "Off Break":    {"dot": +2, "W": +0.5, "6": -0.5, "1": -1},
    "Doosra":       {"W": +1.5, "dot": +1, "1": -1, "4": +0.5},
    "Arm Ball":     {"W": +1.0, "dot": +1.5, "1": -1, "6": -0.5},
    "Top Spinner":  {"W": +0.8, "dot": +1.5, "6": -0.5, "1": -0.5},
    "Carrom Ball":  {"W": +1.2, "dot": +1, "4": +0.5, "1": -1.5},

    # ── Leg spinner variations
    "Leg Break":    {"dot": +2, "W": +0.6, "1": -1, "6": -0.5},
    "Googly":       {"W": +1.8, "dot": +1, "4": +0.5, "1": -1.5},
    "Flipper":      {"W": +1.5, "dot": +1.5, "1": -1, "6": -0.5},
    "Slider":       {"W": +1.0, "dot": +1.5, "1": -1, "4": -0.3},

    # ── Left-arm off spin (orthodox)
    "Orthodox":     {"dot": +2, "W": +0.5, "1": -1, "6": -0.3},
    "Backspinner":  {"W": +1.0, "dot": +1.5, "1": -1.5, "4": -0.3},

    # ── Left-arm leg spin (Chinaman)
    "Chinaman":     {"dot": +2, "W": +0.6, "6": -0.5, "1": -0.5},
    "Wrong'un":     {"W": +1.8, "dot": +1, "4": +0.5, "1": -1.5},
    "Teesra":       {"W": +1.5, "dot": +1, "1": -1, "4": +0.3},

    # default - no special effect
    "Stock Ball":   {},
}


# ═══════════════════════════════════════════════════════════════════════
# LENGTH MODIFIERS (pacers only)
# ═══════════════════════════════════════════════════════════════════════

LENGTH_MODS = {
    # Fast pacer lengths
    "Hard":         {"W": +0.5, "4": -0.5, "dot": +1, "1": -0.5},
    "Good":         {"dot": +1.5, "W": +0.3, "4": -0.5, "6": -0.3},
    "Full":         {"4": +1.5, "6": +0.5, "W": +0.5, "dot": -1, "1": -1},
    "Hit the Deck": {"W": +0.5, "dot": +1, "6": -0.3, "1": -0.5},
    "Yorker":       {"W": +1.5, "dot": +2, "1": -1, "6": -1, "4": -1},
    "Bouncer":      {"W": +1.0, "dot": +1, "wide": +0.5, "noball": +0.3},

    # Medium pacer lengths
    "Good Length":     {"dot": +2, "W": +0.5, "4": -0.5, "1": -0.5},
    "Full Length":     {"4": +1.5, "6": +0.5, "W": +0.4, "dot": -1, "1": -0.5},
    "Short of Length": {"dot": +1, "W": +0.4, "1": -0.5},
    "Back of Length":  {"dot": +1.5, "W": +0.5, "6": -0.3},
}


# ═══════════════════════════════════════════════════════════════════════
# PITCH MODIFIERS
# ═══════════════════════════════════════════════════════════════════════

PITCH_MODS = {
    "Green":  {"W": +1.5, "dot": +2, "4": -1, "6": -1, "1": -0.5},   # seam-friendly
    "Dry":    {"dot": +0.5, "W": +0.3, "6": -0.5},                    # neutral-bowler
    "Dusty":  {"W": +0.5, "dot": +1, "1": +0.5, "6": -0.5},           # spin grip
    "Hard":   {"4": +0.5, "6": +0.5, "1": +0.3, "W": -0.3, "dot": -0.5},  # bouncy
    "Flat":   {"4": +0.5, "6": +0.5, "1": +0.5, "dot": -0.5, "W": -0.3},  # batting-friendly
}


# ═══════════════════════════════════════════════════════════════════════
# PITCH WEAR — deterioration over the course of the match
# ═══════════════════════════════════════════════════════════════════════

# As the pitch wears: more spin grip, lower bounce, slower scoring.
# wear=0 (fresh) to wear=100 (very worn — typical 2nd innings end).
# We apply a graduated modifier on top of the base PITCH_MODS.

def _pitch_wear_mods(pitch_type, wear):
    """Returns a dict of probability modifiers for the current wear level.

    Logic:
    - As wear increases, dots & wickets go up slightly, fours/sixes go down.
    - Effect is doubled on Dusty (already spin-friendly) and Dry (gets dustier).
    - Hard/Flat resist wear better.
    - All wear effects are subtle — typical ±0.5 to ±1.0 at peak wear.
    """
    if wear <= 5:
        return {}
    factor = (wear / 100.0)
    base = {
        "dot": +1.5 * factor,
        "W":   +0.8 * factor,
        "4":   -1.0 * factor,
        "6":   -1.2 * factor,
        "1":   +0.5 * factor,
    }
    # Pitch-specific multipliers
    multiplier = {
        "Dusty": 1.4, "Dry": 1.3,
        "Green": 1.0, "Flat": 0.6, "Hard": 0.5,
    }.get(pitch_type, 1.0)
    return {k: v * multiplier for k, v in base.items()}


def calc_pitch_wear(innings, current_over, total_overs):
    """Compute current pitch wear (0-100) given innings + over.

    1st innings: wear builds 0 → ~30 over the innings
    2nd innings: starts at ~30, builds to ~75 by end
    """
    if innings == 1:
        progress = current_over / max(1, total_overs)
        return int(progress * 30)
    else:  # innings 2
        progress = current_over / max(1, total_overs)
        return int(30 + progress * 45)




PITCH_BOWLER_SYNERGY = {
    ("Green",  "Fast Pacer"):    {"W": +1.0, "dot": +1},
    ("Green",  "Medium Pacer"):  {"W": +0.5, "dot": +0.5},
    ("Dusty",  "Off Spinner"):   {"W": +0.8, "dot": +1},
    ("Dusty",  "Leg Spinner"):   {"W": +1.0, "dot": +1},
    ("Hard",   "Fast Pacer"):    {"W": +0.3, "6": +0.3},
    ("Flat",   "Off Spinner"):   {"4": +0.5, "6": +0.3, "dot": -0.5},
    ("Flat",   "Leg Spinner"):   {"4": +0.3, "6": +0.5, "dot": -0.5},
}


# ═══════════════════════════════════════════════════════════════════════
# PHASE MODIFIERS — based on over within innings
# ═══════════════════════════════════════════════════════════════════════

# Powerplay 1-6, Middle 7-15, Death 16-20. For non-T20 (10/15 over) we scale.

PHASE_MODS = {
    "powerplay": {
        # Boundaries are easier (fielding restrictions); keep singles flowing.
        "4": +1.7, "6": +0.7, "W": +0.5, "dot": -2.0, "1": +0.5, "2": +0.5,
    },
    "middle": {
        # Middle overs should rotate strike instead of stalling on dots.
        "1": +3.0, "2": +1.0, "3": +0.4, "dot": -1.0, "4": -0.4, "6": -0.2, "W": -0.5,
    },
    "death": {
        # Big hits OR big wickets — high variance with fewer dead balls.
        "6": +3.8, "4": +2.3, "W": +1.0, "1": +0.5, "2": +0.5, "dot": -4.0,
    },
}


def _get_phase(over: int, total_overs: int) -> str:
    """Return powerplay/middle/death based on over and total length.
    Scales for non-T20 lengths."""
    if total_overs >= 20:
        if over <= 6: return "powerplay"
        if over <= 15: return "middle"
        return "death"
    # 10 or 15 over: scale phases proportionally
    pp_end = max(2, int(total_overs * 0.3))
    death_start = total_overs - max(2, int(total_overs * 0.2))
    if over <= pp_end: return "powerplay"
    if over <= death_start: return "middle"
    return "death"


# ═══════════════════════════════════════════════════════════════════════
# SHOT MODIFIERS — what the BATSMAN's shot does to the prob distribution
# ═══════════════════════════════════════════════════════════════════════

SHOT_MODS = {
    "Drive":       {"4": +2.5, "1": +1, "dot": -2, "W": +0.3, "6": +0.3},      # safe & productive
    "Cut":         {"4": +2, "1": +0.5, "dot": -1.5, "W": +0.3},
    "Pull":        {"4": +1.5, "6": +1.5, "W": +0.5, "dot": -1, "1": -0.5},    # high-risk reward
    "Leg Glance":  {"1": +3, "2": +0.5, "4": +0.5, "dot": -2, "W": -0.3},      # safest shot
    "Flick":       {"4": +1.5, "1": +1.5, "dot": -1.5, "W": +0.2},
    "Sweep":       {"4": +2, "1": +0.5, "W": +0.8, "dot": -1.5, "6": +0.3},    # spinner-killer, risky
    "Switch Hit":  {"4": +1.5, "6": +2, "W": +1.5, "dot": -1, "1": -1},        # all-or-nothing
    "Slog":        {"6": +4, "4": +1, "W": +2, "dot": -2.5, "1": -1.5},        # max boundaries, max risk
    "Loft":        {"4": +1, "6": +3, "W": +1.2, "dot": -1.5, "1": -1},        # aerial big hit
    "Defend":      {"dot": +20, "1": -2, "2": -1, "4": -3, "6": -1, "W": -0.5},  # block — burn balls, very safe
    "Leave":       {"dot": +35, "1": -5, "2": -1, "4": -4, "6": -2, "W": -1.5}, # let it pass — no runs, very low risk
    # ── Extended shot repertoire (UnderCover /cric parity) ──
    "On Drive":    {"4": +2.0, "1": +1, "dot": -1.5, "W": +0.3, "6": +0.3},     # elegant, productive
    "Off Drive":   {"4": +2.2, "1": +0.8, "dot": -1.5, "W": +0.4},
    "Hook":        {"4": +1.5, "6": +1.5, "W": +1.0, "dot": -1, "1": -0.5},     # vs short, aerial risk
    "Square Cut":  {"4": +2.0, "1": +0.5, "dot": -1.5, "W": +0.4},
    "Late Cut":    {"4": +1.5, "1": +1, "W": +0.6, "dot": -1},                   # fine and risky
    "Reverse Sweep":{"4": +1.8, "6": +0.5, "W": +1.3, "dot": -1, "1": -0.5},    # all-or-nothing vs spin
    "Slog Sweep":  {"6": +3, "4": +1, "W": +1.8, "dot": -2, "1": -1},           # max over cow corner
    "Glance":      {"1": +3, "2": +0.5, "4": +0.4, "dot": -2, "W": -0.2},        # safe rotation
    "Paddle":      {"1": +2, "4": +1, "W": +0.8, "dot": -1.5},                   # fine scoop
    "Upper Cut":   {"4": +1.5, "6": +1.5, "W": +1.2, "dot": -1, "1": -0.5},     # aerial over slips
}


# DB-backed shot mods cache. Refreshed when admin saves.
_SHOT_MODS_CACHE = {"data": None}


def invalidate_shot_mods_cache():
    """Called from admin after saving to force a re-read on next ball."""
    _SHOT_MODS_CACHE["data"] = None


def _get_shot_mods(shot):
    """Get shot mods — DB if available (cached), fallback to SHOT_MODS."""
    if _SHOT_MODS_CACHE["data"] is not None:
        return _SHOT_MODS_CACHE["data"].get(shot, SHOT_MODS.get(shot, {}))

    try:
        from database import get_session
        from models import ShotProbability
        s = get_session()
        try:
            rows = s.query(ShotProbability).filter(
                ShotProbability.enabled == True).all()
            if not rows:
                _SHOT_MODS_CACHE["data"] = {}
                return SHOT_MODS.get(shot, {})
            cache = {}
            for r in rows:
                mods = {}
                if r.mod_dot: mods["dot"] = r.mod_dot
                if r.mod_1: mods["1"] = r.mod_1
                if r.mod_2: mods["2"] = r.mod_2
                if r.mod_3: mods["3"] = r.mod_3
                if r.mod_4: mods["4"] = r.mod_4
                if r.mod_6: mods["6"] = r.mod_6
                if r.mod_wicket: mods["W"] = r.mod_wicket
                if r.mod_extras: mods["extras"] = r.mod_extras
                cache[r.shot_name] = mods
            _SHOT_MODS_CACHE["data"] = cache
            return cache.get(shot, SHOT_MODS.get(shot, {}))
        finally:
            s.close()
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "Couldn't load ShotProbability; using SHOT_MODS fallback")
        _SHOT_MODS_CACHE["data"] = {}
        return SHOT_MODS.get(shot, {})


# ═══════════════════════════════════════════════════════════════════════
# RATING DIFFERENTIAL
# ═══════════════════════════════════════════════════════════════════════
# Higher-rated player dominates. Magnitude scales with diff.

def _apply_rating_diff(probs: dict, bat_rating: int, bowl_rating: int):
    """Adjust probs based on (bat_rating - bowl_rating).

    Diff scale (DOMINANT):
      diff = +30  → batsman utterly dominates (e.g. 95 vs 65) → big score
      diff = +15  → batsman favored (e.g. 90 vs 75)
      diff =   0  → even
      diff = -15  → bowler favored (e.g. 75 vs 90)
      diff = -30  → bowler dominates (e.g. 65 vs 95) → low score, many wickets

    Doubled magnitude vs prior version so rating actually drives match outcomes.
    """
    diff = bat_rating - bowl_rating
    factor = diff / 25.0  # at diff=25, factor=1.0 (significant)

    # Scaling
    boundary_shift = 7.0 * factor    # at factor=1.0 → +7% to boundaries
    wicket_shift = -3.5 * factor     # at factor=1.0 → -3.5% to wickets
    dot_shift = -5.0 * factor        # at factor=1.0 → -5% to dots
    one_shift = 1.5 * factor
    running_shift = 1.2 * factor

    probs["4"] = max(0.5, probs["4"] + boundary_shift * 0.6)
    probs["6"] = max(0.3, probs["6"] + boundary_shift * 0.4)
    probs["W"] = max(0.5, probs["W"] + wicket_shift)
    probs["dot"] = max(4.0, probs["dot"] + dot_shift)
    probs["1"] = max(5.0, probs["1"] + one_shift)
    probs["2"] = max(1.0, probs["2"] + running_shift * 0.8)
    probs["3"] = max(0.5, probs["3"] + running_shift * 0.2)

    # Extra wickets when bowler dominates a lot
    if diff < -15:
        probs["W"] += abs(diff + 15) * 0.15  # extra wickets for big-mismatch bowler dominance
        probs["dot"] += abs(diff + 15) * 0.2
    # Extra boundaries when batsman dominates a lot
    if diff > 15:
        probs["4"] += (diff - 15) * 0.15
        probs["6"] += (diff - 15) * 0.10


# ═══════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════

def _apply_mods(probs: dict, mods: dict):
    """Add mods (percentage points) to probs in-place. Floor at small positive."""
    for key, delta in mods.items():
        if key in probs:
            probs[key] = max(0.1, probs[key] + delta)


def _normalize(probs: dict):
    """Ensure all probs are positive and they sum to 100."""
    for k in probs:
        if probs[k] < 0:
            probs[k] = 0.1
    total = sum(probs.values())
    if total <= 0:
        # Reset to base if everything got zeroed (shouldn't happen)
        for k, v in BASE.items():
            probs[k] = v
        return
    scale = 100.0 / total
    for k in probs:
        probs[k] *= scale


def _get_bowler_key(bowl_style: str) -> str:
    """Normalize bowl_style to one of the BOWLER_MODS keys."""
    if not bowl_style:
        return "Medium Pacer"
    s = bowl_style.lower()
    if "fast" in s: return "Fast Pacer"
    if "medium" in s: return "Medium Pacer"
    if "off spin" in s or "off-spin" in s or "off spinner" in s: return "Off Spinner"
    if "leg spin" in s or "leg-spin" in s or "leg spinner" in s: return "Leg Spinner"
    return "Medium Pacer"


# ═══════════════════════════════════════════════════════════════════════
# DYNAMIC IN-MATCH MECHANICS  (UnderCover /cric parity)
# ═══════════════════════════════════════════════════════════════════════
# Free hit, mystery ball, momentum and bowler-spam penalty. These are
# applied as multiplicative adjustments to the prob distribution just
# before normalization, so they layer cleanly on top of everything above.
# All are gated behind kwargs that default to no-op, so other callers
# (legacy /playmatch, AI autoplay, simulations) are unaffected.

MYSTERY_WICKET_MULT   = 1.35   # +35% wicket chance on a mystery ball
MYSTERY_SCORING_MULT  = 0.50   # -50% boundary scoring on a mystery ball

MOMENTUM_RUNS_WINDOW    = 12   # balls considered for the "recent runs" window
MOMENTUM_RUNS_THRESHOLD = 12   # runs in the window that trigger batting momentum
MOMENTUM_BOUNDARY_MULT  = 1.15 # +15% boundaries when the batters are flying
MOMENTUM_WICKET_MULT    = 1.25 # +25% wicket when the bowler is on a roll (>=2 in a row)

SPAM_REPEAT_THRESHOLD = 3      # same delivery id bowled 3+ times in a row
SPAM_WICKET_STEP      = 0.18   # +18% wicket per extra repeat beyond the threshold
SPAM_WICKET_CAP       = 1.70   # cap the spam wicket multiplier at +70%
SPAM_EXTRAS_MULT      = 1.20   # repeated deliveries leak slightly more extras

FREE_HIT_BOUNDARY_MULT = 1.35  # +35% boundaries on a free hit
FREE_HIT_WICKET_MULT   = 0.10  # only a run-out is possible on a free hit


def _apply_dynamic_mods(probs, *, free_hit=False, mystery=False,
                        recent_runs=0, consec_wickets=0, delivery_repeat=0):
    """Multiplicatively adjust the prob distribution for live-match mechanics.

    Runs after every additive layer and before _normalize, so the relative
    boosts/cuts described by UnderCover translate directly. No-op when every
    flag is at its default.
    """
    # Mystery ball — wicket spike, scoring slump (lost runs flow into dots).
    if mystery:
        probs["W"] *= MYSTERY_WICKET_MULT
        lost = 0.0
        for k in ("4", "6"):
            lost += probs[k] * (1.0 - MYSTERY_SCORING_MULT)
            probs[k] *= MYSTERY_SCORING_MULT
        probs["dot"] += lost * 0.5

    # Batting momentum — recent run glut makes boundaries flow.
    if recent_runs >= MOMENTUM_RUNS_THRESHOLD:
        probs["4"] *= MOMENTUM_BOUNDARY_MULT
        probs["6"] *= MOMENTUM_BOUNDARY_MULT

    # Bowling momentum — wickets in a row breed wickets.
    if consec_wickets >= 2:
        probs["W"] *= MOMENTUM_WICKET_MULT

    # Bowler-spam penalty — predictable lines get punished.
    if delivery_repeat >= SPAM_REPEAT_THRESHOLD:
        extra = delivery_repeat - SPAM_REPEAT_THRESHOLD + 1
        mult = min(SPAM_WICKET_CAP, 1.0 + SPAM_WICKET_STEP * extra)
        probs["W"] *= mult
        probs["wide"] *= SPAM_EXTRAS_MULT
        probs["noball"] *= SPAM_EXTRAS_MULT

    # Free hit — boundary bias up, dismissals (bar run-out) suppressed.
    if free_hit:
        probs["4"] *= FREE_HIT_BOUNDARY_MULT
        probs["6"] *= FREE_HIT_BOUNDARY_MULT
        probs["W"] *= FREE_HIT_WICKET_MULT


# ═══════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════

def calculate_outcome(bowl_style, bowl_hand, variation, length, pitch_type,
                      over, total_overs, shot, bat_rating, bowl_rating,
                      striker_traits=None, bowler_traits=None, trait_ctx=None,
                      pitch_wear=0, free_hit=False, mystery=False,
                      recent_runs=0, consec_wickets=0, delivery_repeat=0):
    """Calculate one ball outcome.

    pitch_wear: 0-100 (deterioration). 0 fresh; 100 fully worn.

    Live-match mechanics (UnderCover /cric parity), all default to no-op:
      free_hit        — this is a free-hit ball (no dismissal except run-out)
      mystery         — this is a "mystery ball" (wicket spike, scoring slump)
      recent_runs     — runs scored over the last ~12 balls (batting momentum)
      consec_wickets  — wickets in a row for the bowling side (bowling momentum)
      delivery_repeat — times this exact delivery was bowled in a row (spam)

    Returns dict: {"type": "runs"|"wicket"|"wide"|"noball"|"legbye",
                   "runs": int, "how": str, "traits_activated": [..],
                   "free_hit": bool, "mystery": bool}
    """
    # Start with base
    probs = dict(BASE)

    # Identify bowler type and phase
    bowler_key = _get_bowler_key(bowl_style)
    phase = _get_phase(over, total_overs)

    # Layer 1: Bowler type
    _apply_mods(probs, BOWLER_MODS.get(bowler_key, {}))

    # Layer 2: Variation (strip "(Surprise)" suffix if present)
    clean_var = (variation or "").replace(" (Surprise)", "").strip()
    _apply_mods(probs, VARIATION_MODS.get(clean_var, {}))

    # Layer 3: Length (pacers only)
    if length and length not in (None, "N/A", ""):
        _apply_mods(probs, LENGTH_MODS.get(length, {}))

    # Layer 4: Pitch
    pitch = pitch_type if pitch_type in PITCH_MODS else "Flat"
    _apply_mods(probs, PITCH_MODS.get(pitch, {}))

    # Layer 4b: Pitch wear (deterioration over the match)
    if pitch_wear > 5:
        _apply_mods(probs, _pitch_wear_mods(pitch, pitch_wear))

    # Layer 5: Pitch × bowler synergy
    _apply_mods(probs, PITCH_BOWLER_SYNERGY.get((pitch, bowler_key), {}))

    # Layer 6: Phase
    _apply_mods(probs, PHASE_MODS.get(phase, {}))

    # Layer 7: Shot
    # Layer 7: Shot — use DB-backed mods if available, fallback to SHOT_MODS
    _apply_mods(probs, _get_shot_mods(shot))

    # Layer 8: Rating differential — the dominance modifier
    _apply_rating_diff(probs, bat_rating, bowl_rating)

    # Layer 9: Traits (optional)
    traits_activated = []
    if striker_traits or bowler_traits:
        try:
            from services.trait_engine import apply_traits
            ctx_for_traits = trait_ctx or {}
            ctx_for_traits.setdefault("over", over)
            ctx_for_traits.setdefault("total_overs", total_overs)
            traits_activated = apply_traits(
                probs, striker_traits or [], bowler_traits or [],
                ctx_for_traits,
            )
        except Exception:
            logger.exception("Trait engine failed; ignoring traits this ball")

    # Layer 10: Admin-tuned global adjustments (cached, read once per ball)
    # These let the admin fix systemic biases without redeploying — e.g. if all
    # the per-layer dot bumps stack up too much, sim_dot_adjust=-8 brings it back.
    try:
        from services.config_service import get_config
        cfg = get_config()
        global_adj = {
            "dot": cfg.get("sim_dot_adjust", 0.0) or 0.0,
            "1":   cfg.get("sim_one_adjust", 0.0) or 0.0,
            "2":   cfg.get("sim_two_adjust", 0.0) or 0.0,
            "4":   cfg.get("sim_four_adjust", 0.0) or 0.0,
            "6":   cfg.get("sim_six_adjust", 0.0) or 0.0,
            "W":   cfg.get("sim_wicket_adjust", 0.0) or 0.0,
        }
        # Extras adjustment splits across wide/noball/legbye proportionally
        extras_adj = cfg.get("sim_extras_adjust", 0.0) or 0.0
        if extras_adj:
            global_adj["wide"] = extras_adj * 0.4
            global_adj["noball"] = extras_adj * 0.2
            global_adj["legbye"] = extras_adj * 0.4
        _apply_mods(probs, global_adj)
    except Exception:
        logger.exception("Failed to apply admin sim adjustments")

    # Layer 11: Dynamic in-match mechanics (free hit / mystery / momentum / spam)
    _apply_dynamic_mods(
        probs, free_hit=free_hit, mystery=mystery,
        recent_runs=recent_runs, consec_wickets=consec_wickets,
        delivery_repeat=delivery_repeat,
    )

    # Final normalize
    _normalize(probs)

    # Roll the dice
    r = random.random() * 100.0
    cumul = 0.0

    def _ret(d):
        d["traits_activated"] = traits_activated
        if free_hit:
            d["free_hit"] = True
        if mystery:
            d["mystery"] = True
        return d

    # Order: extras → wicket → runs (dot first since most common)
    cumul += probs["wide"]
    if r < cumul:
        return _ret({"type": "wide"})

    cumul += probs["noball"]
    if r < cumul:
        return _ret({"type": "noball", "runs": random.choice([0, 1, 1, 2, 4, 6])})

    cumul += probs["legbye"]
    if r < cumul:
        return _ret({"type": "legbye", "runs": random.choice([1, 1, 1, 2])})

    cumul += probs["W"]
    if r < cumul:
        # Free hit: the only legal dismissal is a run-out.
        if free_hit:
            return _ret({"type": "wicket", "runs": random.choice([0, 1]), "how": "Run Out"})
        # Wicket type weighted by shot + bowler context
        hows_pacer = ["Bowled", "Caught", "LBW", "Caught Behind", "Caught & Bowled"]
        hows_spin  = ["Bowled", "Caught", "LBW", "Stumped", "Caught & Bowled"]
        # Risky shots → more catches; defensive shots → more bowled/LBW
        if shot in ("Slog", "Loft", "Switch Hit", "Pull", "Hook",
                    "Upper Cut", "Slog Sweep"):
            base_hows = ["Caught", "Caught", "Caught", "Bowled", "LBW"]
        elif shot in ("Sweep", "Reverse Sweep", "Paddle"):
            base_hows = ["LBW", "Caught", "Bowled", "Caught"]
        else:
            base_hows = hows_spin if bowler_key in ("Off Spinner", "Leg Spinner") else hows_pacer
        # 5% chance of run-out on any wicket
        if random.random() < 0.05:
            return _ret({"type": "wicket", "runs": random.choice([0, 1]), "how": "Run Out"})
        return _ret({"type": "wicket", "runs": 0, "how": random.choice(base_hows)})

    cumul += probs["dot"]
    if r < cumul:
        return _ret({"type": "runs", "runs": 0})

    cumul += probs["1"]
    if r < cumul:
        return _ret({"type": "runs", "runs": 1})

    cumul += probs["2"]
    if r < cumul:
        return _ret({"type": "runs", "runs": 2})

    cumul += probs["3"]
    if r < cumul:
        return _ret({"type": "runs", "runs": 3})

    cumul += probs["4"]
    if r < cumul:
        return _ret({"type": "runs", "runs": 4})

    # 6 runs — anything left over
    return _ret({"type": "runs", "runs": 6})

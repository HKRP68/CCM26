import random
import logging
from typing import Optional
from engine.ground_config import (
    get_scoring_matrix as _gc_scoring_matrix,
    get_run_factor as _gc_run_factor,
    get_wicket_factors as _gc_wicket_factors,
    get_phase_boosts as _gc_phase_boosts,
    get_blending_weights as _gc_blending_weights,
)
from engine.game_state_engine import apply_game_state_to_probs
from engine.format_config import FormatConfig

logger = logging.getLogger(__name__)

# Tune extras frequency to target ~3-6 extras per innings (120 balls).
EXTRA_ERROR_FLOOR = 0.30
EXTRA_WEIGHT_MULTIPLIER = 2.2

# Free hit boundary boost applied independently to Four and Six weights.
FREE_HIT_BOUNDARY_BOOST = 1.75

# -----------------------------------------------------------------------------
# ball_outcome.py
#
# Implements ball-by-ball outcome logic with:
#   • 60% pitch-influence + 40% player-skill blending
#   • Detailed commentary templates
#   • Enhanced boundary & wicket chances in the final 4 overs (17–20)
#
# Pitch average ranges (T20 context):
#   - Green: 150–160 runs (pace gets help, but strike rotation keeps it competitive)
#   - Flat : 180–200 runs (batting paradise)
#   - Dry  : 120–150 runs (favors spin bowlers)
#   - Hard : 150–180 runs (balanced, slight batting edge)
#   - Dead : 200–240 runs (batting festival; very few wickets)
#
# The logic below ensures:
#   – Pitch contributes 60% to each outcome probability
#   – Player ratings (batting, bowling, fielding) contribute 40%
#   – In overs 17–20, boundary (4s/6s) chances and wicket chances are boosted
#     based on pitch type:
#       * Flat/Dead: highest boundary boost (aim ~3 boundaries/over)
#       * Hard       : moderate boundary boost (aim ~2 boundaries/over)
#       * Green/Dry  : minimal boundary boost (max ~1 boundary/over)
#     Wicket chance also increases slightly in these death overs.
#
# Print-based logging is included to trace computations at each step.
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# 1) Commentary templates for each outcome category
# -----------------------------------------------------------------------------
# 1) Commentary templates - REDUCED FOR MEMORY (Data moved to data/commentary_pack.json)
commentary_templates = {
    "Dot": ["Dot ball."],
    "Single": ["One run."],
    "Double": ["Two runs."],
    "Three": ["Three runs."],
    "Four": ["Four runs."],
    "Six": ["Six runs!"],
    "Wicket": ["Out!"],
    "Extras": ["Extra run."]
}

# -----------------------------------------------------------------------------
# 2) Pitch-influence definitions (60% weight)
# -----------------------------------------------------------------------------
PITCH_RUN_FACTOR = {
    "Green":  0.98,   # seaming/swing → par ~123 (bowler's dream)
    "Dusty":  1.04,   # sharp turn → par ~144
    "Dry":    1.00,   # slow turn → par ~155
    "Bouncy": 1.12,   # pace-friendly carry → par ~159
    "Even":   1.06,   # neutral, balanced → par ~172
    "Hard":   1.10,   # true bounce (batting edge) → par ~183
    "Flat":   1.20 * 0.90,   # batting paradise → par ~209
    "Dead":   1.30    # batting festival → ~260
}

# ---------------------------------------------------------------------
# 2) Pitch-influence definitions (60% weight)
# ---------------------------------------------------------------------

PITCH_WICKET_FACTOR = {
    "Green": {
        "Fast":         1.18,   # pace gets help, but no longer triggers collapses
        "Fast-medium":  1.10,
        "Medium-fast":  1.05,
        "default":      0.55    # spinners/pacers that don’t fit above
    },
    "Dry": {
        "Leg spin":     1.15,   # leggies turn square, highest threat
        "Wrist spin":   1.13,   # similar to leggies on a turning track
        "Off spin":     1.11,   # very effective but slightly easier than a leggie
        "Finger spin":  1.08,   # orthodox left-arm; still strong, but a bit less than right-arm
        "default":      0.70    # pace bowlers on a dry turner
    },
    "Hard": {
        "Fast":         1.10,   # pace gets decent bounce & seam, but still batsmen can score
        "Fast-medium":  1.05,
        "Medium-fast":  1.00,
        "default":      0.90    # spin/other styles on a true track
    },
    "Dusty": {
        "Leg spin":     1.18,   # worn turner — spinners influential
        "Wrist spin":   1.16,
        "Off spin":     1.14,
        "Finger spin":  1.10,
        "default":      0.82    # pace bowlers on a dusty turner
    },
    "Bouncy": {
        "Fast":         1.28,   # extra bounce rewards genuine pace
        "Fast-medium":  1.13,
        "Medium-fast":  1.08,
        "default":      0.80    # spinners on a hard, bouncy deck
    },
    "Flat": {
        # Almost no one “takes” wickets easily on Flat—batsmen dominate.
        "default":      0.88
    },
    "Even": {
        # Neutral surface — pace and spin share the wickets evenly.
        "Fast":         1.03,
        "Fast-medium":  1.01,
        "Medium-fast":  1.00,
        "Leg spin":     1.02,
        "Off spin":     1.01,
        "default":      0.95
    },
    "Dead": {
        # Very tough for bowlers on Dead track—wickets are rare
        "Fast":         0.60,
        "Fast-medium":  0.60,
        "Medium-fast":  0.60,
        "Off spin":     0.60,
        "Leg spin":     0.60,
        "Finger spin":  0.60,
        "Wrist spin":   0.60,
        "default":      0.60
    }
}

def get_pitch_run_multiplier(pitch: str, config=None) -> float:
    """
    Returns the run-friendly multiplier for the given pitch.
    Uses ground_conditions.yaml if available, falls back to hardcoded constants.
    Pass *config* to use a user-specific snapshot instead of the global config.
    """
    factor = _gc_run_factor(pitch, config=config)
    if factor is None:
        factor = PITCH_RUN_FACTOR.get(pitch, 1.0)
    return factor

def get_pitch_wicket_multiplier(pitch: str, bowling_type: str, config=None) -> float:
    """
    Returns the wicket-friendly multiplier for the given pitch and bowling type.
    Uses ground_conditions.yaml if available, falls back to hardcoded constants.
    Pass *config* to use a user-specific snapshot instead of the global config.
    """
    wf = _gc_wicket_factors(pitch, config=config)
    if wf:
        return wf.get(bowling_type, wf.get("default", 1.0))
    slot = PITCH_WICKET_FACTOR.get(pitch, {})
    return slot.get(bowling_type, slot.get("default", 1.0))

# -----------------------------------------------------------------------------
# 3) Base outcome probabilities (raw frequencies)
# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------
# 3) Pitch-specific outcome probabilities (realistic scoring patterns)
# -----------------------------------------------------------------------------
PITCH_SCORING_MATRIX = {
    # Calibrated to per-pitch first-innings means (par): Green~123 Dusty~144
    # Dry~155 Bouncy~159 Even~172 Hard~183 Flat~209. Kept in sync with the
    # authoritative ground_conditions.yaml pitch_profiles (this dict is the
    # fallback used only when the YAML config is unavailable).
    "Green": {
        "Dot":     0.394,  # Seaming/swing — pace dominates, hard to score
        "Single":  0.330,
        "Double":  0.081,
        "Three":   0.005,
        "Four":    0.056,
        "Six":     0.026,
        "Wicket":  0.048,
        "Extras":  0.060
    },
    "Dusty": {
        "Dot":     0.348,  # Sharp turn — spin-attack, low-scoring dogfight
        "Single":  0.340,
        "Double":  0.092,
        "Three":   0.006,
        "Four":    0.078,
        "Six":     0.042,
        "Wicket":  0.044,
        "Extras":  0.050
    },
    "Dry": {
        "Dot":     0.324,  # Slow turn — spin bites in the middle overs
        "Single":  0.350,
        "Double":  0.101,
        "Three":   0.006,
        "Four":    0.085,
        "Six":     0.044,
        "Wicket":  0.040,
        "Extras":  0.050
    },
    "Bouncy": {
        "Dot":     0.336,  # Extra bounce — pace-friendly, tricky strokeplay
        "Single":  0.350,
        "Double":  0.099,
        "Three":   0.006,
        "Four":    0.079,
        "Six":     0.045,
        "Wicket":  0.040,
        "Extras":  0.045
    },
    "Even": {
        "Dot":     0.323,  # Neutral, balanced — the standard T20 track
        "Single":  0.350,
        "Double":  0.106,
        "Three":   0.006,
        "Four":    0.086,
        "Six":     0.053,
        "Wicket":  0.036,
        "Extras":  0.040
    },
    "Hard": {
        "Dot":     0.335,  # True bounce, good carry — slight batting edge
        "Single":  0.350,
        "Double":  0.099,
        "Three":   0.006,
        "Four":    0.088,
        "Six":     0.053,
        "Wicket":  0.034,
        "Extras":  0.035
    },
    "Flat": {
        "Dot":     0.351,  # Batting paradise — 200+ thrillers
        "Single":  0.340,
        "Double":  0.097,
        "Three":   0.006,
        "Four":    0.091,
        "Six":     0.060,
        "Wicket":  0.030,
        "Extras":  0.025
    },
    "Dead": {
        # Batting paradise (200+ average, ~4-5 wickets)
        "Dot":     0.18,
        "Single":  0.320,
        "Double":  0.145,
        "Three":   0.005,  # ~0.6 threes per innings (very rare)
        "Four":    0.19,
        "Six":     0.10,
        "Wicket":  0.03,
        "Extras":  0.03
    }
}

# Fallback matrix for unknown pitch types (CORRECTED)
DEFAULT_SCORING_MATRIX = {
    "Dot":     0.27,   # Increased from 0.25
    "Single":  0.352,   # Absorbed most of Three reduction
    "Double":  0.13,   # Absorbed some of Three reduction
    "Three":   0.008,  # ~1 three per innings (was 0.06 — far too high)
    "Four":    0.09,   # Increased from 0.08
    "Six":     0.05,   # Increased from 0.04
    "Wicket":  0.05,   # Rounded from 0.044
    "Extras":  0.05    # Rounded from 0.048
    # Sum: ~1.00 ✅
}


# =============================================================================
# LIST A (50-OVER) SCORING MATRICES
# =============================================================================
# Three phase-specific matrices replace the single T20 matrix.
# Phase selection is driven by FormatConfig.get_phase(over).
#
# Target scoring profile (Hard pitch, 1st innings, neutral conditions):
#   PP1    (overs  0- 9): ~62 runs  — 6.2 RPO  [new ball, field restrictions]
#   Middle (overs 10-39): ~126 runs — 5.0 RPO  [consolidation, spin, dots]
#   Death  (overs 40-49): ~102 runs — 8.5 RPO  [slog, 5 outside 30-yard]
#   Total expected:       ~290 runs
# =============================================================================

# PP1 — overs 0-9 (Mandatory Powerplay: 2 fielders outside 30-yard circle)
# New ball, measured aggression. Strike rotation and 2s dominate over big hitting.
LISTA_PP1_MATRIX = {
    "Dot":    0.338,
    "Single": 0.413,
    "Double": 0.118,
    "Three":  0.004,
    "Four":   0.068,
    "Six":    0.018,
    "Wicket": 0.024,
    "Extras": 0.017,
}

# Middle — overs 10-39 (4 fielders permitted outside 30-yard circle)
# Consolidation phase. Dot/single pressure with steady doubles and very few boundaries.
LISTA_MIDDLE_MATRIX = {
    "Dot":    0.392,
    "Single": 0.443,
    "Double": 0.117,
    "Three":  0.004,
    "Four":   0.024,
    "Six":    0.006,
    "Wicket": 0.012,
    "Extras": 0.002,
}

# Death — overs 40-49 (5 fielders permitted outside 30-yard circle)
# Acceleration phase. Boundaries rise, but singles/doubles still matter.
LISTA_DEATH_MATRIX = {
    "Dot":    0.250,
    "Single": 0.354,
    "Double": 0.123,
    "Three":  0.006,
    "Four":   0.098,
    "Six":    0.052,
    "Wicket": 0.069,
    "Extras": 0.048,
}

# Per-pitch run scaling for ListA (applied on top of phase matrices)
# These reflect how pitch character shifts scoring across 50 overs.
# Must be consistent with _LISTA_PITCH_PAR_FACTORS in format_config.py:
#   Hard ≈ 0.98 → ~285 runs  (par factor 1.00)
#   Flat ≈ 1.21 → ~320 runs  (par factor 1.10)
#   Dead ≈ 1.18 → ~340 runs  (par factor 1.18) ← was 0.68 (contradicted par)
#   Green ≈ 0.68 → ~220 runs (par factor 0.76)
#   Dry   ≈ 0.72 → ~230 runs (par factor 0.80)
LISTA_RUN_FACTORS = {
    "Green": 0.68,   # Strong bowler-friendly suppression
    "Dry":   0.72,   # Spin-friendly
    "Even":  0.92,   # Neutral, balanced
    "Hard":  0.98,   # Baseline 280-320 target band
    "Flat":  1.21,   # High-scoring 320-360 target band
    "Dead":  1.18,   # Batting festival — aligns with par factor 1.18 (~340 runs)
}

# ListA pitch-specific nudges for strike rotation profile.
# Applied before final normalization of raw weights.
# Dead removed: it is now a batting paradise (run_factor 1.18) — dots must NOT
# be boosted. Green/Dry remain: tight bowling on seam/spin surfaces is realistic.
LISTA_DOT_SINGLE_FACTORS = {
    "Green": {"Dot": 1.22, "Single": 1.06},
    "Dry":   {"Dot": 1.20, "Single": 1.08},
}

# ListA-only wicket scaling by pitch (applied as a final scaling layer).
# Dead corrected: batting paradise → very low wicket rate (like Flat, even lower).
# Green/Dry: bowling-friendly → higher wicket rate.
LISTA_WICKET_PITCH_MULT = {
    "Green": 1.18,
    "Dry":   1.12,
    "Hard":  1.00,
    "Flat":  0.70,
    "Dead":  0.58,   # Batting festival: wickets rarer than Flat
}


def _get_lista_matrix(over: int, fmt) -> dict:
    """
    Return the appropriate ListA scoring matrix for the given over.

    Uses FormatConfig phase detection so the logic is driven by the
    format definition, not hardcoded over ranges.
    """
    if fmt.is_death(over):
        return LISTA_DEATH_MATRIX
    if fmt.is_powerplay(over):
        return LISTA_PP1_MATRIX
    return LISTA_MIDDLE_MATRIX


def _apply_lista_phase_boosts(weights: dict, over: int, pitch: str,
                               innings: int, fmt) -> dict:
    """
    Apply mild ListA phase nudges to raw outcome weights.

    The phase character is already embedded in the three ListA matrices
    (PP1 / Middle / Death).  These boosts are small pitch-sensitive adjustments
    on top, not primary scoring drivers.

    PP1   : Slight boundary edge from new ball on friendly pitches.
    Middle: Tiny dot-ball nudge (spinners tightening).
    Death : Modest boundary/wicket uplift; 2nd-innings pressure handling.
    """
    w = dict(weights)

    if fmt.is_powerplay(over):
        # New-ball edges through gully; pace on batting pitches = more boundaries
        if pitch == "Flat":
            w["Four"] = w.get("Four", 0) * 1.14
            w["Six"] = w.get("Six", 0) * 1.12
        elif pitch == "Hard":
            w["Four"] = w.get("Four", 0) * 1.10
            w["Six"] = w.get("Six", 0) * 1.08
        w["Wicket"] = w.get("Wicket", 0) * 1.05

    elif fmt.is_death(over):
        # Death matrix already encodes the slog aggression.
        # Only a small pitch-sensitive wicket nudge and 2nd-innings urgency.
        if pitch == "Flat":
            w["Four"] = w.get("Four", 0) * 1.10
            w["Six"]  = w.get("Six",  0) * 1.15
        w["Wicket"] = w.get("Wicket", 0) * 1.05   # Mild risk-taking spike

        # 2nd-innings ListA pressure:
        # avoid a blanket scoring boost while chasing; scoreboard pressure
        # in long chases typically raises dot/wicket risk.
        if innings == 2:
            w["Dot"] = w.get("Dot", 0) * 1.03
            w["Wicket"] = w.get("Wicket", 0) * 1.08

    else:
        # Middle overs: spin grip and tight fielding = slightly more dots
        w["Dot"] = w.get("Dot", 0) * 1.05

    # Dead is a batting paradise (run_factor 1.18, wicket_mult 0.58).
    # No further suppression applied here; the phase matrices and scaling
    # layers already produce the correct low-dot, low-wicket, high-boundary profile.

    return w


def _apply_lista_pitch_wear(weights: dict, pitch: str,
                             pitch_wear: float) -> dict:
    """
    Progressive pitch wear for ListA (0-300 balls, normalised to [0,1]).

    Unlike T20 (where wear is mild), ListA wear has a pronounced late phase:
      - Green : seam fades after over 20 (wear ~0.40). Batting improves.
      - Dry   : spin gets genuinely unplayable by over 30+ (wear ~0.60).
                Wickets and dots escalate sharply.
      - Hard  : modest steady deterioration across the innings.
      - Flat/Dead: minor wear; pitch remains batting-friendly throughout.

    pitch_wear is the fraction of total balls already bowled (0=fresh, 1=done).
    """
    if pitch_wear <= 0.0:
        return weights

    w = dict(weights)
    pw = pitch_wear

    if pitch == "Dry":
        # Spin track becomes brutal in second half of innings
        # Wickets  : up to +45% by over 50
        # Dots     : up to +20% by over 50
        # Sixes    : down up to -25% (near-impossible to score freely)
        w["Wicket"] = w.get("Wicket", 0) * (1.0 + 0.45 * pw)
        w["Dot"]    = w.get("Dot",    0) * (1.0 + 0.20 * pw)
        w["Six"]    = w.get("Six",    0) * (1.0 - 0.25 * pw)
        w["Four"]   = w.get("Four",   0) * (1.0 - 0.12 * pw)

    elif pitch == "Green":
        # Old ball: seam fades, batting gets easier in overs 25+
        # Meaningful only from wear ~0.40 onwards (over 20 of 50)
        if pw > 0.30:
            late_wear = (pw - 0.30) / 0.70   # 0→1 over overs 15-50
            w["Wicket"] = w.get("Wicket", 0) * (1.0 - 0.18 * late_wear)
            w["Four"]   = w.get("Four",   0) * (1.0 + 0.10 * late_wear)
            w["Six"]    = w.get("Six",    0) * (1.0 + 0.08 * late_wear)

    elif pitch == "Hard":
        # True track deteriorates gently; reversal may assist pace later
        w["Wicket"] = w.get("Wicket", 0) * (1.0 + 0.12 * pw)
        w["Dot"]    = w.get("Dot",    0) * (1.0 + 0.05 * pw)

    elif pitch in ("Flat", "Dead"):
        # Batting-friendly pitches barely deteriorate
        w["Wicket"] = w.get("Wicket", 0) * (1.0 - 0.08 * pw)
        w["Four"]   = w.get("Four",   0) * (1.0 + 0.05 * pw)

    # Re-normalise to preserve total weight
    orig_total = sum(weights.values())
    new_total  = sum(w.values())
    if new_total > 0 and orig_total > 0:
        scale = orig_total / new_total
        w = {k: v * scale for k, v in w.items()}

    return w


def _apply_dew_factor(weights: dict, innings: int, over: int,
                      is_day_night: bool, fmt) -> dict:
    """
    Dew factor for Day-Night ListA matches (floodlit evening conditions).

    Physics: surface moisture makes the ball slippery after ~25 overs of the
    2nd innings. Spin grips less, pace loses control (more wides), batting
    becomes easier — classic ODI D/N swing to the chasing side.

    Effect kicks in from over 25 of the 2nd innings in D/N matches.
    Intensity scales linearly up to full effect at over 45.

    Changes:
      Extras  : +40% (wides from wet ball)
      Wicket  : -15% (harder for spinners to grip)
      Four    : +10% (easier to time on damp outfield)
    """
    if not is_day_night or innings != 2:
        return weights

    dew_start = 24   # 0-based over index (= over 25)
    dew_peak  = 44   # full effect by over 45
    if over < dew_start:
        return weights

    intensity = min((over - dew_start) / max(dew_peak - dew_start, 1), 1.0)
    w = dict(weights)
    w["Extras"] = w.get("Extras", 0) * (1.0 + 0.40 * intensity)
    w["Wicket"] = w.get("Wicket", 0) * (1.0 - 0.15 * intensity)
    w["Four"]   = w.get("Four",   0) * (1.0 + 0.10 * intensity)

    # Re-normalise
    orig_total = sum(weights.values())
    new_total  = sum(w.values())
    if new_total > 0 and orig_total > 0:
        scale = orig_total / new_total
        w = {k: v * scale for k, v in w.items()}

    return w


def _validate_scoring_matrices():
    """Validate that all pitch scoring matrices sum to 1.0"""
    logger.info("Validating pitch scoring matrices:")

    for pitch_type, matrix in PITCH_SCORING_MATRIX.items():
        total = sum(matrix.values())
        status = "OK" if abs(total - 1.0) < 0.001 else "FAIL"
        logger.info(f"  {pitch_type}: {total:.3f} [{status}]")

        if abs(total - 1.0) >= 0.001:
            logger.warning(f"    Warning: {pitch_type} matrix doesn't sum to 1.0!")

    # Validate ListA phase matrices
    for label, matrix in [("ListA-PP1", LISTA_PP1_MATRIX),
                           ("ListA-Middle", LISTA_MIDDLE_MATRIX),
                           ("ListA-Death", LISTA_DEATH_MATRIX)]:
        total = sum(matrix.values())
        status = "OK" if abs(total - 1.0) < 0.001 else "FAIL"
        logger.info(f"  {label}: {total:.3f} [{status}]")
        if abs(total - 1.0) >= 0.001:
            logger.warning(f"    Warning: {label} matrix doesn't sum to 1.0!")

    # Validate default matrix too
    default_total = sum(DEFAULT_SCORING_MATRIX.values())
    # print(f"  DEFAULT: {default_total:.3f} {'OK' if abs(default_total - 1.0) < 0.1 else 'FAIL'}")

# Validate matrices on module import (runs once when ball_outcome.py is imported)
_validate_scoring_matrices()


# -----------------------------------------------------------------------------
# Feature 3: Pitch deterioration function
# -----------------------------------------------------------------------------
def _apply_pitch_wear(raw_weights: dict, pitch_type: str, pitch_wear: float) -> dict:
    """
    Apply pitch-deterioration effects to raw outcome probability weights.

    pitch_wear is a float in [0.0, 1.0] representing how worn the surface is
    (0.0 = fresh, 1.0 = fully worn after 120 balls of the innings).

    Effects by pitch type:
      Dry   – spin deterioration → wickets and dots increase with wear
      Green – old ball eases seam movement → batting gets slightly easier
      Flat/Dead – batting-friendly surface gets even more so
      Hard  – slight deterioration; wickets and dots creep up

    Weights are re-normalised after adjustment so they remain proportional.
    """
    if pitch_wear <= 0.0:
        return raw_weights

    adjusted = dict(raw_weights)
    w = pitch_wear  # shorthand

    if pitch_type == "Dry":
        # Spin track worsens for batting: wickets and dots go up
        adjusted["Wicket"] = adjusted.get("Wicket", 0) * (1.0 + 0.30 * w)
        adjusted["Dot"]    = adjusted.get("Dot",    0) * (1.0 + 0.15 * w)

    elif pitch_type == "Green":
        # Seam movement reduces as ball gets older; batting becomes easier
        adjusted["Four"]   = adjusted.get("Four",   0) * (1.0 + 0.10 * w)
        adjusted["Six"]    = adjusted.get("Six",    0) * (1.0 + 0.08 * w)
        adjusted["Wicket"] = adjusted.get("Wicket", 0) * (1.0 - 0.20 * w)

    elif pitch_type in ("Flat", "Dead"):
        # Already batting-friendly; gets marginally more so with wear
        adjusted["Four"]   = adjusted.get("Four",   0) * (1.0 + 0.08 * w)
        adjusted["Six"]    = adjusted.get("Six",    0) * (1.0 + 0.08 * w)
        adjusted["Wicket"] = adjusted.get("Wicket", 0) * (1.0 - 0.10 * w)

    elif pitch_type == "Hard":
        # Balanced track deteriorates slightly; wickets and dots increase
        adjusted["Wicket"] = adjusted.get("Wicket", 0) * (1.0 + 0.10 * w)
        adjusted["Dot"]    = adjusted.get("Dot",    0) * (1.0 + 0.05 * w)

    # Re-normalise so total weight is preserved (proportional scaling)
    orig_total = sum(raw_weights.values())
    new_total  = sum(adjusted.values())
    if new_total > 0 and orig_total > 0:
        scale    = orig_total / new_total
        adjusted = {k: v * scale for k, v in adjusted.items()}

    return adjusted


# -----------------------------------------------------------------------------
# Rating-based performance gating
# -----------------------------------------------------------------------------
# Product rule (applies to every game mode that funnels through calculate_outcome:
# /cipl, /c<league>, /letsplay, /wpmbot, /wpm, /vsbot, /playmatch, /sim):
#
#   • A batsman whose Batting Rating is below WEAK_BATTING_THRESHOLD struggles:
#       fewer 4s/6s, strike rate dragged below a run-a-ball (more dots, fewer
#       2s/3s), and they fall early/cheaply (higher wicket chance).
#   • A bowler whose Bowling Rating is below WEAK_BOWLING_THRESHOLD leaks runs:
#       more boundaries/singles conceded, fewer dots, and fewer wickets.
#   • The reverse rewards quality players (a strong batsman vs a weak bowler is
#       rewarded), so the rating contest is sharpened in both directions.
#
# Effects scale with how far the rating sits past the threshold, and weights are
# renormalised afterwards so total probability mass is preserved.
WEAK_BATTING_THRESHOLD = 40
WEAK_BOWLING_THRESHOLD = 55
# Quality players (vice-versa) earn a milder edge above these marks.
STRONG_BATTING_THRESHOLD = 75
STRONG_BOWLING_THRESHOLD = 80


def _apply_rating_performance_modifiers(weights: dict,
                                        batting_rating: float,
                                        bowling_rating: float) -> dict:
    """Sharpen how raw player ratings shape a delivery's outcome weights.

    See the module-level note above WEAK_BATTING_THRESHOLD for the product rule.
    The adjustments are multiplicative on the raw outcome weights and are
    renormalised at the end so the total probability mass is unchanged — only
    the *distribution* between dots/runs/boundaries/wickets shifts.
    """
    w = dict(weights)

    def _scale(key: str, factor: float) -> None:
        if key in w:
            w[key] *= factor

    # --- Weak batsman (batting_rating < 40): scores less, gets out early ---
    if batting_rating < WEAK_BATTING_THRESHOLD:
        # deficit in [0,1]: rating 40 → 0.0, rating 20 → 0.5, rating 0 → 1.0
        deficit = min((WEAK_BATTING_THRESHOLD - batting_rating) / 40.0, 1.0)
        _scale("Six",    1.0 - 0.70 * deficit)   # big hits dry up
        _scale("Four",   1.0 - 0.55 * deficit)   # fewer fours
        _scale("Three",  1.0 - 0.40 * deficit)
        _scale("Double", 1.0 - 0.30 * deficit)   # strike rate dragged down
        _scale("Single", 1.0 - 0.12 * deficit)
        _scale("Dot",    1.0 + 0.45 * deficit)   # many more dots → SR < 100
        _scale("Wicket", 1.0 + 0.85 * deficit)   # falls early / cheaply
    # --- Quality batsman (vice-versa): mild scoring edge ---
    elif batting_rating >= STRONG_BATTING_THRESHOLD:
        surplus = min((batting_rating - STRONG_BATTING_THRESHOLD) / 25.0, 1.0)
        _scale("Six",    1.0 + 0.20 * surplus)
        _scale("Four",   1.0 + 0.15 * surplus)
        _scale("Three",  1.0 + 0.08 * surplus)   # better strike rotation
        _scale("Double", 1.0 + 0.06 * surplus)
        _scale("Single", 1.0 + 0.03 * surplus)
        _scale("Dot",    1.0 - 0.10 * surplus)
        _scale("Wicket", 1.0 - 0.15 * surplus)

    # --- Weak bowler (bowling_rating < 55): concedes more, strikes less ---
    if bowling_rating < WEAK_BOWLING_THRESHOLD:
        # deficit in [0,1]: rating 55 → 0.0, rating ~27 → 0.5, rating 0 → 1.0
        deficit = min((WEAK_BOWLING_THRESHOLD - bowling_rating) / 55.0, 1.0)
        _scale("Six",    1.0 + 0.45 * deficit)   # leaks boundaries
        _scale("Four",   1.0 + 0.40 * deficit)
        _scale("Single", 1.0 + 0.12 * deficit)
        _scale("Dot",    1.0 - 0.25 * deficit)   # harder to bowl dots
        _scale("Wicket", 1.0 - 0.45 * deficit)   # fewer wickets
    # --- Quality bowler (vice-versa): mild containment/strike edge ---
    elif bowling_rating >= STRONG_BOWLING_THRESHOLD:
        surplus = min((bowling_rating - STRONG_BOWLING_THRESHOLD) / 20.0, 1.0)
        _scale("Dot",    1.0 + 0.15 * surplus)
        _scale("Wicket", 1.0 + 0.20 * surplus)
        _scale("Single", 1.0 - 0.06 * surplus)   # chokes strike rotation
        _scale("Four",   1.0 - 0.12 * surplus)
        _scale("Six",    1.0 - 0.15 * surplus)

    # Renormalise so total probability mass is preserved (relative shift only).
    orig_total = sum(weights.values())
    new_total = sum(w.values())
    if new_total > 0 and orig_total > 0:
        scale = orig_total / new_total
        w = {k: v * scale for k, v in w.items()}

    return w


# Batting position context multipliers (Feature 9)
# Top-order batters have higher baseline impact; tail-enders are penalised.
_POS_BATTING_MULT: dict = {
    1: 1.05, 2: 1.05, 3: 1.03, 4: 1.02,
    5: 1.00, 6: 0.98, 7: 0.95, 8: 0.90,
    9: 0.85, 10: 0.80, 11: 0.75,
}


# -----------------------------------------------------------------------------
# Multiplicative skill model (T20 / legacy path)
# -----------------------------------------------------------------------------
# The base matrix already encodes the pitch's shape, and the pitch run/wicket
# multiplier sets its scoring level. On top of that, skill enters as a TRUE
# MULTIPLIER centred on 1.0 at rating parity (effective_batting == bowling):
#
#     weight(outcome) = base_prob × pitch_factor × (skill_ratio ** exponent)
#
# The exponent decides how hard each outcome responds to the batting-vs-bowling
# contest — boundaries swing hardest, singles barely move, and wickets are keyed
# off the bowler's edge. This makes the rating contest decisive across the WHOLE
# range (a star is clearly better than an average bat, a rabbit clearly worse)
# instead of the old additive 60/40 blend that flattened ratings 55-90.
#
# Scale every exponent with SKILL_MODEL_STRENGTH for one-knob tuning.
SKILL_MODEL_STRENGTH = 1.5
_SKILL_RUN_EXP = {
    "Dot":    -0.42,
    "Single":  0.10,
    "Double":  0.30,
    "Three":   0.40,
    "Four":    0.66,
    "Six":     0.78,
}
_SKILL_WICKET_EXP = 0.40
# Clamp the ratio so freak mismatches can't produce absurd weights. Bounds span
# the full 30-100 rating range (30/100 = 0.30 .. 100/30 = 3.33, a reciprocal
# pair) so even lopsided-but-legal matchups keep scaling; values outside the
# rating range are still guarded.
_SKILL_RATIO_CLAMP = (0.30, 3.33)
# Hard pitch keeps a mild batting tilt (true track, pace gets less help).
_HARD_BAT_RATIO_BOOST = 1.04
_HARD_WICKET_SKILL_MULT = 0.92

# Absolute-class layer. The skill RATIO above is 1.0 at parity regardless of
# level, which made a 60-vs-60 match statistically identical to a 90-vs-90
# one. These exponents key selected outcomes to the batter's (or bowler's)
# absolute rating, anchored at 75: elite line-ups hit more boundaries and
# leave fewer balls dead; weak line-ups scratch around for singles.
_LEVEL_ANCHOR = 75.0
# Upper bound leaves headroom for confidence/position multipliers on 90+
# batters (otherwise a set 90 and a set 99 clamp to the same level).
_LEVEL_RATIO_CLAMP = (0.45, 1.50)
_LEVEL_RUN_EXP = {
    "Six":    0.50,
    "Four":   0.32,
    "Double": 0.15,
    "Dot":   -0.22,
}
# Wicket: quality bowling strikes more, but a classy batter resists — the two
# nearly cancel at parity so the wicket rate stays calibrated across levels.
_LEVEL_WICKET_BOWL_EXP = 0.25
_LEVEL_WICKET_BAT_EXP = 0.15

# T20 wicket realism lift (non-ListA path only) — see the recalibration note
# where it is applied in calculate_outcome. Raised from 1.30 when the duel
# layer landed: state-mixing + momentum feedback pushed parity scores up
# ~30 runs and wickets down; this pulls the anchor back to ~210 @ ~4.5.
T20_WICKET_REALISM_MULT = 1.15
# Companion re-anchor applied alongside the wicket lift (duel state-mixing
# inflates boundary conversion at parity; trim it back to the calibration).
T20_DUEL_RECAL = {"Four": 0.90, "Six": 0.88, "Dot": 1.10}

# Hardcore rating-vs-rating gap steepener (engine.rating_duel.gap_factor).
# Multiplies the ratio-based skill model by exp(K * tanh(gap/17)) so single
# rating points register and big gaps become decisive (EXTREME dominance).
# K per outcome; Wicket uses the bowler-signed gap.
_GAP_STEEP_EXP = {
    "Six":    0.55,
    "Four":   0.45,
    "Three":  0.30,
    "Double": 0.25,
    "Single": 0.05,
    "Dot":   -0.40,
}
_GAP_STEEP_WICKET_EXP = 0.40


def _skill_multiplier(outcome_type: str, effective_batting: float,
                      bowling: float, pitch: str, bowling_type: str,
                      config=None) -> float:
    """Return ``pitch_factor × skill_ratio**exponent`` for the multiplicative
    model. ``skill_ratio`` is batting/bowling for run outcomes (bowling/batting
    for wickets), so the factor is 1.0 at rating parity and scales smoothly."""
    from engine.rating_duel import (
        gap_factor, batting_tier_mults, bowling_tier_mults)
    import math as _math
    lo, hi = _SKILL_RATIO_CLAMP
    lvl_lo, lvl_hi = _LEVEL_RATIO_CLAMP
    bat_level = max(lvl_lo, min(lvl_hi, effective_batting / _LEVEL_ANCHOR))
    bowl_level = max(lvl_lo, min(lvl_hi, bowling / _LEVEL_ANCHOR))
    gapf = gap_factor(effective_batting, bowling)   # (-1,1), + = batter on top
    bt = batting_tier_mults(effective_batting)
    wt = bowling_tier_mults(bowling)
    if outcome_type == "Wicket":
        ratio = max(lo, min(hi, bowling / max(1.0, effective_batting)))
        skill_mult = ratio ** (_SKILL_WICKET_EXP * SKILL_MODEL_STRENGTH)
        if pitch == "Hard":
            skill_mult *= _HARD_WICKET_SKILL_MULT
        skill_mult *= (bowl_level ** _LEVEL_WICKET_BOWL_EXP
                       / bat_level ** _LEVEL_WICKET_BAT_EXP)
        # Hardcore gap steepener + class tiers (both disciplines).
        skill_mult *= _math.exp(_GAP_STEEP_WICKET_EXP * -gapf)
        skill_mult *= bt["wicket"] * wt["wicket"]
        pitch_factor = get_pitch_wicket_multiplier(pitch, bowling_type, config=config)
    else:
        ratio = effective_batting / max(1.0, bowling)
        if pitch == "Hard":
            ratio *= _HARD_BAT_RATIO_BOOST
        ratio = max(lo, min(hi, ratio))
        exp = _SKILL_RUN_EXP.get(outcome_type, 0.0) * SKILL_MODEL_STRENGTH
        skill_mult = ratio ** exp
        skill_mult *= bat_level ** _LEVEL_RUN_EXP.get(outcome_type, 0.0)
        # Hardcore gap steepener + class tiers.
        skill_mult *= _math.exp(_GAP_STEEP_EXP.get(outcome_type, 0.0) * gapf)
        if outcome_type == "Four":
            skill_mult *= bt["four"] * wt["four"]
        elif outcome_type == "Six":
            skill_mult *= bt["six"] * wt["six"]
        elif outcome_type == "Dot":
            skill_mult *= wt["dot"]
        pitch_factor = get_pitch_run_multiplier(pitch, config=config)
    return pitch_factor * skill_mult


# -----------------------------------------------------------------------------
# 4) Compute blended probability weight for a single outcome
# -----------------------------------------------------------------------------
def compute_weighted_prob(
    outcome_type: str,
    base_prob: float,
    batting: int,
    bowling: int,
    fielding: int,
    pitch: str,
    bowling_type: str,
    streak: dict,
    batter_runs: int = 0,
    balls_faced: int = 0,
    format_name: Optional[str] = None,
    config=None,
) -> float:
    """
    Returns a raw weight for one outcome (Dot/Single/Double/Three/Four/Six/Wicket/Extras),
    combining pitch-influence + player-skill.
    Includes special handling for "Hard" pitch (80/20 split), new-batter vulnerability,
    and graduated confidence curve.
    """
    # 0) Batter innings phase modifiers
    effective_batting = batting

    _is_lista = (format_name == "ListA")

    # New batter vulnerability: first 5 balls are dangerous.
    # ListA uses softer penalties than T20 to avoid middle-order wipeouts.
    if balls_faced <= 2:
        effective_batting *= 0.88 if _is_lista else 0.82
    elif balls_faced <= 5:
        effective_batting *= 0.94 if _is_lista else 0.90

    # Graduated confidence based on runs scored.
    # ListA keeps this curve flatter to reduce opener snowballing.
    if batter_runs >= 50:
        effective_batting *= 1.10 if _is_lista else 1.20
    elif batter_runs >= 35:
        effective_batting *= 1.07 if _is_lista else 1.15
    elif batter_runs >= 20:
        effective_batting *= 1.05 if _is_lista else 1.10
    elif batter_runs >= 10:
        effective_batting *= 1.02 if _is_lista else 1.05

    # Balls-faced confidence layer (independent of runs).
    if balls_faced >= 20:
        effective_batting *= 1.02 if _is_lista else 1.05
    elif balls_faced >= 12:
        effective_batting *= 1.01 if _is_lista else 1.03

    # Extras depend on bowler error, not the batting contest — handle first.
    if outcome_type == "Extras":
        error_rate = max(EXTRA_ERROR_FLOOR, (100 - bowling) / 100.0)
        raw_weight = base_prob * error_rate * EXTRA_WEIGHT_MULTIPLIER
        return max(raw_weight, 0.0)

    # 1) Combine pitch + skill into a single per-outcome factor.
    if _is_lista:
        # ListA keeps the additive 60/40 blend (it has its own phase matrix +
        # run/wicket scaling layers, tuned separately from the T20 model).
        if outcome_type == "Wicket":
            skill_frac = (bowling / (effective_batting + bowling)
                          if (effective_batting + bowling) > 0 else 0.5)
        else:
            skill_frac = (effective_batting / (effective_batting + bowling)
                          if (effective_batting + bowling) > 0 else 0.5)
        # ListA handles pitch separately downstream → pitch_frac neutral here.
        _weights = _gc_blending_weights(config=config)
        alpha = _weights[0] if _weights else 0.6
        beta = _weights[1] if _weights else 0.4
        blended_frac = (alpha * 1.0) + (beta * skill_frac)
    else:
        # T20 / legacy: TRUE MULTIPLICATIVE model (see _skill_multiplier).
        blended_frac = _skill_multiplier(
            outcome_type, effective_batting, bowling, pitch, bowling_type,
            config=config)

    # 2) Streak boosts/penalties (unchanged).
    boundary_penalty = 1.0
    if outcome_type in ("Four", "Six") and streak.get("boundaries", 0) >= 2:
        boundary_penalty = 0.8

    wicket_boost = 1.0
    if outcome_type == "Wicket" and streak.get("boundaries", 0) >= 2:
        wicket_boost = 1.5

    raw_weight = base_prob * blended_frac * boundary_penalty * wicket_boost

    return max(raw_weight, 0.0)

# -----------------------------------------------------------------------------
# 4b) Wicket type selection based on bowling style
# -----------------------------------------------------------------------------
def _get_wicket_type_by_bowling(bowling_type: str):
    """Return (types, weights) for wicket dismissal based on bowling style.

    Includes Stumped as a dismissal mode. Spinners produce far more stumpings
    than pace bowlers, matching real T20 cricket patterns.
    """
    if bowling_type in ("Fast", "Fast-medium", "Medium-fast"):
        # Pace bowlers: more bowled/LBW, very few stumpings
        types   = ["Caught", "Bowled", "LBW", "Run Out", "Stumped"]
        weights = [0.40,     0.28,     0.20,   0.08,      0.04]
    elif bowling_type in ("Off spin", "Leg spin", "Finger spin", "Wrist spin"):
        # Spinners: high stumping rate, more caught (bat-pad)
        types   = ["Caught", "Stumped", "Bowled", "LBW", "Run Out"]
        weights = [0.30,     0.25,      0.18,    0.15,   0.12]
    else:
        # Medium pace / default: balanced distribution
        types   = ["Caught", "Bowled", "LBW", "Run Out", "Stumped"]
        weights = [0.35,     0.25,     0.20,   0.10,      0.10]
    return types, weights

# -----------------------------------------------------------------------------
# 5) Main outcome selection function: calculate_outcome
# -----------------------------------------------------------------------------
def calculate_outcome(
    batter: dict,
    bowler: dict,
    pitch: str,
    streak: dict,
    over_number: int,
    batter_runs: int,
    innings: int = 1,
    pressure_effects: dict = None,
    allow_extras: bool = True,
    free_hit: bool = False,
    balls_faced: int = 0,
    game_state: dict = None,
    pitch_wear: float = 0.0,
    batting_position: int = 5,
    game_mode_override: str = None,
    fielding_quality: float = None,
    ground_config_override: dict = None,
    format_config: Optional[FormatConfig] = None,
    is_day_night: bool = False,
    batting_approach: str = None,
    bowling_approach: str = None,
    approach_context: dict = None,
    weight_hook=None,
) -> dict:
    """
    Determines the outcome of a single delivery.
    Returns a dict:
      - "type"       ∈ {"run", "wicket", "extra"}
      - "runs"       ∈ {0,1,2,3,4,6}
      - "description": string commentary
      - "wicket_type": if a wicket, one of ["Caught","Bowled","LBW","Run Out"], else None
      - "is_extra"   ∈ {True, False}
      - "batter_out" ∈ {True, False}

    In the final 4 overs (over_number >= 16), boundary (4/6) and wicket probabilities
    are boosted based on pitch type:
      • Flat/Dead: largest boundary boost
      • Hard     : moderate boundary boost
      • Green/Dry: minimal boundary boost (max ~1 boundary/over)
      • Wicket   : slight boost in all cases
    """
    # print("\n==================== New Delivery ====================")
    # print(f"Ball context -> Over: {over_number + 1}, BatterRunsSoFar: {batter_runs}")
    # print(f"Batter: {batter['name']}, BattingRating: {batter['batting_rating']}, BattingHand: {batter['batting_hand']}")
    # print(f"Bowler: {bowler['name']}, BowlingRating: {bowler['bowling_rating']}, FieldingRating: {bowler['fielding_rating']}, BowlingHand: {bowler['bowling_hand']}, BowlingType: {bowler['bowling_type']}")
    # print(f"Pitch type: {pitch}, Current Streak: {streak}")

    # 1) Unpack numeric ratings & attributes
    # Feature 9: batting position context — top-order batters have a higher
    # effective batting rating; tail-enders face a modest penalty.
    _lista_pos_mult = {
        1: 1.02, 2: 1.02, 3: 1.01, 4: 1.00, 5: 1.00,
        6: 0.99, 7: 0.97, 8: 0.94, 9: 0.90, 10: 0.86, 11: 0.82,
    }
    _pos_mult = (
        _lista_pos_mult.get(batting_position, 1.00)
        if (format_config is not None and format_config.name == "ListA")
        else _POS_BATTING_MULT.get(batting_position, 1.00)
    )
    batting = batter["batting_rating"] * _pos_mult
    bowling = bowler["bowling_rating"]
    fielding = bowler["fielding_rating"]
    batting_hand = batter["batting_hand"]
    bowling_hand = bowler["bowling_hand"]
    bowling_type = bowler["bowling_type"]

    # 2) Select scoring matrix — format-aware.
    #    ListA: phase-specific matrix (PP1 / Middle / Death) scaled by pitch run factor.
    #    T20 / legacy: ground_conditions.yaml → hardcoded matrix (existing path).
    _gc = ground_config_override  # shorthand; None → global config cache
    _is_lista = (format_config is not None and format_config.name == "ListA")

    if _is_lista:
        # Pick the phase matrix then scale every outcome by the pitch run factor.
        # We do NOT apply game_mode_override here — ListA uses its own phase boosts
        # instead of T20 game-mode multipliers.
        _base_matrix = _get_lista_matrix(over_number, format_config)
        _run_factor  = LISTA_RUN_FACTORS.get(pitch, 1.0)
        # Boundary and Wicket rows are pitch-modulated differently from run rows.
        # Scale run outcomes by run_factor; keep Wicket/Extras at original proportions.
        _RUN_OUTCOMES = {"Dot", "Single", "Double", "Three", "Four", "Six"}
        pitch_matrix = {}
        for _k, _v in _base_matrix.items():
            if _k in _RUN_OUTCOMES:
                pitch_matrix[_k] = _v * _run_factor
            else:
                pitch_matrix[_k] = _v
        # Renormalise so weights still sum to 1.0
        _pm_total = sum(pitch_matrix.values())
        if _pm_total > 0:
            pitch_matrix = {k: v / _pm_total for k, v in pitch_matrix.items()}
        logger.debug("[ListA] phase=%s pitch=%s run_factor=%.2f",
                     format_config.get_phase(over_number).name, pitch, _run_factor)
    else:
        # Existing T20 / legacy path
        pitch_matrix = (_gc_scoring_matrix(pitch, mode_override=game_mode_override, config=_gc)
                        or PITCH_SCORING_MATRIX.get(pitch, DEFAULT_SCORING_MATRIX))

    raw_weights = {}
    for outcome in pitch_matrix:
        base = pitch_matrix[outcome]
        # print(f"\n-- Computing weight for outcome: {outcome} (Base: {base}) --")

        if outcome == "Extras" and not allow_extras:
            raw_weights[outcome] = 0.0
            continue

        # Compute base weight via 60/40 blending
        # --- Bowling matchup modifier (computed once, applied to wickets + boundaries) ---
        matchup_boost = 1.0

        # 1. Spin turning away from bat — classic cricket advantage
        if bowling_type in ("Off spin", "Finger spin") and batting_hand == "Left":
            matchup_boost *= 1.15  # Turning away from left-hander
        if bowling_type in ("Leg spin", "Wrist spin") and batting_hand == "Right":
            matchup_boost *= 1.15  # Turning away from right-hander

        # 2. Pace vs tail-enders — raw pace terrifies lower order
        if bowling_type in ("Fast", "Fast-medium", "Medium-fast") and batting < 30:
            matchup_boost *= 1.25

        # 3. Left-arm pace angle vs right-handers (all pitches)
        if (bowling_hand == "Left" and batting_hand == "Right"
                and bowling_type in ("Fast", "Fast-medium", "Medium-fast")):
            matchup_boost *= 1.10
            if pitch == "Green":
                matchup_boost *= 1.08  # Extra seam movement on Green

        # 4. Spin vs lower-order on turning tracks
        if (bowling_type in ("Off spin", "Leg spin", "Finger spin", "Wrist spin")
                and pitch == "Dry" and batting < 50):
            matchup_boost *= 1.10

        # Boundary suppression when bowler has matchup advantage
        boundary_suppression = 1.0
        if matchup_boost > 1.0:
            boundary_suppression = 1.0 / (matchup_boost ** 0.5)  # Mild inverse

        if outcome in ("Dot", "Single", "Double", "Three", "Four", "Six"):
            weight = compute_weighted_prob(
                outcome, base,
                batting, bowling, fielding,
                pitch, bowling_type, streak, batter_runs, balls_faced,
                format_name=(format_config.name if format_config is not None else None),
                config=_gc,
            )
            # Apply boundary suppression when bowler has favorable matchup
            if outcome in ("Four", "Six") and boundary_suppression < 1.0:
                weight *= boundary_suppression

        elif outcome == "Wicket":
            weight = compute_weighted_prob(
                outcome, base,
                batting, bowling, fielding,
                pitch, bowling_type, streak, batter_runs, balls_faced,
                format_name=(format_config.name if format_config is not None else None),
                config=_gc,
            ) * matchup_boost
        else:  # "Extras"
            weight = compute_weighted_prob(
                outcome, base,
                batting, bowling, fielding,
                pitch, bowling_type, streak,
                format_name=(format_config.name if format_config is not None else None),
                config=_gc,
            )

        # --- T20 phase boosts (skipped for ListA — handled via _apply_lista_phase_boosts) ---
        if not _is_lista:
            # Load configurable phase boosts with hardcoded fallbacks
            _phase = _gc_phase_boosts(config=_gc) or {}
            _pp_cfg = _phase.get("powerplay", {})
            _death_cfg = _phase.get("death_overs", {})
            _inn2_cfg = _phase.get("second_innings_death", {})

            # Powerplay boosts
            pp_start = _pp_cfg.get("overs_start", 0)
            pp_end = _pp_cfg.get("overs_end", 5)
            # Death-over boosts (last 4 overs: 17-20)
            death_start = _death_cfg.get("overs_start", 16)
            death_end = _death_cfg.get("overs_end", 19)
            # The Hundred (100 balls mapped onto ~17 six-ball overs) carries its
            # own powerplay (first 25 balls) and death window in the FormatConfig;
            # honour those instead of the T20 over ranges. Strictly gated to the
            # Hundred so T20 / custom-over /sim / ListA paths are unchanged.
            if format_config is not None and format_config.name == "The100":
                if format_config.powerplay_phases:
                    pp_start = format_config.powerplay_phases[0].start
                    pp_end = format_config.powerplay_phases[-1].end
                if format_config.death_phase is not None:
                    death_start = format_config.death_phase.start
                    death_end = format_config.death_phase.end

            if pp_start <= over_number <= pp_end:
                if outcome in ("Four", "Six"):
                    pp_boost = _pp_cfg.get("boundary_multiplier", 1.25)
                    logger.debug(f"  [Powerplay] Boosting {outcome} by {pp_boost}x")
                    weight *= pp_boost

            in_death = death_start <= over_number <= death_end

            if in_death:
                if outcome in ("Four", "Six"):
                    if pitch in ("Flat", "Dead", "Hard"):
                        boundary_boost = _death_cfg.get("boundary_boost_batting_pitch", 2.2)
                    else:  # Green or Dry
                        boundary_boost = _death_cfg.get("boundary_boost_bowling_pitch", 1.8)
                    logger.debug(f"  DeathOver: BOUNDARY ({outcome}) on {pitch} by factor {boundary_boost}")
                    weight *= boundary_boost

                if outcome == "Wicket":
                    wicket_boost = _death_cfg.get("wicket_boost", 1.6)
                    logger.debug(f"  DeathOver: WICKET on {pitch} by factor {wicket_boost}")
                    weight *= wicket_boost

            # Second innings death-over boosts (mild — chasing advantage already helps)
            if innings == 2 and in_death:
                if outcome in ("Single", "Double", "Three", "Four", "Six"):
                    scoring_boost = _inn2_cfg.get("scoring_boost", 1.05)
                    weight *= scoring_boost

                if outcome == "Wicket":
                    wicket_boost_2nd = _inn2_cfg.get("wicket_boost", 1.15)
                    weight *= wicket_boost_2nd

        # Ensure no negative weights
        weight = max(weight, 0.0)
        raw_weights[outcome] = weight
        # print(f"  FinalRawWeight[{outcome}]: {weight:.6f}")
    
    # 3.25) Apply phase boosts and pitch deterioration.
    # ListA uses its own phase boost table and progressive wear model.
    # T20 uses the existing _apply_pitch_wear (unchanged).
    # All wear layers run BEFORE GSME so the momentum engine sees adjusted weights.
    if _is_lista:
        # ListA phase boosts (PP1 / Middle / Death boundary/wicket modifiers)
        raw_weights = _apply_lista_phase_boosts(raw_weights, over_number, pitch,
                                                innings, format_config)
        # ListA progressive pitch wear (more pronounced on Dry/Hard over 50 overs)
        if pitch_wear > 0.0:
            raw_weights = _apply_lista_pitch_wear(raw_weights, pitch, pitch_wear)
            logger.debug("[ListA PitchWear=%.3f] Applied ListA wear model.", pitch_wear)
        # Dew factor for Day/Night matches (2nd innings evening)
        raw_weights = _apply_dew_factor(raw_weights, innings, over_number,
                                        is_day_night, format_config)
        # Pitch-specific fine-tuning after wear and dew layers.
        if pitch == "Hard":
            # Keep Hard as scoring-friendly but avoid excessive wicket suppression.
            raw_weights["Single"] = raw_weights.get("Single", 0.0) * 1.03
            raw_weights["Four"] = raw_weights.get("Four", 0.0) * 1.03
            raw_weights["Six"] = raw_weights.get("Six", 0.0) * 1.03
        elif pitch == "Flat":
            raw_weights["Wicket"] = raw_weights.get("Wicket", 0.0) * 0.85
            raw_weights["Dot"] = raw_weights.get("Dot", 0.0) * 0.94
            raw_weights["Four"] = raw_weights.get("Four", 0.0) * 1.06
            raw_weights["Six"] = raw_weights.get("Six", 0.0) * 1.10
        elif pitch == "Dead":
            # Batting festival: reinforce low-dot, high-boundary profile.
            # run_factor (1.18) and wicket_mult (0.58) do the heavy lifting;
            # this nudge removes residual dot excess from the phase matrices.
            raw_weights["Dot"] = raw_weights.get("Dot", 0.0) * 0.88
            raw_weights["Four"] = raw_weights.get("Four", 0.0) * 1.08
            raw_weights["Six"] = raw_weights.get("Six", 0.0) * 1.12

        # Pitch-specific ListA rotation profile.
        pitch_dot_single = LISTA_DOT_SINGLE_FACTORS.get(pitch)
        if pitch_dot_single:
            raw_weights["Dot"] = raw_weights.get("Dot", 0.0) * pitch_dot_single["Dot"]
            raw_weights["Single"] = raw_weights.get("Single", 0.0) * pitch_dot_single["Single"]

        raw_weights["Wicket"] = raw_weights.get("Wicket", 0.0) * LISTA_WICKET_PITCH_MULT.get(pitch, 1.0)
    else:
        # T20 / legacy path — existing pitch wear model unchanged
        if pitch_wear > 0.0:
            raw_weights = _apply_pitch_wear(raw_weights, pitch, pitch_wear)
            logger.debug("[PitchWear=%.3f] Applied T20 pitch deterioration.", pitch_wear)
        # Realism recalibration: measured wicket rate at rating parity was
        # ~3 per 20 overs (real T20 sits near 6). Lift the wicket weight so
        # innings feel like a contest instead of a 220/3 batting parade; the
        # drop-catch mechanic gives some of these lives straight back.
        raw_weights["Wicket"] = raw_weights.get("Wicket", 0.0) * T20_WICKET_REALISM_MULT
        for _k, _m in T20_DUEL_RECAL.items():
            if _k in raw_weights:
                raw_weights[_k] *= _m

    # 3.4) Rating-based performance gating — ListA only.
    # The T20 / legacy path uses the multiplicative skill model in
    # compute_weighted_prob, which already sharpens raw ratings continuously
    # across the whole range, so this threshold-based gate would double-count
    # there. ListA keeps the additive blend and still needs it.
    if _is_lista:
        raw_weights = _apply_rating_performance_modifiers(
            raw_weights,
            batter["batting_rating"],
            bowler["bowling_rating"],
        )

    # 3.45) Execution duel — this ball's explicit rating contest (T20 path).
    # The bowler tries to execute, the batter tries to read; the winner of
    # the exchange reshapes this delivery's weights (engine.rating_duel).
    # State odds are pure functions of the two ratings — a big rating edge
    # wins far more exchanges, which is where extreme dominance comes from.
    duel_state = None
    if not _is_lista:
        try:
            from engine.rating_duel import resolve_duel, approach_risk_class
            duel_state, _dm = resolve_duel(
                batting, bowling, approach_risk_class(batting_approach))
            _duel_keymap = {"Dot": "dot", "Single": "one", "Double": "two",
                            "Three": "three", "Four": "four", "Six": "six",
                            "Wicket": "wicket", "Extras": "extras"}
            for _wk, _mk in _duel_keymap.items():
                if _wk in raw_weights:
                    raw_weights[_wk] *= _dm[_mk]
        except Exception:
            logger.exception("rating duel failed; ignoring this ball")

    # 3.5) Apply Game State Momentum Engine (GSME) adjustments.
    # This layer accounts for ball history (last 18 deliveries), run-rate
    # pressure, resources remaining, and collapse risk — BEFORE the
    # pressure-engine and scenario-engine modifiers are applied.
    if game_state is not None:
        raw_weights = apply_game_state_to_probs(raw_weights, game_state)
        logger.debug("[GSME] Applied game-state multipliers to raw_weights.")

    # 3.6) Calculate total weight
    total_weight = sum(raw_weights.values())

    # 3.7) Apply pressure effects if provided
    if pressure_effects:
        logger.debug(f"  [PRESSURE] Applying pressure effects: {pressure_effects}")
        
        # Increase dot ball probability
        if "Dot" in raw_weights:
            original_dot = raw_weights["Dot"]
            dot_bonus = pressure_effects.get('dot_bonus', 0.0)
            raw_weights["Dot"] += dot_bonus * total_weight
            logger.debug(f"  [PRESSURE] Dot: {original_dot:.6f} -> {raw_weights['Dot']:.6f}")
        
        # Modify boundary probabilities
        boundary_modifier = pressure_effects.get('boundary_modifier', 1.0)
        for boundary_type in ["Four", "Six"]:
            if boundary_type in raw_weights:
                original_boundary = raw_weights[boundary_type]
                raw_weights[boundary_type] *= boundary_modifier
                logger.debug(f"  [PRESSURE] {boundary_type}: {original_boundary:.6f} -> {raw_weights[boundary_type]:.6f}")
        
        # Modify wicket probability
        if "Wicket" in raw_weights:
            original_wicket = raw_weights["Wicket"]
            raw_weights["Wicket"] *= pressure_effects.get('wicket_modifier', 1.0)
            logger.debug(f"  [PRESSURE] Wicket: {original_wicket:.6f} -> {raw_weights['Wicket']:.6f}")
        
        # 🔧 NEW: Handle singles (boost or penalty with floor)
        if "Single" in raw_weights:
            original_single = raw_weights["Single"]
            
            # Apply single boost (defensive mode)
            if 'single_boost' in pressure_effects:
                raw_weights["Single"] *= pressure_effects['single_boost']
                logger.debug(f"  [PRESSURE] Single BOOST: {original_single:.6f} -> {raw_weights['Single']:.6f}")
            
            # Apply single penalty with floor (aggressive mode)
            elif 'strike_rotation_penalty' in pressure_effects:
                penalty = pressure_effects['strike_rotation_penalty']
                single_floor = pressure_effects.get('single_floor', 0.0)
                
                # Apply penalty but enforce minimum floor
                new_single_weight = original_single * (1 - penalty)
                floor_weight = single_floor * total_weight
                raw_weights["Single"] = max(new_single_weight, floor_weight)

                logger.debug(f"  [PRESSURE] Single PENALTY: {original_single:.6f} -> {raw_weights['Single']:.6f} (floor: {floor_weight:.6f})")
        
        # Reduce strike rotation for threes
        if "Three" in raw_weights:
            strike_rotation_penalty = pressure_effects.get('strike_rotation_penalty', 0.0)
            if strike_rotation_penalty > 0:
                original_three = raw_weights["Three"]
                raw_weights["Three"] *= (1 - strike_rotation_penalty)
                logger.debug(f"  [PRESSURE] Three: {original_three:.6f} -> {raw_weights['Three']:.6f}")
        
        # Recalculate total weight after pressure modifications
        total_weight = sum(raw_weights.values())
    
    # 4) Free hit: slight boundary boost (+10%) for both Four and Six, and the
    # wicket chance collapses to a sliver — the only dismissal a free hit allows
    # is a run out (forced below in calculate_outcome's wicket branch).
    if free_hit:
        if "Four" in raw_weights and "Six" in raw_weights:
            raw_weights["Four"] *= FREE_HIT_BOUNDARY_BOOST
            raw_weights["Six"] *= FREE_HIT_BOUNDARY_BOOST
        if "Wicket" in raw_weights:
            raw_weights["Wicket"] *= 0.10  # only run-outs remain possible
        total_weight = sum(raw_weights.values())

    # 4b) Approach modifiers (Challenge League over-by-over mode). No-op when
    # both approaches are None/Balanced, so existing callers are unaffected.
    # ``approach_context`` (engine.approach_modifiers.make_context) switches on
    # the situational layers — pitch/phase/mind-game/momentum/fatigue/chemistry;
    # without one only the three base approach layers apply.
    if batting_approach is not None or bowling_approach is not None:
        from engine.approach_modifiers import apply_approach_modifiers
        raw_weights = apply_approach_modifiers(
            raw_weights, batting_approach, bowling_approach,
            bowler_rating=bowling, context=approach_context)
        total_weight = sum(raw_weights.values())

    # 4c) Generic weight hook — used by /letsplay to apply player TRAITS to the
    # final outcome weights right before sampling (so traits respect ratings,
    # pitch, momentum, pressure, scenario and approach layers). No-op when None,
    # so Challenge League / every other caller is unaffected. Any failure is
    # swallowed so a trait bug can never crash a delivery.
    if weight_hook is not None:
        try:
            hooked = weight_hook(raw_weights)
            if hooked:
                raw_weights = hooked
                total_weight = sum(raw_weights.values())
        except Exception:
            logger.exception("ball_outcome weight_hook failed; ignoring this ball")

    # 4d) Wicket safety valve (T20 path). The multiplicative layers (duel x
    # GSME collapse x pressure x realism lift) can stack the wicket weight
    # into instant-cascade territory in long auto-sims that lack /cipl's
    # anti-cluster hook. Real T20 per-ball dismissal probability never
    # sustains past ~12-13%; cap the NORMALIZED wicket share at 12% so
    # collapses stay possible but a side can no longer fold inside five overs
    # at rating parity. Capping the share (not the raw weight) means solving
    # w/(w+other) = share for the max wicket weight, i.e. share/(1-share)*other.
    if not _is_lista and not free_hit:
        _share = 0.12
        _w = raw_weights.get("Wicket", 0.0)
        _other = max(0.0, total_weight - _w)
        _wcap = (_share / (1.0 - _share)) * _other
        if _other > 0.0 and _w > _wcap:
            raw_weights["Wicket"] = _wcap
            total_weight = sum(raw_weights.values())

    # 5) Normalize weights into probabilities
    # print(f"\n[calculate_outcome] Total raw weight sum: {total_weight:.6f}")
    if total_weight <= 0:
        # Fallback in pathological case
        chosen = "Dot"
        # print("[calculate_outcome] Warning: Total weight <= 0, defaulting to Dot ball")
    else:
        normalized_weights = [raw_weights[o] / total_weight for o in raw_weights]
        # logger.debug(f"[calculate_outcome] Normalized weights:")
        for o, nw in zip(raw_weights.keys(), normalized_weights):
            logger.debug(f"  {o}: {nw:.4f}")
        chosen = random.choices(list(raw_weights.keys()), weights=normalized_weights)[0]

    # print(f"[calculate_outcome] Chosen outcome: {chosen}")

    # 5) Build and return the result dictionary
    result = {
        "type": None,
        "runs": 0,
        "description": "",
        "wicket_type": None,
        "is_extra": False,
        "batter_out": False
    }
    if duel_state:
        result["duel"] = duel_state

    if chosen == "Wicket":
        result["type"] = "wicket"
        result["runs"] = 0
        result["batter_out"] = True

        # Decide wicket type based on bowling style (A7: varies by bowling type, A6: includes Stumped)
        types, weights_pct = _get_wicket_type_by_bowling(bowling_type)
        if free_hit:
            # On a free hit (the ball after a no-ball) the only legal dismissal
            # is a run out — bowled/caught/LBW/stumped do not count.
            wicket_choice = "Run Out"
        else:
            wicket_choice = random.choices(types, weights=weights_pct)[0]

        result["wicket_type"] = wicket_choice

        # A1: Run Out happens after completing 1 run (out attempting the 2nd)
        if wicket_choice == "Run Out":
            result["runs"] = 1

        # FIELDING: Catch-drop check for Caught and Stumped dismissals.
        # High fielding team rarely drops; poor fielding team drops more often.
        # fielding=90 → ~3% drop  |  fielding=60 → ~10% drop  |  fielding=30 → ~19% drop
        if wicket_choice in ("Caught", "Stumped") and fielding_quality is not None:
            drop_prob = max(0.02, 0.22 - (fielding_quality / 100.0) * 0.19)
            if random.random() < drop_prob:
                # Dropped! Convert wicket into runs
                result["batter_out"] = False
                result["wicket_type"] = None
                result["type"] = "run"
                result["runs"] = random.choices([1, 2, 4], weights=[35, 35, 30])[0]
                result["dropped_catch"] = True
                result["description"] = "DROPPED! The chance goes begging — a costly miss in the field!"
                logger.debug("[Fielding] Catch dropped (quality=%.1f, drop_prob=%.3f)", fielding_quality, drop_prob)
                return result

        # Use guaranteed wicket commentary templates
        wicket_descriptions = [
        "He's out! Brilliant delivery!",
        "Gone! A crucial wicket falls!",
        "What a fantastic catch to dismiss him!",
        "Wicket! Excellent bowling!",
        "Out! Clean bowled!",
        "Caught! Brilliant fielding!",
        "LBW! Plumb in front!",
        "Stumped! Lightning quick!",
        "Run out! Direct hit!",
        "Caught behind! Great catch!",
        "Bowled! Perfect delivery!",
        "Out! Magnificent catch!",
        "Wicket falls! Great bowling!",
        "Dismissed! Excellent work!",
        "Gone! Spectacular catch!",
        "Wicket! Superb delivery!",
        "Caught! Brilliant take!",
        "Bowled middle stump!",
        "Out! Perfect line and length!",
        "Gone! Perfect execution!"
    ]

        # Use commentary template for Wicket
        template = random.choice(wicket_descriptions)
        result["description"] = template

        # print(f"[calculate_outcome] WICKET! Type: {wicket_choice}, Description: {template}")

    elif chosen == "Extras":
        result["type"] = "extra"
        result["is_extra"] = True

        # A4: Weighted extra type selection — format-aware distribution.
        # ListA: more wides (slower pace/spin in 30-over middle overs bowl
        #        wider lines; spinner drifts are common); fewer no-balls
        #        (less aggressive short-ball pace attack than T20).
        # T20:   higher no-ball rate from aggressive pace bowling.
        if _is_lista:
            extra_types   = ["Wide", "No Ball", "Leg Bye", "Byes"]
            extra_weights = [0.52,   0.13,      0.22,      0.13]
        else:
            extra_types   = ["Wide", "No Ball", "Leg Bye", "Byes"]
            extra_weights = [0.40,   0.25,      0.20,      0.15]
        extra_choice  = random.choices(extra_types, weights=extra_weights)[0]

        # A4: Variable runs per extra type
        if extra_choice == "Wide":
            result["runs"] = 1
        elif extra_choice == "No Ball":
            result["runs"] = 1
        elif extra_choice == "Leg Bye":
            result["runs"] = random.choices([1, 2], weights=[0.80, 0.20])[0]
        elif extra_choice == "Byes":
            result["runs"] = random.choices([1, 2, 4], weights=[0.85, 0.10, 0.05])[0]

        result["extra_type"] = extra_choice
        template = random.choice(commentary_templates["Extras"])
        result["description"] = f"{template} ({extra_choice})"

    else:
        # It must be one of Dot, Single, Double, Three, Four, Six
        runs_map = {
            "Dot":    0,
            "Single": 1,
            "Double": 2,
            "Three":  3,
            "Four":   4,
            "Six":    6
        }
        result["type"] = "run"
        result["runs"] = runs_map[chosen]
        result["batter_out"] = False

        # Use commentary template for run outcomes
        template = random.choice(commentary_templates[chosen])
        result["description"] = f"{template}"

        # FIELDING: Misfield mechanic — poor fielding teams give away extra runs.
        # Only on dot balls and singles (not boundaries or multiple-run shots).
        # fielding=90 → ~1.5%  |  fielding=60 → ~5%  |  fielding=30 → ~10%
        if result["runs"] in (0, 1) and fielding_quality is not None:
            misfield_prob = max(0.01, 0.115 - (fielding_quality / 100.0) * 0.105)
            if random.random() < misfield_prob:
                result["runs"] += 1
                result["misfield"] = True
                result["description"] += " — misfield, they steal an extra!"
                logger.debug("[Fielding] Misfield! extra run granted (quality=%.1f, prob=%.3f)", fielding_quality, misfield_prob)

        # logger.debug(f"[calculate_outcome] RUN! Outcome: {chosen}, Runs: {result['runs']}, Description: {template}")

    logger.debug("=======================================================\n")
    return result

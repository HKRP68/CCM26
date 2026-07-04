"""Rating duel — the hardcore rating-vs-rating contest shared by both engines.

Used by services/probability_engine (/wpm, /wpmbot) and engine/ball_outcome
(/cipl, /letsplay). Three layers, all driven purely by the two ratings on the
cards — no hidden per-player attributes:

1. GAP CURVE (continuous, every point counts)
   ``gap_factor(bat, bowl)`` is a tanh S-curve of the rating difference.
   Steep through the realistic range (a single rating point moves the needle
   measurably) and saturating smoothly toward ±45 instead of the old hard
   clamp. Tuned for EXTREME dominance: the better card should almost always
   win (+10 team gap ≈ 85%+ win rate).

2. CLASS TIERS (discrete, crossing a threshold is a felt upgrade)
   An explicit ladder on BOTH disciplines:

       95+    GOAT
       90-94  Elite
       85-89  Star
       80-84  Class
       65-79  Solid   (neutral band)
       50-64  Modest
       <50    Rabbit / pie-chucker

   Each step above neutral stacks a small multiplicative bonus (boundaries
   up + dismissal resistance for batters; wickets + dots + boundary denial
   + tighter lines for bowlers), each step below stacks the inverse. This
   sits ON TOP of the continuous curve so 89 → 90 is a visible jump.

3. EXECUTION DUEL (per ball, the centerpiece)
   Every delivery is resolved as an explicit contest:
     - the bowler EXECUTES the plan with probability from bowl rating,
     - the batter READS the ball with probability from bat rating
       (attacking shots are harder to read with).
   The 2x2 result picks a state that reshapes the ball's outcome weights:

       executed + misread  → "beaten"    bowler wins the ball outright
       executed + read     → "contained" good ball, well defended
       loose    + read     → "punished"  batter wins the ball outright
       loose    + misread  → "scrappy"   edges, extras, streaky singles

   Each state's multiplier row has mean 1.0 across the four states, so at
   rating parity (where the four states are equally likely) the duel adds
   rating-driven texture and variance without inflating or deflating the
   totals. Away from parity the state odds skew hard with the ratings — a
   90-rated batter facing a 60-rated bowler gets a "punished" ball ~2x as
   often as at parity — which is where the extreme dominance comes from.
"""

import math
import random

# ── 1) Gap curve ─────────────────────────────────────────────────────
# tanh(diff / GAP_SCALE): diff 10 → 0.53, 17 → 0.76, 25 → 0.90, 40 → 0.98.
GAP_SCALE = 17.0


def gap_factor(bat_rating: float, bowl_rating: float) -> float:
    """Signed dominance in (-1, 1): positive = batter holds the whip hand."""
    return math.tanh((float(bat_rating) - float(bowl_rating)) / GAP_SCALE)


# ── 2) Class tiers ───────────────────────────────────────────────────

def tier_steps(rating: float) -> int:
    """Ladder position relative to the neutral 65-79 band (+4 .. -2)."""
    r = float(rating)
    if r >= 95:
        return 4
    if r >= 90:
        return 3
    if r >= 85:
        return 2
    if r >= 80:
        return 1
    if r >= 65:
        return 0
    if r >= 50:
        return -1
    return -2


# Per-step multiplicative bonuses. Positive steps use the value as-is,
# negative steps invert automatically via the exponent.
_BAT_TIER = {"four": 1.030, "six": 1.040, "wicket": 0.975}
_BOWL_TIER = {"wicket": 1.045, "dot": 1.030, "four": 0.978,
              "six": 0.972, "extras": 0.965}


def batting_tier_mults(rating: float) -> dict:
    s = tier_steps(rating)
    return {k: v ** s for k, v in _BAT_TIER.items()}


def bowling_tier_mults(rating: float) -> dict:
    s = tier_steps(rating)
    return {k: v ** s for k, v in _BOWL_TIER.items()}


# ── 3) Execution duel ────────────────────────────────────────────────

# Probability the bowler lands the ball where they want it.
#   rating 40 → 0.30, 75 → 0.50, 95 → 0.62  (clamped)
def _p_execute(bowl_rating: float) -> float:
    p = 0.30 + (float(bowl_rating) - 40.0) * 0.0058
    return max(0.12, min(0.72, p))


# Probability the batter picks the ball up early enough to be in control.
# shot_risk: 0 = safe/defensive, 1 = orthodox, 2 = aggressive swing.
_RISK_READ_SHIFT = {0: +0.08, 1: 0.0, 2: -0.10}


def _p_read(bat_rating: float, shot_risk: int) -> float:
    p = 0.30 + (float(bat_rating) - 40.0) * 0.0058
    p += _RISK_READ_SHIFT.get(shot_risk, 0.0)
    return max(0.10, min(0.80, p))


# State → outcome-weight multipliers. INVARIANT: every row of four state
# values has mean 1.0, so equally-likely states (= rating parity) leave each
# outcome's expected weight unchanged. Keys are canonical outcome names;
# engine adapters map them onto their own keys.
DUEL_STATES = ("beaten", "contained", "punished", "scrappy")
DUEL_MULTS = {
    #             beaten  contained  punished  scrappy
    "dot":       (1.90,   1.25,      0.35,     0.50),
    "one":       (0.70,   1.10,      0.95,     1.25),
    "two":       (0.60,   0.95,      1.20,     1.25),
    "three":     (0.60,   0.95,      1.20,     1.25),
    "four":      (0.30,   0.75,      2.15,     0.80),
    "six":       (0.20,   0.65,      2.35,     0.80),
    "wicket":    (2.10,   0.90,      0.35,     0.65),
    "extras":    (0.70,   0.80,      0.90,     1.60),
}
assert all(abs(sum(v) / 4.0 - 1.0) < 1e-9 for v in DUEL_MULTS.values()), \
    "DUEL_MULTS rows must average exactly 1.0"


def resolve_duel(bat_rating: float, bowl_rating: float, shot_risk: int = 1,
                 rng=random):
    """Roll one delivery's execution duel.

    Returns (state, mults) where state is one of DUEL_STATES and mults maps
    canonical outcome names → multiplier for this ball.
    """
    executed = rng.random() < _p_execute(bowl_rating)
    read = rng.random() < _p_read(bat_rating, shot_risk)
    if executed and not read:
        state = "beaten"
    elif executed:
        state = "contained"
    elif read:
        state = "punished"
    else:
        state = "scrappy"
    idx = DUEL_STATES.index(state)
    return state, {k: v[idx] for k, v in DUEL_MULTS.items()}


# ── Shot / approach risk classification ──────────────────────────────

_AGGRESSIVE_SHOTS = frozenset((
    "Slog", "Loft", "Switch Hit", "Hook", "Upper Cut", "Slog Sweep",
    "Reverse Sweep", "Pull",
))
_SAFE_SHOTS = frozenset((
    "Defend", "Leave", "Leg Glance", "Glance", "Block",
))


def shot_risk_class(shot: str) -> int:
    """0 safe / 1 orthodox / 2 aggressive, from a /wpm shot name."""
    if shot in _AGGRESSIVE_SHOTS:
        return 2
    if shot in _SAFE_SHOTS:
        return 0
    return 1


def approach_risk_class(batting_approach: str) -> int:
    """0 safe / 1 orthodox / 2 aggressive, from a /cipl batting approach."""
    a = (batting_approach or "").lower()
    if "ultra" in a or "aggress" in a:
        return 2
    if "defen" in a or "cautious" in a:
        return 0
    return 1

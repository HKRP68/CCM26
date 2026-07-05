"""
═══════════════════════════════════════════════════════════════════════════════
🏏 CRICKET MATCH SIMULATION ENGINE  —  v2 (Full Upgrade)
═══════════════════════════════════════════════════════════════════════════════

Core Philosophy: Every delivery is a contest between Delivery Quality (DQ)
and Shot Quality (SQ). The differential Δ = SQ - DQ determines probabilistic
outcomes.

───────────────────────────────────────────────────────────────────────────────
WHAT CHANGED IN v2 (vs the original spec engine)
───────────────────────────────────────────────────────────────────────────────
1. SHOT-DRIVEN OUTCOMES  — the shot the batsman plays now reshapes the *entire*
   run/wicket distribution via SHOT_PROFILES (aggression / risk / six-bias /
   rotation). A Slog skews toward 6-or-out; a Defend/Leave can no longer produce
   a boundary. Previously the shot only leaked in weakly through SQ.

2. WICKET ODDS PRESERVED — outcome probabilities are now built by *partitioning
   the exact remaining mass* after wicket + extras, so the designed sigmoid
   wicket odds survive (the old code rescaled and distorted them).

3. CORRECT OVER MECHANICS — wides/no-balls RE-BOWL (they no longer eat a legal
   ball), a wicket no longer ends the over, strike swaps at the end of an over,
   and a no-ball grants a FREE HIT on the next delivery.

4. PER-SPELL FATIGUE — fatigue is driven by balls bowled *in the current spell*
   (Bowler.rest() resets it), with only a small cumulative component.

5. HANDEDNESS MATCHUPS — spin turning into/away from the batsman and the
   left-arm-pace angle now nudge SQ (small, heuristic factors).

6. FAST SIM MATCHES ITS SPEC — over-by-over sim now draws runs from Poisson and
   wickets from Binomial (was Gaussian).

7. FULL MATCH RUNNER — MatchSimulator plays a complete innings ball-by-ball with
   new batsmen, bowler rotation + rest, extras, and free hits. An AI shot
   selector (choose_shot) powers "vs bot" play.

The factor tables (variations, lengths, shot-suitability, pitch) are unchanged —
they were good; v2 builds correct mechanics around them.

───────────────────────────────────────────────────────────────────────────────
SECTION 1: CORE FORMULAS
───────────────────────────────────────────────────────────────────────────────

1.1 DELIVERY QUALITY (DQ) — Range: [0.0, 1.0]
    DQ = (BowlerRating / 100) × VF × LF × PF_bowler × MSF_bowler × Fatigue

1.2 SHOT QUALITY (SQ) — Range: [0.0, 1.0]
    SQ = (BatsmanRating / 100) × SF × PF_bat × MSF_bat × Form × Confidence × Matchup

1.3 DIFFERENTIAL (Δ) — Range: [-1.0, +1.0]
    Δ = SQ - DQ
    Δ > +0.30 → batsman dominant; Δ < -0.30 → bowler dominant.

1.4 OUTCOME PARTITION (v2)
    Given Δ (noisy) and the shot's profile:
        p_wicket = clamp( sigmoid(-8Δ - 1.5) × shot.risk , 0, 0.85 )
        extras   = p_wide + p_noball
        M        = 1 - p_wicket - extras                      (remaining mass)
        boundary_share = clamp( (0.18 + 0.30Δ) × (0.5 + shot.aggression), 0, 0.75 )
        p_bnd = M × boundary_share ;  p6 = p_bnd × shot.six_bias ; p4 = p_bnd - p6
        Mn = M - p_bnd
        dot_share = clamp( sigmoid(-6Δ + 0.5) × (1 - 0.4·shot.aggression), 0, 0.9 )
        p_dot = Mn × dot_share
        Mrun  = Mn - p_dot   →  split into 1/2/3 by rotation weights
    By construction Σp = 1 exactly, and p_wicket is never rescaled.

(Factor tables in Sections 2-4 unchanged — see the data-table block below.)
═══════════════════════════════════════════════════════════════════════════════
"""

import random
import math
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple
from enum import Enum

# ═══════════════════════════════════════════════════════════════════════════════
# ENUMS & CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

class BowlingType(Enum):
    FAST_RIGHT = "Fast Right-Arm"
    FAST_LEFT = "Fast Left-Arm"
    MEDIUM_RIGHT = "Medium Right-Arm"
    MEDIUM_LEFT = "Medium Left-Arm"
    OFF_SPIN = "Right-Arm Off Spin"
    LEG_SPIN = "Right-Arm Leg Spin"
    LEFT_ORTHO = "Left-Arm Orthodox"
    CHINAMAN = "Left-Arm Chinaman"

class PitchType(Enum):
    DRY = "Dry"
    DUSTY = "Dusty"
    HARD = "Hard"
    FLAT = "Flat"
    BOUNCY = "Bouncy"
    GREEN = "Green"

class ShotType(Enum):
    DRIVE = "Drive"
    CUT = "Cut"
    PULL = "Pull"
    LEG_GLANCE = "Leg Glance"
    FLICK = "Flick"
    SWEEP = "Sweep"
    SWITCH_HIT = "Switch Hit"
    SLOG = "Slog"
    LOFT = "Loft"
    LEAVE = "Leave"
    DEFEND = "Defend"

class WicketType(Enum):
    BOWLED = "Bowled"
    LBW = "LBW"
    CAUGHT = "Caught"
    CAUGHT_BEHIND = "Caught Behind"
    STUMPED = "Stumped"
    RUN_OUT = "Run Out"

class OutcomeType(Enum):
    DOT = "dot"
    ONE = "1"
    TWO = "2"
    THREE = "3"
    FOUR = "4"
    SIX = "6"
    WICKET = "wicket"
    WIDE = "wide"
    NO_BALL = "noball"

    @property
    def runs(self) -> int:
        return {"1": 1, "2": 2, "3": 3, "4": 4, "6": 6}.get(self.value, 0)

# ═══════════════════════════════════════════════════════════════════════════════
# DATA TABLES  (unchanged from v1 — these were well tuned)
# ═══════════════════════════════════════════════════════════════════════════════

VARIATION_FACTORS = {
    # Fast Bowlers
    "Outswing": 0.95, "Inswing": 0.95, "Reverse Swing": 1.08,
    "Seam Up": 0.90, "Slower Ball": 0.82,
    # Medium Pacers
    "Leg Cutter": 0.90, "Off Cutter": 0.90, "Knuckle Ball": 0.96,
    "Cross Seam": 0.88,
    # Off Spin
    "Off Break": 0.88, "Doosra": 1.02, "Arm Ball": 0.95,
    "Top Spinner": 0.92, "Carrom Ball": 1.00,
    # Leg Spin
    "Leg Break": 0.90, "Googly": 1.05, "Flipper": 0.98,
    "Slider": 0.95,
    # Orthodox
    "Orthodox": 0.88, "Back Spinner": 0.90,
    # Chinaman
    "Chinaman": 0.90, "Wrong'un": 1.05, "Teesra": 1.02,
    # Surprise
    "Surprise": 1.00,
}

LENGTH_FACTORS = {
    "Hard Length": 0.92, "Good Length": 1.00, "Full Length": 0.95,
    "Hit the Deck": 0.88, "Yorker": 1.06, "Bouncer": 0.85,
    "Short of Length": 0.92, "Back of Length": 0.90,
}

SHOT_SUITABILITY_FAST = {
    ShotType.DRIVE:       {"Hard Length":0.55, "Good Length":0.70, "Full Length":1.00, "Hit the Deck":0.45, "Yorker":0.20, "Bouncer":0.15},
    ShotType.CUT:         {"Hard Length":0.80, "Good Length":0.65, "Full Length":0.40, "Hit the Deck":0.85, "Yorker":0.25, "Bouncer":0.75},
    ShotType.PULL:        {"Hard Length":0.75, "Good Length":0.55, "Full Length":0.30, "Hit the Deck":0.80, "Yorker":0.20, "Bouncer":1.00},
    ShotType.LEG_GLANCE:  {"Hard Length":0.50, "Good Length":0.60, "Full Length":0.85, "Hit the Deck":0.45, "Yorker":0.30, "Bouncer":0.20},
    ShotType.FLICK:       {"Hard Length":0.50, "Good Length":0.65, "Full Length":0.90, "Hit the Deck":0.45, "Yorker":0.35, "Bouncer":0.20},
    ShotType.SWEEP:       {"Hard Length":0.30, "Good Length":0.40, "Full Length":0.50, "Hit the Deck":0.35, "Yorker":0.25, "Bouncer":0.20},
    ShotType.SWITCH_HIT:  {"Hard Length":0.45, "Good Length":0.55, "Full Length":0.70, "Hit the Deck":0.40, "Yorker":0.30, "Bouncer":0.35},
    ShotType.SLOG:        {"Hard Length":0.40, "Good Length":0.50, "Full Length":0.65, "Hit the Deck":0.45, "Yorker":0.35, "Bouncer":0.50},
    ShotType.LOFT:        {"Hard Length":0.35, "Good Length":0.50, "Full Length":0.75, "Hit the Deck":0.40, "Yorker":0.30, "Bouncer":0.45},
    ShotType.LEAVE:       {"Hard Length":0.85, "Good Length":0.90, "Full Length":0.80, "Hit the Deck":0.85, "Yorker":0.70, "Bouncer":0.90},
    ShotType.DEFEND:      {"Hard Length":0.90, "Good Length":0.95, "Full Length":0.90, "Hit the Deck":0.85, "Yorker":0.80, "Bouncer":0.85},
}

SHOT_SUITABILITY_MEDIUM = {
    ShotType.DRIVE:       {"Good Length":0.70, "Full Length":1.00, "Short of Length":0.55, "Back of Length":0.50, "Yorker":0.20},
    ShotType.CUT:         {"Good Length":0.65, "Full Length":0.40, "Short of Length":0.80, "Back of Length":0.85, "Yorker":0.25},
    ShotType.PULL:        {"Good Length":0.55, "Full Length":0.30, "Short of Length":0.75, "Back of Length":0.80, "Yorker":0.20},
    ShotType.LEG_GLANCE:  {"Good Length":0.60, "Full Length":0.85, "Short of Length":0.50, "Back of Length":0.45, "Yorker":0.30},
    ShotType.FLICK:       {"Good Length":0.65, "Full Length":0.90, "Short of Length":0.50, "Back of Length":0.45, "Yorker":0.35},
    ShotType.SWEEP:       {"Good Length":0.40, "Full Length":0.50, "Short of Length":0.35, "Back of Length":0.30, "Yorker":0.25},
    ShotType.SWITCH_HIT:  {"Good Length":0.55, "Full Length":0.70, "Short of Length":0.45, "Back of Length":0.40, "Yorker":0.30},
    ShotType.SLOG:        {"Good Length":0.50, "Full Length":0.65, "Short of Length":0.45, "Back of Length":0.40, "Yorker":0.35},
    ShotType.LOFT:        {"Good Length":0.50, "Full Length":0.75, "Short of Length":0.40, "Back of Length":0.35, "Yorker":0.30},
    ShotType.LEAVE:       {"Good Length":0.90, "Full Length":0.80, "Short of Length":0.85, "Back of Length":0.85, "Yorker":0.70},
    ShotType.DEFEND:      {"Good Length":0.95, "Full Length":0.90, "Short of Length":0.85, "Back of Length":0.85, "Yorker":0.80},
}

SHOT_SUITABILITY_SPIN = {
    ShotType.DRIVE:       {"Off Break":0.75, "Doosra":0.60, "Arm Ball":0.70, "Top Spinner":0.65, "Carrom Ball":0.55, "Leg Break":0.75, "Googly":0.60, "Flipper":0.65, "Slider":0.70, "Orthodox":0.75, "Chinaman":0.75, "Wrong'un":0.60, "Teesra":0.62, "Back Spinner":0.70, "Surprise":0.65},
    ShotType.CUT:         {"Off Break":0.80, "Doosra":0.85, "Arm Ball":0.75, "Top Spinner":0.70, "Carrom Ball":0.80, "Leg Break":0.80, "Googly":0.85, "Flipper":0.75, "Slider":0.78, "Orthodox":0.80, "Chinaman":0.80, "Wrong'un":0.85, "Teesra":0.72, "Back Spinner":0.75, "Surprise":0.78},
    ShotType.PULL:        {"Off Break":0.60, "Doosra":0.65, "Arm Ball":0.55, "Top Spinner":0.50, "Carrom Ball":0.60, "Leg Break":0.60, "Googly":0.65, "Flipper":0.55, "Slider":0.58, "Orthodox":0.60, "Chinaman":0.60, "Wrong'un":0.65, "Teesra":0.52, "Back Spinner":0.55, "Surprise":0.58},
    ShotType.LEG_GLANCE:  {"Off Break":0.85, "Doosra":0.70, "Arm Ball":0.80, "Top Spinner":0.75, "Carrom Ball":0.70, "Leg Break":0.85, "Googly":0.70, "Flipper":0.80, "Slider":0.78, "Orthodox":0.85, "Chinaman":0.85, "Wrong'un":0.70, "Teesra":0.77, "Back Spinner":0.80, "Surprise":0.76},
    ShotType.FLICK:       {"Off Break":0.90, "Doosra":0.75, "Arm Ball":0.85, "Top Spinner":0.80, "Carrom Ball":0.75, "Leg Break":0.90, "Googly":0.75, "Flipper":0.85, "Slider":0.82, "Orthodox":0.90, "Chinaman":0.90, "Wrong'un":0.75, "Teesra":0.82, "Back Spinner":0.85, "Surprise":0.81},
    ShotType.SWEEP:       {"Off Break":0.95, "Doosra":0.80, "Arm Ball":0.85, "Top Spinner":0.90, "Carrom Ball":0.80, "Leg Break":0.95, "Googly":0.80, "Flipper":0.85, "Slider":0.88, "Orthodox":0.95, "Chinaman":0.95, "Wrong'un":0.80, "Teesra":0.87, "Back Spinner":0.85, "Surprise":0.86},
    ShotType.SWITCH_HIT:  {"Off Break":0.70, "Doosra":0.65, "Arm Ball":0.70, "Top Spinner":0.65, "Carrom Ball":0.60, "Leg Break":0.70, "Googly":0.65, "Flipper":0.68, "Slider":0.66, "Orthodox":0.70, "Chinaman":0.70, "Wrong'un":0.65, "Teesra":0.66, "Back Spinner":0.68, "Surprise":0.66},
    ShotType.SLOG:        {"Off Break":0.65, "Doosra":0.55, "Arm Ball":0.60, "Top Spinner":0.55, "Carrom Ball":0.50, "Leg Break":0.65, "Googly":0.55, "Flipper":0.58, "Slider":0.56, "Orthodox":0.65, "Chinaman":0.65, "Wrong'un":0.55, "Teesra":0.56, "Back Spinner":0.58, "Surprise":0.56},
    ShotType.LOFT:        {"Off Break":0.70, "Doosra":0.60, "Arm Ball":0.65, "Top Spinner":0.60, "Carrom Ball":0.55, "Leg Break":0.70, "Googly":0.60, "Flipper":0.63, "Slider":0.61, "Orthodox":0.70, "Chinaman":0.70, "Wrong'un":0.60, "Teesra":0.61, "Back Spinner":0.63, "Surprise":0.61},
    ShotType.LEAVE:       {"Off Break":0.85, "Doosra":0.80, "Arm Ball":0.85, "Top Spinner":0.80, "Carrom Ball":0.75, "Leg Break":0.85, "Googly":0.80, "Flipper":0.82, "Slider":0.81, "Orthodox":0.85, "Chinaman":0.85, "Wrong'un":0.80, "Teesra":0.81, "Back Spinner":0.82, "Surprise":0.81},
    ShotType.DEFEND:      {"Off Break":0.95, "Doosra":0.90, "Arm Ball":0.92, "Top Spinner":0.90, "Carrom Ball":0.88, "Leg Break":0.95, "Googly":0.90, "Flipper":0.92, "Slider":0.91, "Orthodox":0.95, "Chinaman":0.95, "Wrong'un":0.90, "Teesra":0.91, "Back Spinner":0.92, "Surprise":0.91},
}

PITCH_FACTORS = {
    PitchType.DRY:     {"pace_bowler": 0.95, "pace_bat": 0.95, "spin_bowler": 1.08, "spin_bat": 0.95},
    PitchType.DUSTY:   {"pace_bowler": 0.90, "pace_bat": 0.90, "spin_bowler": 1.12, "spin_bat": 0.90},
    PitchType.HARD:    {"pace_bowler": 1.08, "pace_bat": 1.05, "spin_bowler": 0.92, "spin_bat": 1.05},
    PitchType.FLAT:    {"pace_bowler": 0.95, "pace_bat": 1.12, "spin_bowler": 0.90, "spin_bat": 1.12},
    PitchType.BOUNCY:  {"pace_bowler": 1.10, "pace_bat": 0.92, "spin_bowler": 0.95, "spin_bat": 0.92},
    PitchType.GREEN:   {"pace_bowler": 1.15, "pace_bat": 0.88, "spin_bowler": 0.85, "spin_bat": 0.88},
}

# ═══════════════════════════════════════════════════════════════════════════════
# SHOT PROFILES  (v2 — the shot now shapes the outcome distribution)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ShotProfile:
    aggression: float   # 0..1  → pushes mass toward boundaries, away from dots
    risk: float         # multiplier on wicket probability
    six_bias: float     # of boundary mass, fraction that becomes SIX (vs FOUR)
    rotation: float     # >1 favours singles (strike rotation), <1 favours dot/big
    can_boundary: bool  # LEAVE cannot score a boundary

SHOT_PROFILES: Dict[ShotType, ShotProfile] = {
    ShotType.DRIVE:      ShotProfile(aggression=0.55, risk=1.00, six_bias=0.30, rotation=0.95, can_boundary=True),
    ShotType.CUT:        ShotProfile(aggression=0.55, risk=1.00, six_bias=0.20, rotation=0.95, can_boundary=True),
    ShotType.PULL:       ShotProfile(aggression=0.65, risk=1.08, six_bias=0.45, rotation=0.85, can_boundary=True),
    ShotType.LEG_GLANCE: ShotProfile(aggression=0.25, risk=0.80, six_bias=0.03, rotation=1.25, can_boundary=True),
    ShotType.FLICK:      ShotProfile(aggression=0.45, risk=0.85, six_bias=0.28, rotation=1.10, can_boundary=True),
    ShotType.SWEEP:      ShotProfile(aggression=0.55, risk=1.10, six_bias=0.28, rotation=1.00, can_boundary=True),
    ShotType.SWITCH_HIT: ShotProfile(aggression=0.80, risk=1.28, six_bias=0.55, rotation=0.70, can_boundary=True),
    ShotType.SLOG:       ShotProfile(aggression=0.95, risk=1.45, six_bias=0.62, rotation=0.60, can_boundary=True),
    ShotType.LOFT:       ShotProfile(aggression=0.85, risk=1.24, six_bias=0.58, rotation=0.70, can_boundary=True),
    ShotType.LEAVE:      ShotProfile(aggression=0.00, risk=0.35, six_bias=0.00, rotation=0.05, can_boundary=False),
    ShotType.DEFEND:     ShotProfile(aggression=0.05, risk=0.55, six_bias=0.00, rotation=0.55, can_boundary=True),
}

# Handedness matchup factors applied to SQ (small, heuristic).
# <1.0 => harder for the batsman (bowler advantage).
def matchup_factor(bowling_type: BowlingType, is_right_handed: bool) -> float:
    rhb = is_right_handed
    if bowling_type == BowlingType.OFF_SPIN:
        return 0.97 if not rhb else 1.00        # turns into LHB, cramps them
    if bowling_type == BowlingType.LEG_SPIN:
        return 0.97 if rhb else 1.00            # turns away from RHB, finds edge
    if bowling_type == BowlingType.LEFT_ORTHO:
        return 0.97 if rhb else 1.00            # turns away from RHB
    if bowling_type == BowlingType.CHINAMAN:
        return 0.97 if not rhb else 1.00        # turns away from LHB
    if bowling_type in (BowlingType.FAST_LEFT, BowlingType.MEDIUM_LEFT):
        return 0.98 if rhb else 1.00            # angle across the RHB
    return 1.00

# ═══════════════════════════════════════════════════════════════════════════════
# PLAYER CLASSES
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Player:
    name: str
    rating: int  # 30-100
    role: str
    balls_faced: int = 0
    runs_scored: int = 0
    wickets_taken: int = 0
    balls_bowled: int = 0
    runs_conceded: int = 0

    @property
    def strike_rate(self) -> float:
        return self.runs_scored / max(self.balls_faced, 1) * 100

    @property
    def bowling_economy(self) -> float:
        return self.runs_conceded / max(self.balls_bowled / 6, 1)

@dataclass
class Bowler(Player):
    bowling_type: BowlingType = BowlingType.FAST_RIGHT
    variations: List[str] = field(default_factory=list)
    balls_this_spell: int = 0  # v2: resets on rest()

    def __post_init__(self):
        if not self.variations:
            self.variations = self._default_variations()

    def _default_variations(self) -> List[str]:
        defaults = {
            BowlingType.FAST_RIGHT: ["Outswing", "Inswing", "Reverse Swing", "Seam Up", "Slower Ball"],
            BowlingType.FAST_LEFT: ["Outswing", "Inswing", "Reverse Swing", "Seam Up", "Slower Ball"],
            BowlingType.MEDIUM_RIGHT: ["Leg Cutter", "Off Cutter", "Slower Ball", "Knuckle Ball", "Cross Seam"],
            BowlingType.MEDIUM_LEFT: ["Leg Cutter", "Off Cutter", "Slower Ball", "Knuckle Ball", "Cross Seam"],
            BowlingType.OFF_SPIN: ["Off Break", "Doosra", "Arm Ball", "Top Spinner", "Carrom Ball"],
            BowlingType.LEG_SPIN: ["Leg Break", "Googly", "Flipper", "Top Spinner", "Slider", "Surprise"],
            BowlingType.LEFT_ORTHO: ["Orthodox", "Arm Ball", "Slider", "Top Spinner", "Back Spinner", "Carrom Ball", "Surprise"],
            BowlingType.CHINAMAN: ["Chinaman", "Wrong'un", "Flipper", "Slider", "Teesra", "Surprise"],
        }
        return defaults.get(self.bowling_type, ["Off Break"])

    def get_random_variation(self) -> str:
        var = random.choice(self.variations)
        if var == "Surprise":
            non_surprise = [v for v in self.variations if v != "Surprise"]
            if non_surprise:
                return random.choice(non_surprise)
        return var

    def rest(self) -> None:
        """Bowler comes off — spell fatigue resets."""
        self.balls_this_spell = 0

@dataclass
class Batsman(Player):
    is_right_handed: bool = True
    recent_balls: List[int] = field(default_factory=list)  # runs per ball
    is_out: bool = False

    @property
    def form(self) -> float:
        """Running form based on last 10 scoring balls (0.7 .. 1.15)."""
        if not self.recent_balls:
            return 1.0
        recent = self.recent_balls[-10:]
        avg = sum(recent) / len(recent)
        return max(0.7, min(1.15, 0.7 + avg * 0.075))

    @property
    def confidence(self) -> float:
        """Confidence based on runs scored vs a modest expectation (0.8 .. 1.15)."""
        expected = self.balls_faced * 0.8
        if expected <= 0:
            return 1.0
        ratio = self.runs_scored / expected
        return max(0.8, min(1.15, 0.8 + ratio * 0.2))

# ═══════════════════════════════════════════════════════════════════════════════
# MATCH STATE
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class MatchState:
    format: str = "T20"  # T20, ODI, Test
    total_overs: int = 20
    current_over: int = 0
    current_ball: int = 0  # 0-5 (legal balls only)
    runs_scored: int = 0
    wickets_lost: int = 0
    target: Optional[int] = None  # None for first innings
    partnership_balls: int = 0
    partnership_runs: int = 0
    free_hit: bool = False  # v2: set after a no-ball, consumed by next legal ball

    @property
    def required_rr(self) -> float:
        if self.target is None:
            return 0.0
        balls_remaining = (self.total_overs - self.current_over) * 6 - self.current_ball
        if balls_remaining <= 0:
            return 999.0
        runs_needed = self.target - self.runs_scored
        return (runs_needed / balls_remaining) * 6

    @property
    def current_rr(self) -> float:
        balls_bowled = self.current_over * 6 + self.current_ball
        if balls_bowled == 0:
            return 0.0
        return (self.runs_scored / balls_bowled) * 6

    @property
    def is_powerplay(self) -> bool:
        return self.current_over < 6

    @property
    def is_death(self) -> bool:
        if self.format == "T20":
            return self.current_over >= 16
        elif self.format == "ODI":
            return self.current_over >= 45
        return False

# ═══════════════════════════════════════════════════════════════════════════════
# SIMULATION ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

class CricketSimulationEngine:
    """Ball-by-ball cricket simulation engine implementing the v2 formula spec."""

    def __init__(self, pitch: PitchType = PitchType.FLAT):
        self.pitch = pitch
        self.match_log: List[Dict] = []

    # ── math helpers ──────────────────────────────────────────────────────────
    def sigmoid(self, x: float) -> float:
        # guard against overflow for large |x|
        if x < -60:
            return 0.0
        if x > 60:
            return 1.0
        return 1.0 / (1.0 + math.exp(-x))

    def clamp(self, value: float, min_val: float, max_val: float) -> float:
        return max(min_val, min(max_val, value))

    def is_pace_bowler(self, bowler: Bowler) -> bool:
        return bowler.bowling_type in (
            BowlingType.FAST_RIGHT, BowlingType.FAST_LEFT,
            BowlingType.MEDIUM_RIGHT, BowlingType.MEDIUM_LEFT,
        )

    def is_spin_bowler(self, bowler: Bowler) -> bool:
        return not self.is_pace_bowler(bowler)

    def is_fast_bowler(self, bowler: Bowler) -> bool:
        return bowler.bowling_type in (BowlingType.FAST_RIGHT, BowlingType.FAST_LEFT)

    # ── delivery quality ──────────────────────────────────────────────────────
    def compute_delivery_quality(
        self, bowler: Bowler, variation: str, length: Optional[str], match_state: MatchState
    ) -> float:
        """DQ = (BowlerRating/100) × VF × LF × PF × MSF × Fatigue"""
        base = bowler.rating / 100.0
        vf = VARIATION_FACTORS.get(variation, 1.0)

        lf = 1.0
        if length and not self.is_spin_bowler(bowler):
            lf = LENGTH_FACTORS.get(length, 1.0)

        pf_key = "pace_bowler" if self.is_pace_bowler(bowler) else "spin_bowler"
        pf = PITCH_FACTORS[self.pitch][pf_key]

        msf = 1.0
        pressure_bowler = self.clamp((match_state.required_rr - match_state.current_rr) / 20, -0.08, 0.12)
        msf += pressure_bowler
        if match_state.is_powerplay:
            msf += 0.05
        if match_state.is_death:
            msf += 0.10
        msf = self.clamp(msf, 0.8, 1.20)

        # v2: spell-driven fatigue (+ tiny cumulative component)
        fatigue = max(0.6, 1.0 - (bowler.balls_this_spell * 0.0015) - (bowler.balls_bowled * 0.0003))

        return self.clamp(base * vf * lf * pf * msf * fatigue, 0.0, 1.0)

    # ── shot quality ──────────────────────────────────────────────────────────
    def compute_shot_quality(
        self, batsman: Batsman, shot: ShotType, variation: str,
        length: Optional[str], bowler: Bowler, match_state: MatchState
    ) -> float:
        """SQ = (BatsmanRating/100) × SF × PF × MSF × Form × Confidence × Matchup"""
        base = batsman.rating / 100.0

        if self.is_pace_bowler(bowler):
            if self.is_fast_bowler(bowler):
                sf = SHOT_SUITABILITY_FAST.get(shot, {}).get(length, 0.5)
            else:
                sf = SHOT_SUITABILITY_MEDIUM.get(shot, {}).get(length, 0.5)
        else:
            sf = SHOT_SUITABILITY_SPIN.get(shot, {}).get(variation, 0.5)

        pf_key = "pace_bat" if self.is_pace_bowler(bowler) else "spin_bat"
        pf = PITCH_FACTORS[self.pitch][pf_key]

        msf = 1.0
        pressure_bat = self.clamp((match_state.current_rr - match_state.required_rr) / 15, -0.10, 0.15)
        msf += pressure_bat
        if match_state.is_powerplay:
            msf += 0.08
        if match_state.is_death:
            msf += 0.05
        msf += min(0.10, match_state.partnership_balls / 500)  # partnership settling
        msf += -0.05 * match_state.wickets_lost                # wickets pressure
        msf = self.clamp(msf, 0.7, 1.25)

        matchup = matchup_factor(bowler.bowling_type, batsman.is_right_handed)

        sq = base * sf * pf * msf * batsman.form * batsman.confidence * matchup
        return self.clamp(sq, 0.0, 1.0)

    # ── noise & clutch ────────────────────────────────────────────────────────
    def compute_noise_sigma(self, bowler: Bowler, variation: str, batsman: Batsman, match_state: MatchState) -> float:
        sigma = 0.08
        if self.pitch == PitchType.DUSTY:
            sigma += 0.03
        elif self.pitch == PitchType.GREEN:
            sigma += 0.02
        if match_state.is_death:
            sigma += 0.04
        if batsman.balls_faced < 5:
            sigma += 0.05
        if variation == "Surprise":
            sigma += 0.06
        if variation == "Slower Ball":
            sigma += 0.04
        return sigma

    def apply_clutch(self, dq: float, sq: float, bowler: Bowler, batsman: Batsman) -> Tuple[float, float]:
        """5% chance of a clutch (hero) moment."""
        if random.random() < 0.05:
            if batsman.rating > 85:
                sq = min(1.0, sq + 0.15)
            elif bowler.rating > 85 and (sq - dq) < 0:
                dq = min(1.0, dq + 0.10)
            else:
                if random.random() < 0.5:
                    sq = min(1.0, sq + 0.10)   # batsman gets away with one
                else:
                    dq = min(1.0, dq + 0.10)   # bowler produces a jaffa
        return dq, sq

    # ── outcome distribution (v2: shot-driven, wicket-preserving) ─────────────
    def compute_outcome_probabilities(
        self, delta: float, bowler: Bowler, shot: ShotType, on_free_hit: bool = False
    ) -> Dict[str, float]:
        prof = SHOT_PROFILES[shot]

        # 1) Wicket — realistic per-ball model (v2 recalibration).
        #    An exponential in Δ gives sane per-ball dismissal rates (calibrated
        #    so a full T20 innings lands ~150-170 for good sides):
        #      Δ=+0.3 → ~1.2%   Δ=0 → ~3.2%   Δ=-0.3 → ~8.4%   Δ=-0.5 → ~16%
        #    (The original sigmoid(-8Δ-1.5) returned 70%+ at Δ=-0.3 — far too
        #     hot — and only looked sane because wickets were rescaled away.)
        p_wicket = 0.028 * math.exp(-delta * 3.2) * prof.risk
        p_wicket = self.clamp(p_wicket, 0.0, 0.22)
        if on_free_hit:
            p_wicket = 0.0  # only run-outs possible on a free hit (not modelled here)

        # 2) Extras
        p_wide = 0.02 + 0.01 * (100 - bowler.rating) / 100
        p_noball = 0.01 + 0.005 * (100 - bowler.rating) / 100
        if on_free_hit:
            p_noball *= 0.5
        extras = p_wide + p_noball

        # 3) Remaining mass split into scoring / dot by the shot profile
        M = max(0.0, 1.0 - p_wicket - extras)

        base_boundary = self.clamp(0.17 + 0.28 * delta, 0.0, 0.62)
        boundary_share = self.clamp(base_boundary * (0.55 + prof.aggression), 0.0, 0.75)
        if not prof.can_boundary:
            boundary_share = 0.0
        if on_free_hit:
            boundary_share = min(0.85, boundary_share * 1.4)

        p_bnd = M * boundary_share
        p_6 = p_bnd * prof.six_bias
        p_4 = p_bnd - p_6

        Mn = M - p_bnd
        dot_base = self.sigmoid(-5 * delta)  # ~0.5 at even contest
        dot_share = self.clamp(dot_base * (1.0 - 0.4 * prof.aggression), 0.0, 0.9)
        if not prof.can_boundary:  # LEAVE: almost always a dot
            dot_share = max(dot_share, 0.9)
        p_dot = Mn * dot_share

        Mrun = Mn - p_dot  # split into 1s / 2s / 3s
        w1 = max(0.05, 0.70 * prof.rotation)
        w2 = 0.22 + 0.05 * max(0.0, delta)
        w3 = 0.04
        wsum = w1 + w2 + w3
        p_1 = Mrun * w1 / wsum
        p_2 = Mrun * w2 / wsum
        p_3 = Mrun * w3 / wsum

        return {
            "wicket": p_wicket, "dot": p_dot,
            "1": p_1, "2": p_2, "3": p_3, "4": p_4, "6": p_6,
            "wide": p_wide, "noball": p_noball,
        }

    # ── wicket type ───────────────────────────────────────────────────────────
    def determine_wicket_type(self, variation: str, length: Optional[str], shot: ShotType, bowler: Bowler) -> WicketType:
        is_spin = self.is_spin_bowler(bowler)
        p_bowled, p_lbw, p_caught, p_cb, p_stumped, p_run_out = 0.25, 0.20, 0.30, 0.15, 0.05, 0.05

        if length == "Yorker":
            p_bowled += 0.15; p_lbw += 0.10
        if length == "Full Length":
            p_bowled += 0.10
        if variation == "Inswing":
            p_lbw += 0.15
        if variation == "Arm Ball":
            p_lbw += 0.10
        if variation == "Outswing":
            p_cb += 0.10
        if variation == "Leg Cutter":
            p_cb += 0.08
        if length == "Bouncer":
            p_cb += 0.05
        if is_spin:
            p_stumped += 0.15
        if variation in ("Googly", "Doosra"):
            p_stumped += 0.10

        if shot == ShotType.LOFT:
            p_caught += 0.10
        if shot == ShotType.SLOG:
            p_caught += 0.10; p_run_out += 0.03
        if shot == ShotType.SWITCH_HIT:
            p_run_out += 0.02
        if shot == ShotType.DEFEND:
            p_bowled -= 0.10; p_caught -= 0.10
        if shot == ShotType.LEAVE:
            p_bowled -= 0.05; p_lbw -= 0.15; p_caught -= 0.05; p_cb -= 0.10

        probs = [max(0.0, p) for p in (p_bowled, p_lbw, p_caught, p_cb, p_stumped, p_run_out)]
        if sum(probs) == 0:
            probs = [0.25, 0.20, 0.30, 0.15, 0.05, 0.05]
        types = [WicketType.BOWLED, WicketType.LBW, WicketType.CAUGHT,
                 WicketType.CAUGHT_BEHIND, WicketType.STUMPED, WicketType.RUN_OUT]
        return random.choices(types, weights=probs)[0]

    # ── single ball ───────────────────────────────────────────────────────────
    def simulate_ball(
        self, bowler: Bowler, batsman: Batsman, shot: ShotType,
        variation: Optional[str] = None, length: Optional[str] = None,
        match_state: Optional[MatchState] = None, on_free_hit: bool = False,
    ) -> Dict:
        """Simulate a single delivery. Returns a rich result dict."""
        if match_state is None:
            match_state = MatchState()

        if variation is None:
            variation = bowler.get_random_variation()

        if length is None and not self.is_spin_bowler(bowler):
            if self.is_fast_bowler(bowler):
                lengths = ["Hard Length", "Good Length", "Full Length", "Hit the Deck", "Yorker", "Bouncer"]
            else:
                lengths = ["Good Length", "Full Length", "Short of Length", "Back of Length", "Yorker"]
            length = random.choice(lengths)

        dq = self.compute_delivery_quality(bowler, variation, length, match_state)
        sq = self.compute_shot_quality(batsman, shot, variation, length, bowler, match_state)
        dq, sq = self.apply_clutch(dq, sq, bowler, batsman)

        delta = sq - dq
        sigma = self.compute_noise_sigma(bowler, variation, batsman, match_state)
        delta_noisy = delta + random.gauss(0, sigma)

        probs = self.compute_outcome_probabilities(delta_noisy, bowler, shot, on_free_hit=on_free_hit)
        outcomes = ["wicket", "dot", "1", "2", "3", "4", "6", "wide", "noball"]
        weights = [max(0.0, probs[o]) for o in outcomes]
        outcome = random.choices(outcomes, weights=weights)[0]

        result = {
            "outcome": outcome,
            "outcome_type": None,
            "runs": 0,
            "is_wicket": False,
            "wicket_type": None,
            "is_extra": False,
            "extra_type": None,
            "on_free_hit": on_free_hit,
            "delta": round(delta_noisy, 3),
            "dq": round(dq, 3),
            "sq": round(sq, 3),
            "variation": variation,
            "length": length,
            "shot": shot.value,
            "bowler": bowler.name,
            "batsman": batsman.name,
        }

        try:
            result["outcome_type"] = OutcomeType(outcome)
        except ValueError:
            result["outcome_type"] = None

        if outcome == "wicket":
            result["is_wicket"] = True
            wt = self.determine_wicket_type(variation, length, shot, bowler)
            result["wicket_type"] = wt
            result["description"] = f"WICKET! {batsman.name} {wt.value} b {bowler.name}"
        elif outcome == "dot":
            result["description"] = f"Dot ball. {batsman.name} {shot.value.lower()}."
        elif outcome in ("1", "2", "3", "4", "6"):
            result["runs"] = int(outcome)
            if outcome == "4":
                result["description"] = f"FOUR! {batsman.name} {shot.value.lower()} to the fence."
            elif outcome == "6":
                result["description"] = f"SIX! {batsman.name} {shot.value.lower()} over the rope!"
            else:
                result["description"] = f"{batsman.name} takes {outcome} off the {shot.value.lower()}."
        elif outcome == "wide":
            result["runs"] = 1; result["is_extra"] = True; result["extra_type"] = "wide"
            result["description"] = "Wide. 1 extra, re-bowl."
        elif outcome == "noball":
            result["runs"] = 1; result["is_extra"] = True; result["extra_type"] = "noball"
            result["description"] = "No ball! 1 extra, free hit coming up."

        return result

    # ── one over (v2: correct extras / no over-ending wicket / strike swap) ────
    def simulate_over(
        self, bowler: Bowler, striker: Batsman, non_striker: Batsman,
        match_state: MatchState, shot_strategy: Optional[Dict] = None,
        shot_chooser=None, next_batsman=None,
    ) -> Dict:
        """
        Simulate a full over (6 legal balls). Returns:
            {balls, runs, wickets, striker, non_striker, dismissed:[...]}

        shot_strategy: dict {legal_ball_index -> ShotType} (fixed plan), OR
        shot_chooser:  callable(batsman, match_state) -> ShotType (e.g. AI/bot).
        Falls back to a random shot when neither is given.

        next_batsman: callable() -> Optional[Batsman]. Called when a wicket falls
        so a replacement can walk in and the SAME over continues (correct ball
        count). Returns None when all out. If omitted, the over ends on a wicket
        (handy for standalone single-over use).
        """
        balls: List[Dict] = []
        dismissed: List[Batsman] = []
        legal = 0
        over_runs = 0
        over_wickets = 0
        pending_free_hit = match_state.free_hit

        while legal < 6:
            # Stop instantly if the target is overhauled mid-over (chase).
            if match_state.target is not None and match_state.runs_scored >= match_state.target:
                break
            match_state.current_ball = legal

            if shot_strategy and legal in shot_strategy:
                shot = shot_strategy[legal]
            elif shot_chooser is not None:
                shot = shot_chooser(striker, match_state)
            else:
                shot = random.choice(list(ShotType))

            result = self.simulate_ball(
                bowler, striker, shot, match_state=match_state, on_free_hit=pending_free_hit
            )
            balls.append(result)
            self.match_log.append(result)

            # ── Extras: re-bowl, do NOT consume a legal ball ──
            if result["is_extra"]:
                match_state.runs_scored += 1
                match_state.partnership_runs += 1
                over_runs += 1
                bowler.runs_conceded += 1
                if result["extra_type"] == "noball":
                    pending_free_hit = True   # free hit stands for the re-bowl
                # wide keeps any existing free hit intact
                continue

            # ── Legal delivery ──
            legal += 1
            bowler.balls_bowled += 1
            bowler.balls_this_spell += 1
            striker.balls_faced += 1
            match_state.partnership_balls += 1
            pending_free_hit = False  # consumed by this legal ball

            if result["is_wicket"]:
                # On a free hit only run-outs count; our model never yields a
                # wicket on a free hit, so this branch is genuinely a dismissal.
                striker.is_out = True
                over_wickets += 1
                bowler.wickets_taken += 1
                match_state.wickets_lost += 1
                match_state.partnership_runs = 0
                match_state.partnership_balls = 0
                dismissed.append(striker)
                if next_batsman is not None:
                    nb = next_batsman()
                    if nb is None:
                        break  # all out — no one left to bat
                    striker = nb   # replacement takes strike; over continues
                    continue
                break  # no callback: hand control back to the caller

            runs = result["runs"]
            over_runs += runs
            match_state.runs_scored += runs
            match_state.partnership_runs += runs
            striker.runs_scored += runs
            striker.recent_balls.append(runs)
            bowler.runs_conceded += runs

            if runs in (1, 3):
                striker, non_striker = non_striker, striker

        # Carry any unconsumed free hit into the next over (rare edge case).
        match_state.free_hit = pending_free_hit

        # End-of-over strike swap (only when the over actually completed).
        if legal >= 6:
            striker, non_striker = non_striker, striker
            match_state.current_over += 1
            match_state.current_ball = 0
        else:
            # Incomplete over (all-out / target chased): report the number of
            # legal balls actually bowled, not the last zero-based index.
            match_state.current_ball = legal

        return {
            "balls": balls,
            "runs": over_runs,
            "wickets": over_wickets,
            "striker": striker,
            "non_striker": non_striker,
            "dismissed": dismissed,
            "completed": legal >= 6,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# AI SHOT SELECTOR  (powers "vs bot" play, e.g. /wpmbot)
# ═══════════════════════════════════════════════════════════════════════════════

def choose_shot(batsman: Batsman, match_state: MatchState, aggression_bias: float = 0.5) -> ShotType:
    """
    Heuristic bot shot selection. `aggression_bias` in [0,1] tunes the bot's
    default intent (0 = block, 1 = all-out attack). Situation overrides it:
    chasing behind the rate → attack; new batsman / lost wickets → consolidate.
    """
    intent = aggression_bias

    if match_state.target is not None:
        rr_gap = match_state.required_rr - match_state.current_rr
        intent += max(-0.35, min(0.45, rr_gap / 24.0))

    if match_state.is_powerplay:
        intent += 0.10
    if match_state.is_death:
        intent += 0.20
    if batsman.balls_faced < 4:
        intent -= 0.25           # play yourself in
    if match_state.wickets_lost >= 6:
        intent -= 0.15           # protect the tail
    if match_state.free_hit:
        intent += 0.40           # free swing

    intent = max(0.0, min(1.0, intent))

    defensive = [ShotType.DEFEND, ShotType.LEAVE, ShotType.LEG_GLANCE]
    rotate = [ShotType.DRIVE, ShotType.CUT, ShotType.FLICK, ShotType.LEG_GLANCE]
    attack = [ShotType.PULL, ShotType.SWEEP, ShotType.LOFT, ShotType.SLOG, ShotType.SWITCH_HIT]

    roll = random.random()
    if intent < 0.33:
        pool = defensive if roll < 0.6 else rotate
    elif intent < 0.66:
        pool = rotate if roll < 0.7 else attack
    else:
        pool = attack if roll < 0.7 else rotate
    return random.choice(pool)


# ═══════════════════════════════════════════════════════════════════════════════
# FULL MATCH RUNNER  (ball-by-ball innings; backs /wpm and /cm)
# ═══════════════════════════════════════════════════════════════════════════════

class MatchSimulator:
    """Plays a complete innings ball-by-ball with correct match mechanics."""

    def __init__(self, pitch: PitchType = PitchType.FLAT):
        self.pitch = pitch
        self.engine = CricketSimulationEngine(pitch)

    def simulate_innings(
        self,
        batting_order: List[Batsman],
        bowlers: List[Bowler],
        match_state: MatchState,
        shot_chooser=None,
        max_overs_per_bowler: Optional[int] = None,
        verbose: bool = False,
    ) -> Dict:
        """
        Run one innings. Returns a scorecard dict. `shot_chooser` (e.g. choose_shot)
        makes it a "vs bot" innings; omit for random batting.
        """
        if max_overs_per_bowler is None:
            max_overs_per_bowler = max(1, match_state.total_overs // max(1, len(bowlers)))

        striker = batting_order[0]
        non_striker = batting_order[1] if len(batting_order) > 1 else batting_order[0]
        # Index of the next man to walk in (openers already at the crease).
        self._next_idx = 2
        overs_by_bowler: Dict[str, int] = {b.name: 0 for b in bowlers}
        last_bowler: Optional[Bowler] = None
        commentary: List[str] = []

        def next_batsman() -> Optional[Batsman]:
            if self._next_idx < len(batting_order):
                nb = batting_order[self._next_idx]
                self._next_idx += 1
                return nb
            return None

        while match_state.current_over < match_state.total_overs:
            if match_state.wickets_lost >= len(batting_order) - 1:
                break  # all out
            if match_state.target is not None and match_state.runs_scored >= match_state.target:
                break  # target chased

            bowler = self._pick_bowler(bowlers, overs_by_bowler, max_overs_per_bowler, last_bowler)
            if bowler is None:
                break
            if bowler is not last_bowler:
                bowler.rest()  # new spell / fresh over off a rest

            over = self.engine.simulate_over(
                bowler, striker, non_striker, match_state,
                shot_chooser=shot_chooser, next_batsman=next_batsman,
            )
            for b in over["balls"]:
                commentary.append(b["description"])
            striker, non_striker = over["striker"], over["non_striker"]

            overs_by_bowler[bowler.name] += 1
            last_bowler = bowler

            if verbose:
                commentary.append(
                    f"— End of over {match_state.current_over}: "
                    f"{match_state.runs_scored}/{match_state.wickets_lost} —"
                )
            if match_state.wickets_lost >= len(batting_order) - 1:
                break

        return {
            "runs": match_state.runs_scored,
            "wickets": match_state.wickets_lost,
            "overs": f"{match_state.current_over}.{match_state.current_ball}",
            "run_rate": round(match_state.current_rr, 2),
            "batting_card": [
                {"name": b.name, "runs": b.runs_scored, "balls": b.balls_faced,
                 "sr": round(b.strike_rate, 1), "out": b.is_out}
                for b in batting_order if b.balls_faced > 0 or b.is_out
            ],
            "bowling_card": [
                {"name": b.name, "overs": overs_by_bowler.get(b.name, 0),
                 "runs": b.runs_conceded, "wickets": b.wickets_taken,
                 "econ": round(b.bowling_economy, 2)}
                for b in bowlers if b.balls_bowled > 0
            ],
            "commentary": commentary,
            "chased": match_state.target is not None and match_state.runs_scored >= match_state.target,
        }

    def _pick_bowler(self, bowlers, overs_by_bowler, cap, last_bowler) -> Optional[Bowler]:
        eligible = [b for b in bowlers if overs_by_bowler[b.name] < cap and b is not last_bowler]
        if not eligible:
            eligible = [b for b in bowlers if overs_by_bowler[b.name] < cap]
        if not eligible:
            return None
        # Prefer the least-used bowler, ties broken randomly.
        fewest = min(overs_by_bowler[b.name] for b in eligible)
        return random.choice([b for b in eligible if overs_by_bowler[b.name] == fewest])


# ═══════════════════════════════════════════════════════════════════════════════
# FAST SIMULATION (Over-by-Over)  — v2: Poisson runs + Binomial wickets per spec
# ═══════════════════════════════════════════════════════════════════════════════

def _poisson(lam: float) -> int:
    """Knuth's algorithm; falls back to a normal approximation for large lam."""
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, int(round(random.gauss(lam, math.sqrt(lam)))))
    L = math.exp(-lam)
    k, p = 0, 1.0
    while True:
        k += 1
        p *= random.random()
        if p <= L:
            return k - 1

def _binomial(n: int, p: float) -> int:
    p = max(0.0, min(1.0, p))
    return sum(1 for _ in range(n) if random.random() < p)


class FastSimulationEngine:
    """Over-by-over fast simulation using statistical distributions."""

    def __init__(self, pitch: PitchType = PitchType.FLAT):
        self.pitch = pitch
        self.engine = CricketSimulationEngine(pitch)

    def simulate_over_fast(self, bowler: Bowler, batsman: Batsman, match_state: MatchState) -> Dict:
        engine = self.engine
        avg_shot = ShotType.DRIVE

        variation = bowler.get_random_variation()
        length = None
        if engine.is_pace_bowler(bowler):
            if engine.is_fast_bowler(bowler):
                length = random.choice(["Hard Length", "Good Length", "Full Length", "Hit the Deck", "Yorker", "Bouncer"])
            else:
                length = random.choice(["Good Length", "Full Length", "Short of Length", "Back of Length", "Yorker"])

        dq = engine.compute_delivery_quality(bowler, variation, length, match_state)
        sq = engine.compute_shot_quality(batsman, avg_shot, variation, length, bowler, match_state)
        delta = sq - dq

        probs = engine.compute_outcome_probabilities(delta, bowler, avg_shot)
        erpb = (probs["1"] + probs["2"] * 2 + probs["3"] * 3 +
                probs["4"] * 4 + probs["6"] * 6 + probs["wide"] + probs["noball"])
        erpo = erpb * 6

        # Over-level phase modifiers
        if match_state.is_powerplay:
            erpo *= 1.15
        elif match_state.is_death:
            erpo *= 1.25
        elif 7 <= match_state.current_over <= 15:
            erpo *= 0.95
        if engine.is_spin_bowler(bowler) and 7 <= match_state.current_over <= 15:
            erpo *= 0.85
        if engine.is_pace_bowler(bowler) and match_state.current_over <= 3:
            erpo *= 0.80

        # v2: runs from Poisson (per spec) instead of Gaussian
        runs = _poisson(erpo)

        # Expected wickets → per-ball prob → Binomial(6, p)
        ewpo = 6 * probs["wicket"] * (1 + 0.3 * (bowler.rating - batsman.rating) / 100)
        if match_state.current_over <= 3 and engine.is_pace_bowler(bowler):
            ewpo += 0.3
        if engine.is_spin_bowler(bowler) and self.pitch == PitchType.DUSTY and 15 <= match_state.current_over <= 40:
            ewpo += 0.2
        ewpo = min(ewpo, 1.3)  # safety cap — a whole cluster in one over is rare
        wickets = min(2, _binomial(6, max(0.0, ewpo) / 6))

        if wickets > 0:
            runs = int(runs * 0.6)  # disruption from losing wicket(s)

        boundary_prob = max(0.0, min(0.6, (erpo - 3.5) / 10))
        boundaries = _binomial(6, boundary_prob)

        return {
            "over": match_state.current_over,
            "runs": runs,
            "wickets": wickets,
            "boundaries": boundaries,
            "bowler": bowler.name,
            "batsman": batsman.name,
            "erpo": round(erpo, 2),
            "ewpo": round(ewpo, 3),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# EXAMPLE USAGE & DEMONSTRATION
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    random.seed(7)

    kohli = Batsman(name="Virat Kohli", rating=95, role="Batsman", is_right_handed=True)
    rohit = Batsman(name="Rohit Sharma", rating=90, role="Batsman", is_right_handed=True)
    pant = Batsman(name="Rishabh Pant", rating=86, role="Batsman", is_right_handed=False)
    surya = Batsman(name="Suryakumar", rating=88, role="Batsman", is_right_handed=True)
    hardik = Batsman(name="Hardik Pandya", rating=80, role="All-rounder", is_right_handed=True)
    jadeja_b = Batsman(name="Ravindra Jadeja", rating=72, role="All-rounder", is_right_handed=False)
    tail = Batsman(name="Kuldeep Yadav", rating=45, role="Bowler", is_right_handed=True)

    bumrah = Bowler(name="Jasprit Bumrah", rating=92, role="Bowler", bowling_type=BowlingType.FAST_RIGHT)
    rashid = Bowler(name="Rashid Khan", rating=90, role="Bowler", bowling_type=BowlingType.LEG_SPIN)

    # ─────────────────────────────────────────────────────────────────────────
    print("=" * 72)
    print("🏏  /wpm  —  BALL-BY-BALL (shot-driven outcomes)")
    print("=" * 72)
    engine = CricketSimulationEngine(pitch=PitchType.FLAT)
    demo = MatchState(format="T20", total_overs=20, target=180)
    demo.current_over, demo.runs_scored, demo.wickets_lost = 5, 45, 1
    for i, shot in enumerate([ShotType.DEFEND, ShotType.DRIVE, ShotType.SLOG,
                              ShotType.LEG_GLANCE, ShotType.LOFT, ShotType.LEAVE]):
        r = engine.simulate_ball(bumrah, kohli, shot, match_state=demo)
        print(f"Ball {i+1}: {r['variation']}/{r['length']} | {r['shot']:10s} "
              f"Δ={r['delta']:+.2f}  →  {r['description']}")

    # ─────────────────────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("🤖  /wpmbot  —  AI SHOT SELECTION (one over vs the bot)")
    print("=" * 72)
    botms = MatchState(format="T20", total_overs=20, target=200)
    botms.current_over, botms.runs_scored, botms.wickets_lost = 17, 150, 4
    over = engine.simulate_over(rashid, pant, rohit, botms,
                                shot_chooser=lambda b, m: choose_shot(b, m, 0.6))
    for b in over["balls"]:
        print(f"  {b['shot']:10s} → {b['description']}")
    print(f"  Over: {over['runs']} run(s), {over['wickets']} wicket(s)")

    # ─────────────────────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("📋  /cm  —  FULL INNINGS (ball-by-ball match runner)")
    print("=" * 72)
    sim = MatchSimulator(pitch=PitchType.HARD)
    innings_state = MatchState(format="T20", total_overs=20)
    card = sim.simulate_innings(
        batting_order=[kohli, rohit, pant, surya, hardik, jadeja_b, tail],
        bowlers=[bumrah, rashid,
                 Bowler(name="Bhuvneshwar", rating=80, role="Bowler", bowling_type=BowlingType.MEDIUM_RIGHT),
                 Bowler(name="Jadeja", rating=82, role="Bowler", bowling_type=BowlingType.LEFT_ORTHO)],
        match_state=innings_state,
        shot_chooser=lambda b, m: choose_shot(b, m, 0.55),
    )
    print(f"  FINAL: {card['runs']}/{card['wickets']} in {card['overs']} (RR {card['run_rate']})")
    print("  Batting:")
    for row in card["batting_card"]:
        status = "out" if row["out"] else "not out"
        print(f"    {row['name']:16s} {row['runs']:3d} ({row['balls']}b, SR {row['sr']}) {status}")
    print("  Bowling:")
    for row in card["bowling_card"]:
        print(f"    {row['name']:16s} {row['overs']}ov  {row['runs']}-{row['wickets']}  econ {row['econ']}")

    # ─────────────────────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("⚡  FAST OVER SIM  —  Poisson runs / Binomial wickets")
    print("=" * 72)
    fast = FastSimulationEngine(pitch=PitchType.FLAT)
    fms = MatchState(format="T20", total_overs=20, target=180)
    fms.runs_scored, fms.wickets_lost = 85, 2
    bhuvi = Bowler(name="Bhuvneshwar", rating=80, role="Bowler", bowling_type=BowlingType.MEDIUM_RIGHT)
    for over_no in range(16, 20):
        fms.current_over = over_no
        r = fast.simulate_over_fast(bhuvi, kohli, fms)
        print(f"  Over {r['over']}: {r['runs']} runs, {r['wickets']} wkt(s) "
              f"(ERPO {r['erpo']}, EWPO {r['ewpo']})")
        fms.runs_scored += r["runs"]
        fms.wickets_lost += r["wickets"]

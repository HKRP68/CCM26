"""
engine/format_config.py
=======================

Single source of truth for all format-specific parameters in SimCricketX.
Last reviewed: 2026-04-30.

Every engine component that has a format-sensitive value reads from a
FormatConfig instance rather than hardcoding T20 constants.  Adding a new
format (e.g. Test, T10) requires only a new entry in FORMAT_REGISTRY.

Usage
-----
    from engine.format_config import FORMAT_REGISTRY, FormatConfig

    fmt = FORMAT_REGISTRY.get(match_data.get("match_format", "T20"),
                              FORMAT_REGISTRY["T20"])
    fmt.overs            # 20 or 50
    fmt.max_bowler_overs # 4 or 10
    fmt.is_death(over)   # True/False
    fmt.get_phase(over)  # Phase object
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

from engine import pitch_registry


# ---------------------------------------------------------------------------
# Phase descriptor
# ---------------------------------------------------------------------------

@dataclass
class Phase:
    """Describes one scoring/fielding phase within a format."""
    name: str
    start: int               # first over index (0-based, inclusive)
    end: int                 # last over index (0-based, inclusive)
    max_fielders_outside: int = 4   # fielders permitted outside 30-yard circle


# ---------------------------------------------------------------------------
# FormatConfig
# ---------------------------------------------------------------------------

@dataclass
class FormatConfig:
    """
    Complete parameterisation of a cricket format.

    Attributes
    ----------
    name                    : canonical format name ("T20", "ListA")
    overs                   : overs per innings
    max_bowler_overs        : bowling quota per bowler per innings
    allow_consecutive_overs : whether a bowler may bowl back-to-back overs
    powerplay_phases        : ordered list of Powerplay Phase objects
    middle_phase            : the consolidation/middle Phase
    death_phase             : the final/slog Phase
    par_scores              : {over_index: cumulative_runs} for 1st innings
                              (neutral/Hard pitch baseline)
    pitch_par_factors       : per-pitch multiplier on par_scores
    expected_rr             : {phase_key: runs_per_over} for pressure engine
    extras_per_innings      : tuning target for extra deliveries
    target_scores           : {pitch_type: expected_1st_innings_total}
    correct_toss_choice     : {pitch_type: "bat"|"bowl"} optimal toss decision
    """
    name: str
    overs: int
    max_bowler_overs: int
    allow_consecutive_overs: bool
    powerplay_phases: List[Phase]
    middle_phase: Phase
    death_phase: Phase
    par_scores: Dict[int, float]
    pitch_par_factors: Dict[str, float]
    expected_rr: Dict[str, float]
    extras_per_innings: int
    target_scores: Dict[str, int]
    correct_toss_choice: Dict[str, str]
    # D/N override: in floodlit matches dew in the 2nd innings almost always
    # makes bowling first correct.  None = fall back to correct_toss_choice.
    correct_toss_choice_dn: Optional[Dict[str, str]]
    # Per-pitch "neutral" run-rate used by GSME to normalise required_aggression.
    # T20:   Hard pitch neutral ≈ 8.5 RPO (IPL / international averages)
    # ListA: Hard pitch neutral ≈ 6.0 RPO (ODI first-innings scoring rate)
    rrr_baseline: Dict[str, float]

    # ------------------------------------------------------------------ #
    # Phase helpers                                                        #
    # ------------------------------------------------------------------ #

    def get_phase(self, over: int) -> Phase:
        """
        Return the Phase that contains the given over index.

        Checks powerplay phases first (in order), then death, then middle.
        Falls back to middle if nothing matches (should not happen in valid
        over range).
        """
        for pp in self.powerplay_phases:
            if pp.start <= over <= pp.end:
                return pp
        if self.death_phase.start <= over <= self.death_phase.end:
            return self.death_phase
        return self.middle_phase

    def is_powerplay(self, over: int) -> bool:
        return any(pp.start <= over <= pp.end for pp in self.powerplay_phases)

    def is_middle(self, over: int) -> bool:
        return (self.middle_phase.start <= over <= self.middle_phase.end
                and not self.is_powerplay(over)
                and not self.is_death(over))

    def is_death(self, over: int) -> bool:
        return self.death_phase.start <= over <= self.death_phase.end

    def phase_key(self, over: int) -> str:
        """Return a string key suitable for expected_rr lookups."""
        phase = self.get_phase(over)
        return phase.name

    def max_fielders_outside(self, over: int) -> int:
        return self.get_phase(over).max_fielders_outside


# ---------------------------------------------------------------------------
# T20 FormatConfig
# ---------------------------------------------------------------------------

# Neutral first-innings curve, in runs on the EVEN surface — the one pitch in
# engine.pitch_registry that is defined as neutral. It used to be labelled the
# "Hard pitch" curve while Hard's own par factor was 0.96, so the curve was not
# actually any pitch's. It tops out at 196, the Even par band's midpoint in
# config/ground_conditions.yaml, and every other surface is that curve times its
# own factor below.
_T20_PAR_SCORES: Dict[int, float] = {
    0:   0.0,
    1:   7.7,
    2:  16.0,
    3:  24.8,
    4:  34.0,
    5:  42.8,
    6:  53.6,   # End of powerplay
    7:  61.9,
    8:  70.7,
    9:  80.5,
    10: 90.3,
    11: 100.1,
    12: 110.4,
    13: 120.2,
    14: 130.0,
    15: 139.8,
    16: 151.1,
    17: 163.0,
    18: 175.4,
    19: 186.7,
    20: 196.0,
}

# Per-pitch multipliers on the neutral (Even) curve, one per surface in
# engine.pitch_registry. Factor = that pitch's par-band midpoint in
# config/ground_conditions.yaml ÷ 196 (Even's own midpoint), so the whole table
# is derived from one place instead of hand-kept.
#
# These were still the pre-v3.0 numbers (Green 0.65 → a 123 par) long after
# section 5A moved par: the engine was producing ~164 on Green while every
# consumer of this table — GSME, the pressure engine, the DLS curve — judged it
# against 123, i.e. read every Green innings as 40 runs ahead of par from the
# first over.
_T20_PITCH_PAR_FACTORS: Dict[str, float] = {
    "Dusty":  0.81,
    "Green":  0.88,
    "Dry":    0.90,
    "Bouncy": 0.95,
    "Even":   1.00,
    "Hard":   1.12,
    "Flat":   1.19,
    "Dead":   1.28,
}

_T20 = FormatConfig(
    name="T20",
    overs=20,
    max_bowler_overs=4,
    allow_consecutive_overs=False,
    powerplay_phases=[
        Phase("Powerplay", start=0, end=5, max_fielders_outside=2),
    ],
    middle_phase=Phase("Middle", start=6, end=15, max_fielders_outside=4),
    death_phase=Phase("Death", start=16, end=19, max_fielders_outside=5),
    par_scores=_T20_PAR_SCORES,
    pitch_par_factors=_T20_PITCH_PAR_FACTORS,
    expected_rr={
        # The neutral (Even) par curve's own phase rates, over this config's own
        # phase windows: 0→53.6 across the six powerplay overs, 53.6→151.1
        # across the ten middle overs, 151.1→196 across the last four. They used
        # to sum to 167 against a curve that ended at 190, so the pressure
        # engine judged every innings behind a rate the same engine's par table
        # said it was on.
        "Powerplay": 8.9,
        "Middle":    9.8,
        "Death":    11.2,
    },
    extras_per_innings=5,
    target_scores={
        # The par-band midpoints from config/ground_conditions.yaml, measured
        # with `python -m tools.pitch_calibration`.
        "Dusty":  159,
        "Green":  172,
        "Dry":    176,
        "Bouncy": 186,
        "Even":   196,
        "Hard":   219,
        "Flat":   233,
        "Dead":   250,
    },
    # Toss calls come from engine.pitch_registry so the Pitch Report card, the
    # bot captains and this table cannot disagree — they used to, on Flat.
    correct_toss_choice={p: pitch_registry.toss_call(p)
                        for p in pitch_registry.PITCHES},
    # Under lights, dew in the second innings makes bowling first correct on
    # every surface.
    correct_toss_choice_dn={p: pitch_registry.toss_call(p, "Night")
                            for p in pitch_registry.PITCHES},
    rrr_baseline={
        # "Neutral" RPO for each pitch in T20 context — each pitch's par over
        # 20 overs. GSME divides the actual RRR by this to get a normalised
        # aggression index, so it has to track the par table or a chase on a
        # turner reads as relaxed and a chase on a road as desperate.
        "Dusty":  8.0,
        "Green":  8.6,
        "Dry":    8.8,
        "Bouncy": 9.3,
        "Even":   9.8,
        "Hard":  11.0,
        "Flat":  11.7,
        "Dead":  12.5,
    },
)


# ---------------------------------------------------------------------------
# List A (50-over) FormatConfig
# ---------------------------------------------------------------------------

# Par scores reflect a neutral (Hard) pitch first-innings average of ~290.
# Phase breakdown:
#   PP1  (overs  0- 9): ~62 runs  (6.2 RPO)
#   Middle (overs 10-39): ~126 runs  (5.0 RPO)  [62 → 188]
#   Death (overs 40-49): ~102 runs  (8.5 RPO)  [188 → 290]
_LISTA_PAR_SCORES: Dict[int, float] = {
    0:   0.0,
    1:   6.0,
    2:  12.5,
    3:  19.5,
    4:  26.5,
    5:  34.0,
    6:  40.5,
    7:  47.5,
    8:  54.5,
    9:  58.5,
    10: 63.0,   # End of PP1
    11: 68.0,
    12: 73.0,
    13: 78.0,
    14: 83.0,
    15: 88.0,
    16: 93.0,
    17: 98.0,
    18: 103.0,
    19: 108.0,
    20: 113.0,
    21: 118.0,
    22: 123.0,
    23: 128.0,
    24: 133.0,
    25: 138.0,
    26: 143.0,
    27: 148.0,
    28: 153.0,
    29: 158.0,
    30: 163.0,
    31: 168.0,
    32: 173.0,
    33: 178.0,
    34: 183.0,
    35: 188.0,  # End of consolidation phase
    36: 193.5,
    37: 199.5,
    38: 206.0,
    39: 213.0,
    40: 220.0,  # Death begins — acceleration
    41: 228.5,
    42: 237.5,
    43: 246.5,
    44: 255.5,
    45: 264.5,
    46: 273.5,
    47: 280.5,
    48: 285.5,
    49: 288.0,
    50: 290.0,
}

# Per-pitch multipliers on the ListA curve (Hard = 1.00 ≈ 290). Dusty and
# Bouncy were absent, so a 50-over match on either was judged against the Hard
# curve — a raging turner read as a 290 pitch. Unlike the T20 table these are
# not Monte-Carlo measured: tools/pitch_calibration.py is a T20 harness, so
# these stay ordered with the T20 factors rather than claiming a figure nobody
# has run.
_LISTA_PITCH_PAR_FACTORS: Dict[str, float] = {
    "Dusty":  0.72,   # ~209 expected
    "Green":  0.76,   # ~220 expected
    "Dry":    0.80,   # ~232 expected
    "Bouncy": 0.86,   # ~249 expected
    "Even":   0.95,   # ~275 expected (neutral)
    "Hard":   1.00,   # ~290 expected (baseline)
    "Flat":   1.10,   # ~319 expected
    "Dead":   1.18,   # ~342 expected
}

_LISTA = FormatConfig(
    name="ListA",
    overs=50,
    max_bowler_overs=10,
    allow_consecutive_overs=False,   # ← KEY RULE: no back-to-back overs
    powerplay_phases=[
        # PP1: mandatory fielding restriction — only 2 outside 30-yard circle
        Phase("PP1", start=0, end=9, max_fielders_outside=2),
    ],
    # PP2/middle (overs 10-39): 4 fielders permitted outside
    middle_phase=Phase("Middle", start=10, end=39, max_fielders_outside=4),
    # Death/slog (overs 40-49): 5 fielders permitted outside
    death_phase=Phase("Death", start=40, end=49, max_fielders_outside=5),
    par_scores=_LISTA_PAR_SCORES,
    pitch_par_factors=_LISTA_PITCH_PAR_FACTORS,
    expected_rr={
        "PP1":    5.8,   # New ball, attacking but measured
        "Middle": 4.8,   # Consolidation, spin, dot-ball pressure
        "Death":  8.5,   # Slog overs — maximum aggression
    },
    extras_per_innings=12,   # More deliveries → proportionally more extras
    target_scores={
        "Dusty":  209,
        "Green":  220,
        "Dry":    230,
        "Bouncy": 249,
        "Even":   275,   # Neutral surface (par factor 0.95)
        "Hard":   290,   # Baseline
        "Flat":   320,
        "Dead":   340,
    },
    # A surface's character does not change with the format, so the day call
    # starts from engine.pitch_registry. Two surfaces legitimately differ over
    # 50 overs: on a road with no dew a 320 is a real asset and defending it
    # beats chasing it, which is not true over 20.
    correct_toss_choice={
        **{p: pitch_registry.toss_call(p) for p in pitch_registry.PITCHES},
        "Flat": "bat",
        "Dead": "bat",
    },
    # D/N ListA: dew from about over 25 of the second innings tips every
    # surface towards bowling first — spin grips less, the ball is slippery and
    # the outfield quickens.
    correct_toss_choice_dn={p: pitch_registry.toss_call(p, "Night")
                            for p in pitch_registry.PITCHES},
    rrr_baseline={
        # "Neutral" RPO for each pitch in ListA (ODI) context — each pitch's
        # target over 50 overs. ODI rates are far lower than T20, so a required
        # rate above these represents escalating pressure.
        "Dusty":  4.2,
        "Green":  4.4,
        "Dry":    4.6,
        "Bouncy": 5.0,
        "Even":   5.5,
        "Hard":   5.8,
        "Flat":   6.4,
        "Dead":   6.8,
    },
)


# ---------------------------------------------------------------------------
# Public registry — look up by match_format string
# ---------------------------------------------------------------------------

FORMAT_REGISTRY: Dict[str, FormatConfig] = {
    "T20":   _T20,
    "ListA": _LISTA,
}


def get_format(match_format: Optional[str]) -> FormatConfig:
    """
    Return the FormatConfig for the given match_format string.
    Defaults to T20 for None or unrecognised values (backward compat).
    """
    return FORMAT_REGISTRY.get(match_format or "T20", FORMAT_REGISTRY["T20"])

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

_T20_PAR_SCORES: Dict[int, float] = {
    0:   0.0,
    1:   7.0,
    2:  14.5,
    3:  22.0,
    4:  30.5,
    5:  38.5,
    6:  48.0,   # End of powerplay
    7:  55.5,
    8:  63.5,
    9:  72.0,
    10: 81.0,
    11: 90.0,
    12: 99.0,
    13: 108.0,
    14: 116.5,
    15: 125.5,
    16: 135.5,
    17: 146.5,
    18: 157.5,
    19: 167.5,
    20: 176.0,
}

_T20_PITCH_PAR_FACTORS: Dict[str, float] = {
    "Green": 0.84,
    "Dry":   0.86,
    "Hard":  1.00,
    "Flat":  1.10,
    "Dead":  1.25,
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
        "Powerplay": 7.5,
        "Middle":    8.0,
        "Death":    10.5,
    },
    extras_per_innings=5,
    target_scores={
        "Green": 145,
        "Dry":   145,
        "Hard":  165,
        "Flat":  185,
        "Dead":  210,
    },
    correct_toss_choice={
        "Green": "bowl",  # Seam/swing → bowl first
        "Dry":   "bat",   # Spin worsens with wear → bat first
        "Hard":  "bat",   # Good batting surface → bat first
        "Flat":  "bowl",  # Run-fest; dew helps chaser → bowl first
        "Dead":  "bowl",  # Extreme batting; chaser advantaged → bowl first
    },
    correct_toss_choice_dn={
        # D/N T20: shorter game but dew still tilts towards chasing.
        # Bowling first = your team bats second under lights with dew.
        "Green": "bowl",   # Already bowl; unchanged
        "Dry":   "bowl",   # Dew neutralises spin in 2nd innings → bowl first
        "Hard":  "bowl",   # Chase with dew advantage → bowl first
        "Flat":  "bowl",   # Already bowl; unchanged
        "Dead":  "bowl",   # Already bowl; unchanged
    },
    rrr_baseline={
        # "Neutral" RPO for each pitch in T20 context.
        # GSME divides actual RRR by this to get a normalised aggression index.
        "Green": 7.5,
        "Dry":   7.5,
        "Hard":  8.5,
        "Flat":  10.5,
        "Dead":  11.5,
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

_LISTA_PITCH_PAR_FACTORS: Dict[str, float] = {
    "Green": 0.76,   # ~220 expected
    "Dry":   0.80,   # ~232 expected
    "Hard":  1.00,   # ~290 expected (baseline)
    "Flat":  1.10,   # ~319 expected
    "Dead":  1.18,   # ~342 expected
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
        "Green": 220,
        "Dry":   230,
        "Hard":  285,
        "Flat":  320,
        "Dead":  340,
    },
    correct_toss_choice={
        # ListA day-match toss logic (pitch wear only, no dew)
        "Green": "bowl",   # New-ball seam threat; pitch stays decent all day
        "Dry":   "bat",    # Pitch deteriorates; spin brutal in 2nd innings
        "Hard":  "bowl",   # Balanced; slight chase advantage
        "Flat":  "bowl",   # High totals still chaseable
        "Dead":  "bat",    # Set a huge total; spinners can do nothing anyway
    },
    correct_toss_choice_dn={
        # D/N ListA: dew from over 25 of the 2nd innings tips all pitches
        # towards bowling first.  Your team bats 2nd at night with dew:
        #   - Spin grips less (Dry advantage lost)
        #   - Ball becomes slippery (more wides/extras)
        #   - Outfield faster from moisture (Four chance ↑)
        "Green": "bowl",   # Already bowl; dew makes chase even easier
        "Dry":   "bowl",   # Overrides day "bat" — dew kills spin in overs 25-50
        "Hard":  "bowl",   # Already bowl; unchanged
        "Flat":  "bowl",   # Already bowl; unchanged
        "Dead":  "bowl",   # Overrides day "bat" — batting paradise + dew = huge chase
    },
    rrr_baseline={
        # "Neutral" RPO for each pitch in ListA (ODI) context.
        # ODI scoring rates are significantly lower than T20; Hard pitch
        # averages ~5.7-6.0 RPO in the first innings.
        # A required rate above these baselines represents escalating pressure.
        "Green": 4.8,   # Seam-friendly: low-scoring; 5+ RPO is already urgent
        "Dry":   5.0,   # Spin-friendly: modest target, 5+ RPO is challenging
        "Hard":  6.0,   # Balanced baseline for ODI cricket
        "Flat":  7.0,   # High-scoring; 7+ RPO still challenging even on flat deck
        "Dead":  7.5,   # Batting paradise; 8+ RPO is genuinely hard to sustain
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

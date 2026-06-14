"""
super_over_outcome.py

Simulates each delivery in a Super Over using a specialized scoring matrix
and “excitement” multipliers, while aligning with the new 60% pitch‐influence
/ 40% player‐skill rating system defined in ball_outcome.py. All original
features—SUPER_OVER_SCORING_MATRIX, commentary_templates, and frame logic—
are preserved, with only the internal probability computations updated to
remove the old pitch_factor and incorporate get_pitch_run_multiplier and
get_pitch_wicket_multiplier instead.
"""

import random
from engine.ball_outcome import (
    get_pitch_run_multiplier,
    get_pitch_wicket_multiplier,
    _get_wicket_type_by_bowling
)

# -----------------------------------------------------------------------------
# 1) Super Over outcome probabilities (more exciting than a regular over)
# -----------------------------------------------------------------------------
SUPER_OVER_SCORING_MATRIX = {
    "Dot":     0.20,   # Reduced — high pressure, fewer leaves/blocks
    "Single":  0.25,   # Quick singles under pressure
    "Double":  0.10,   # Running twos
    "Three":   0.005,  # Almost never in a super over
    "Four":    0.18,   # Boundaries flow in super overs
    "Six":     0.12,   # Big hits under pressure
    "Wicket":  0.04,   # Pressure wickets
    "Extras":  0.045   # Bowler nerves
}

def calculate_super_over_outcome(
    batter: dict,
    bowler: dict,
    pitch: str,
    streak: dict,
    over_number: int,
    batter_runs: int
) -> dict:
    """
    Simulates one delivery in a Super Over. Uses SUPER_OVER_SCORING_MATRIX
    plus “excitement” modifiers:

      - Fours/Sixes get an extra 1.2× boost for “super‐over excitement.”
        If the batter has already hit ≥3 boundaries in this over, further
        boundary chance is reduced by 10% (×0.9).

      - Wicket chances are 1.3× higher due to pressure. If the batter has
        hit ≥2 boundaries, wicket chance is further boosted by 1.4×.

      - Extras are 1.2× more probable under pressure.

    All “run” and “wicket” probabilities blend pitch influence (60%) with
    player skill (40%), using get_pitch_run_multiplier and
    get_pitch_wicket_multiplier from ball_outcome.py. Extras remain purely
    a function of bowler error.

    Parameters:
        batter (dict): {
            "name": str,
            "batting_rating": int (0–100),
            "batting_hand": "Left" | "Right"
        }
        bowler (dict): {
            "name": str,
            "bowling_rating": int (0–100),
            "fielding_rating": int (0–100),
            "bowling_type": one of allowed styles or "",
            "bowling_hand": "Left" | "Right"
        }
        pitch (str): one of {"Green", "Flat", "Dry", "Hard", "Dead"}.
        streak (dict): e.g. {"boundaries": int} representing number of
                        boundaries hit so far in this over by the current batter.
        over_number (int): zero-based index of the current over in the match.
                           For a Super Over, this will typically be 0.
        batter_runs (int): total runs scored by the batter so far in the match
                           (used only if additional context is required).

    Returns:
        dict with keys:
            - "type":       "run" | "wicket" | "extra"
            - "runs":       0 | 1 | 2 | 3 | 4 | 6
            - "description": detailed commentary string
            - "wicket_type": if type == "wicket", one of {"Caught","Bowled","LBW","Run Out"}
            - "is_extra":   True | False
            - "batter_out": True | False
    """

    # 1) Unpack player ratings & attributes
    batting = batter["batting_rating"]
    bowling = bowler["bowling_rating"]
    fielding = bowler["fielding_rating"]
    batting_hand = batter["batting_hand"]
    bowling_hand = bowler["bowling_hand"]
    bowling_type = bowler["bowling_type"]

    # 2) Build raw weights using SUPER_OVER_SCORING_MATRIX + new 60/40 logic
    outcomes = list(SUPER_OVER_SCORING_MATRIX.keys())
    raw_weights = {}

    for outcome in outcomes:
        base_prob = SUPER_OVER_SCORING_MATRIX[outcome]

        if outcome in ("Dot", "Single", "Double", "Three"):
            # Run outcomes (non-boundary) blend pitch & skill
            skill_frac = batting / (batting + bowling) if (batting + bowling) > 0 else 0.5
            pitch_frac = get_pitch_run_multiplier(pitch)
            blended_frac = 0.4 * skill_frac + 0.6 * pitch_frac
            weight = base_prob * blended_frac

        elif outcome in ("Four", "Six"):
            # Boundary outcomes: same blending + 1.2× super-over excitement
            skill_frac = batting / (batting + bowling) if (batting + bowling) > 0 else 0.5
            pitch_frac = get_pitch_run_multiplier(pitch)
            blended_frac = 0.4 * skill_frac + 0.6 * pitch_frac
            weight = base_prob * blended_frac * 1.2

            # If batter has hit ≥3 boundaries already, diminish chance by 10%
            if streak.get("boundaries", 0) >= 3:
                weight *= 0.9

        elif outcome == "Wicket":
            # Wicket outcome: blend pitch & skill for wicket, then 1.3× pressure boost
            skill_frac = ((bowling / (batting + bowling)) * (fielding / 100.0)) if (batting + bowling) > 0 else 0.5
            pitch_frac = get_pitch_wicket_multiplier(pitch, bowling_type)
            blended_frac = 0.4 * skill_frac + 0.6 * pitch_frac
            weight = base_prob * blended_frac * 1.3

            # If batter has hit ≥2 boundaries, boost wicket chance by 1.4×
            if streak.get("boundaries", 0) >= 2:
                weight *= 1.4

        else:  # outcome == "Extras"
            # Extras depend solely on bowler error, with a 1.2× super-over boost
            weight = base_prob * ((100 - bowling) / 100.0) * 1.2

        # Ensure non-negative
        raw_weights[outcome] = max(weight, 0.0)

    # 3) Normalize raw weights to probabilities
    total_weight = sum(raw_weights.values())
    if total_weight <= 0:
        # In the extremely unlikely case of zero total weight, default to a Dot ball
        chosen = "Dot"
    else:
        normalized = [raw_weights[o] / total_weight for o in outcomes]
        chosen = random.choices(outcomes, weights=normalized, k=1)[0]

    # 4) Construct result dict with default structure
    result = {
        "type": None,
        "runs": 0,
        "description": "",
        "wicket_type": None,
        "is_extra": False,
        "batter_out": False
    }

    # 5) Commentary templates (unchanged from original)
    commentary_templates = {
        "Dot": [
            "Pressure delivery! No run.",
            "Dot ball under pressure."
        ],
        "Single": [
            "Quick single under pressure.",
            "Rotates strike in super over."
        ],
        "Double": [
            "Pushed into the gap for two!",
            "Great running, two runs."
        ],
        "Three": [
            "Excellent placement for three!",
            "Superb running between wickets!"
        ],
        "Four": [
            "BOUNDARY! Crucial four in super over!",
            "What a shot under pressure! FOUR!"
        ],
        "Six": [
            "MASSIVE SIX! Gone into the stands!",
            "HUGE hit! Six runs in super over!"
        ],
        "Wicket": [
            "WICKET! Pressure gets to batsman!",
            "OUT! Crucial breakthrough!"
        ],
        "Extras": [
            "Extra runs under pressure.",
            "Pressure gets to bowler - extras."
        ]
    }

    # 6) Populate result based on chosen outcome
    if chosen == "Wicket":
        result["type"] = "wicket"
        result["runs"] = 0
        result["batter_out"] = True

        # A7+A6: Wicket type based on bowling style (includes Stumped)
        wicket_types, weights_pct = _get_wicket_type_by_bowling(bowling_type)
        chosen_wicket = random.choices(wicket_types, weights=weights_pct, k=1)[0]
        result["wicket_type"] = chosen_wicket
        result["description"] = random.choice(commentary_templates["Wicket"])

        # A1: Run Out can happen attempting 1, 2, or 3 runs.
        # Distribution roughly mirrors how often run-outs occur per attempted total.
        if chosen_wicket == "Run Out":
            result["runs"] = random.choices([0, 1, 2], weights=[0.30, 0.60, 0.10], k=1)[0]

    elif chosen == "Extras":
        result["type"] = "extra"
        result["is_extra"] = True

        # A4: Weighted extra type selection
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
        result["description"] = f"{random.choice(commentary_templates['Extras'])} ({extra_choice})"

    else:
        # Run outcomes (Dot, Single, Double, Three, Four, Six)
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
        result["description"] = random.choice(commentary_templates[chosen])

    return result

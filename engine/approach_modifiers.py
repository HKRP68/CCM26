"""Approach modifiers for the over-by-over Challenge League simulation.

Each over both captains pick a tactical *approach*. These approaches scale the
raw outcome weights produced by ``engine.ball_outcome.calculate_outcome`` right
before the final delivery is sampled, so the result still respects ratings,
pitch, momentum, pressure and scenario logic — the approach only *tilts* the
odds the way the spec in ``New Features.md`` describes.

Outcome keys match the engine's weight dict:
    Dot, Single, Double, Three, Four, Six, Wicket, Extras
Any key omitted from a table is left unchanged (multiplier 1.0). The "balanced"
approach on both sides is intentionally neutral (all multipliers 1.0).
"""

# ── Batting approaches ────────────────────────────────────────────────
# Ordered for button display: (key, emoji, label).
BATTING_APPROACHES = [
    ("defensive", "🛡", "Defensive"),
    ("rotate", "🔁", "Rotate Strike"),
    ("balanced", "⚖", "Balanced"),
    ("aggressive", "🚀", "Aggressive"),
    ("ultra", "💥", "Ultra Attack"),
]

# ── Bowling approaches ────────────────────────────────────────────────
BOWLING_APPROACHES = [
    ("defensive", "🛡", "Defensive"),
    ("balanced", "⚖", "Balanced"),
    ("mixed", "☠️", "Mixed"),
    ("aggressive", "🔥", "Aggressive"),
    ("variation", "🌀", "Variation"),
]

BATTING_KEYS = {k for k, _, _ in BATTING_APPROACHES}
BOWLING_KEYS = {k for k, _, _ in BOWLING_APPROACHES}

DEFAULT_BATTING = "balanced"
DEFAULT_BOWLING = "balanced"

# Per-approach outcome multipliers. Missing outcomes default to 1.0.
_BATTING_MULT = {
    "defensive": {"Dot": 1.40, "Single": 1.20, "Double": 0.90, "Three": 0.80,
                  "Four": 0.50, "Six": 0.30, "Wicket": 0.60},
    "rotate":    {"Dot": 0.80, "Single": 1.50, "Double": 1.40, "Three": 1.10,
                  "Four": 0.70, "Six": 0.50, "Wicket": 0.75},
    "balanced":  {},
    "aggressive": {"Dot": 0.85, "Single": 0.95, "Double": 1.00, "Three": 1.00,
                   "Four": 1.40, "Six": 1.50, "Wicket": 1.35},
    "ultra":     {"Dot": 0.70, "Single": 0.80, "Double": 0.90, "Three": 0.90,
                  "Four": 1.60, "Six": 2.20, "Wicket": 1.90},
}

_BOWLING_MULT = {
    "defensive": {"Dot": 1.35, "Single": 1.10, "Double": 0.95,
                  "Four": 0.55, "Six": 0.45, "Wicket": 0.80},
    "balanced":  {},
    "mixed":     {"Dot": 1.15, "Single": 1.00, "Four": 0.80,
                  "Six": 0.60, "Wicket": 1.10},
    "aggressive": {"Dot": 0.95, "Single": 1.00, "Four": 1.20,
                   "Six": 1.20, "Wicket": 1.50},
    "variation": {"Dot": 1.10, "Single": 1.00, "Four": 0.85,
                  "Six": 0.80, "Wicket": 1.25},
}


def normalize_batting(approach):
    a = (approach or "").strip().lower()
    return a if a in BATTING_KEYS else DEFAULT_BATTING


def normalize_bowling(approach):
    a = (approach or "").strip().lower()
    return a if a in BOWLING_KEYS else DEFAULT_BOWLING


def _mixed_six_mult(bowler_rating):
    """Mixed bowling suppresses sixes more when the bowler is highly rated
    ("reduces six chance if bowler rating is high"); weaker bowlers leak."""
    try:
        r = float(bowler_rating)
    except (TypeError, ValueError):
        return 0.70
    if r >= 80:
        return 0.55
    if r >= 65:
        return 0.70
    return 0.90


def apply_approach_modifiers(weights, batting_approach=None, bowling_approach=None,
                             bowler_rating=None):
    """Return a new weight dict scaled by both captains' approaches.

    weights: {outcome_key: float} as built inside calculate_outcome.
    batting_approach / bowling_approach: approach keys (normalized internally).
    bowler_rating: used for Mixed bowling's rating-scaled six suppression.
    """
    bat = normalize_batting(batting_approach)
    bowl = normalize_bowling(bowling_approach)
    if bat == DEFAULT_BATTING and bowl == DEFAULT_BOWLING:
        return dict(weights)

    bat_tbl = _BATTING_MULT.get(bat, {})
    bowl_tbl = dict(_BOWLING_MULT.get(bowl, {}))
    if bowl == "mixed":
        bowl_tbl["Six"] = _mixed_six_mult(bowler_rating)

    out = {}
    for key, val in weights.items():
        mult = bat_tbl.get(key, 1.0) * bowl_tbl.get(key, 1.0)
        out[key] = max(0.0, val * mult)
    return out


def batting_keyboard_rows(callback_prefix):
    """[(text, callback_data)] rows for the batting approach buttons."""
    return [(f"{emoji} {label}", f"{callback_prefix}{idx}")
            for idx, (_, emoji, label) in enumerate(BATTING_APPROACHES)]


def bowling_keyboard_rows(callback_prefix):
    """[(text, callback_data)] rows for the bowling approach buttons."""
    return [(f"{emoji} {label}", f"{callback_prefix}{idx}")
            for idx, (_, emoji, label) in enumerate(BOWLING_APPROACHES)]


def batting_label(key):
    key = normalize_batting(key)
    for k, emoji, label in BATTING_APPROACHES:
        if k == key:
            return f"{emoji} {label}"
    return key


def bowling_label(key):
    key = normalize_bowling(key)
    for k, emoji, label in BOWLING_APPROACHES:
        if k == key:
            return f"{emoji} {label}"
    return key

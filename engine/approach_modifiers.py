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

# ── Balance note ──────────────────────────────────────────────────────
# The first version of these tables was measurably broken: simulated against
# the live engine (500 overs per cell, four pitches, seven rating match-ups),
# Ultra Attack beat every other batting approach in *every* bowling column and
# Defensive bowling beat every other plan in *every* batting row. Held for a
# whole innings that was 244 all-out-proof runs for Ultra against 180 for
# Defensive, so the approach layer had exactly one correct answer for each
# captain and picking anything else was a mistake. The tables below restore the
# trade-off, along two axes:
#
#   • **Risk is priced.** Aggressive and Ultra now buy their boundaries with a
#     genuinely higher wicket rate, so slogging from ball one runs out of
#     batting long before it runs out of overs — which is what stops "always
#     Ultra" from being free.
#   • **Every plan has a weakness** (:data:`_COUNTER_MULT`). Defensive bowling
#     shuts the boundary down but hands over singles; attacking plans buy
#     wickets by offering pace to hit; change-of-pace punishes a slogger but is
#     wasted on a batter content to knock it around.
#
# Between them those make the over a real simultaneous-move game with a mixed
# solution instead of a table with one right answer. Measured the same way
# afterwards, the best reply now moves with what the other captain picked:
#
#   vs Defensive bowling → Ultra          vs Defensive batting  → Aggressive
#   vs Balanced  bowling → Ultra          vs Rotate    batting  → Defensive
#   vs Mixed     bowling → Rotate         vs Balanced  batting  → Mixed
#   vs Aggressive bowling→ Aggressive     vs Aggressive batting → Variation
#   vs Variation bowling → Balanced       vs Ultra     batting  → Variation
#
# Two options are deliberately never *optimal* on runs alone and that is the
# role they play rather than a bug: Balanced bowling is the safe neutral, and
# Defensive batting is a survival tool whose value (protecting the last wicket,
# seeing off a spell) a runs-scored yardstick cannot show.
#
# Any retune should be re-checked the same way — ``tests/test_cipl_approach.py``
# asserts the no-dominant-approach property directly, so a change that brings
# back a single right answer fails the suite rather than shipping quietly.

# Per-approach outcome multipliers. Missing outcomes default to 1.0.
_BATTING_MULT = {
    "defensive": {"Dot": 1.45, "Single": 1.25, "Double": 0.90, "Three": 0.80,
                  "Four": 0.45, "Six": 0.25, "Wicket": 0.45},
    "rotate":    {"Dot": 0.78, "Single": 1.66, "Double": 1.45, "Three": 1.15,
                  "Four": 0.72, "Six": 0.48, "Wicket": 0.64},
    "balanced":  {},
    "aggressive": {"Dot": 0.86, "Single": 0.92, "Double": 1.00, "Three": 1.00,
                   "Four": 1.38, "Six": 1.45, "Wicket": 1.45},
    "ultra":     {"Dot": 0.72, "Single": 0.78, "Double": 0.88, "Three": 0.90,
                  "Four": 1.51, "Six": 1.98, "Wicket": 1.80},
}

_BOWLING_MULT = {
    "defensive": {"Dot": 1.04, "Single": 1.48, "Double": 0.98,
                  "Four": 0.76, "Six": 0.70, "Wicket": 0.66},
    "balanced":  {},
    "mixed":     {"Dot": 1.00, "Single": 1.14, "Double": 1.08, "Four": 0.84,
                  "Six": 0.60, "Wicket": 1.18},
    "aggressive": {"Dot": 0.86, "Single": 1.02, "Four": 1.29,
                   "Six": 1.30, "Wicket": 1.55},
    "variation": {"Dot": 1.05, "Single": 0.98, "Four": 0.90,
                  "Six": 0.84, "Wicket": 1.10},
}

# ── The counter matrix ───────────────────────────────────────────────
# Applied on top of both tables above, keyed (batting approach, bowling plan).
# The per-approach tables alone are separable — each one scales its outcomes
# the same way whatever the other captain does — and a separable game collapses
# to one dominant answer per side. These interaction terms are what make the
# over rock-paper-scissors: the same plan is a match-winner against one intent
# and a gift against another, so neither captain can settle on a favourite.
#
# Read each line as the cricket it describes.
_COUNTER_MULT = {
    # Change of pace is the answer to a slogger — but only to a slogger.
    ("ultra", "variation"):      {"Six": 0.55, "Four": 0.72, "Wicket": 1.48},
    ("aggressive", "variation"): {"Six": 0.68, "Four": 0.82, "Wicket": 1.34},
    ("ultra", "mixed"):          {"Six": 0.70, "Four": 0.86, "Wicket": 1.24},
    ("aggressive", "mixed"):     {"Six": 0.82, "Four": 0.92, "Wicket": 1.22},

    # ...and pace on a length to a batter swinging through the line is a gift.
    ("ultra", "aggressive"):      {"Four": 1.10, "Six": 1.12, "Wicket": 0.90},
    ("aggressive", "aggressive"): {"Four": 1.16, "Six": 1.20, "Wicket": 0.94},

    # Attacking fields against someone with no intent of scoring: all upside.
    ("defensive", "aggressive"): {"Wicket": 1.75, "Four": 0.78, "Six": 0.72,
                                  "Dot": 1.12},
    ("rotate", "aggressive"):    {"Wicket": 1.45, "Four": 0.84, "Six": 0.80,
                                  "Single": 1.14},

    # Boundary-denial concedes the single — so milking it beats it.
    ("rotate", "defensive"):     {"Single": 1.30, "Double": 1.12, "Dot": 0.84},
    ("balanced", "defensive"):   {"Single": 1.12, "Dot": 0.94},
    ("aggressive", "defensive"): {"Four": 1.14, "Six": 1.18},
    ("ultra", "defensive"):      {"Four": 1.18, "Six": 1.24},
    # Two captains both refusing to take a risk: nobody scores, nobody gets out.
    ("defensive", "defensive"):  {"Dot": 1.14, "Wicket": 0.82},

    # Mixed and Variation are built to strangle the single, which is exactly
    # what a strike-rotator needs — and they nip at a batter with no answer.
    ("rotate", "mixed"):         {"Single": 0.84, "Dot": 1.12, "Wicket": 1.14},
    ("rotate", "variation"):     {"Single": 1.02, "Dot": 0.98, "Wicket": 0.96},
    ("balanced", "mixed"):       {"Wicket": 1.24, "Four": 0.88},
    ("defensive", "mixed"):      {"Wicket": 1.10},
    ("defensive", "variation"):  {"Wicket": 1.12},
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

    Three layers multiply together: the batting intent, the bowling plan, and
    the :data:`_COUNTER_MULT` term for how those two specific choices interact.
    """
    bat = normalize_batting(batting_approach)
    bowl = normalize_bowling(bowling_approach)
    counter = _COUNTER_MULT.get((bat, bowl), {})
    if bat == DEFAULT_BATTING and bowl == DEFAULT_BOWLING and not counter:
        return dict(weights)

    bat_tbl = _BATTING_MULT.get(bat, {})
    bowl_tbl = dict(_BOWLING_MULT.get(bowl, {}))
    if bowl == "mixed":
        bowl_tbl["Six"] = _mixed_six_mult(bowler_rating)

    out = {}
    for key, val in weights.items():
        mult = (bat_tbl.get(key, 1.0) * bowl_tbl.get(key, 1.0)
                * counter.get(key, 1.0))
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

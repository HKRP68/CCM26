"""Bounded condition and tactic adjustments.

The brief (§5) asks for the real-data baseline first, then *bounded*
adjustments for pitch, weather/dew, tactics, fatigue, wicket pressure and the
chase — and explicitly warns against applying the same influence twice.

Two rules make that concrete here:

1. **One family, one application.** Each function below owns exactly one
   influence and is applied exactly once. Phase is *not* in this module,
   because phase is already the baseline's third hierarchy level; applying a
   powerplay boost on top of a powerplay baseline is the double count the
   brief names. Likewise the rating gap is not here — that is
   ``rating_curve``.

2. **Everything is bounded, individually and jointly.** A family may move an
   outcome by at most ``conditions.bound`` (1.60x by default) and all
   families together by at most ``conditions.total_bound`` (2.20x). The
   baseline therefore always dominates: conditions colour a delivery, they
   cannot replace the distribution with a different sport.

Where the data does not support an effect, the effect is *neutral* rather
than invented. The handedness match-up is the clearest case: Cricsheet gives
bowler and batter but no line, length or turn, and published handedness
splits are small and noisy, so ``style_multipliers`` applies a deliberately
small, documented nudge and nothing more. See §8 of the brief.

Pure functions, no RNG.
"""

import math

from engine.realism import styles
from engine.realism.config import get_config

OUTCOMES = ("Dot", "Single", "Double", "Three", "Four", "Six", "Wicket", "Extras")

_ONE = {o: 1.0 for o in OUTCOMES}

# --------------------------------------------------------------------------
# Pitch
# --------------------------------------------------------------------------
# The engine's own pitch model is the source of truth for *which way* a
# surface plays: engine/ball_outcome.PITCH_RUN_FACTOR and
# PITCH_WICKET_FACTOR, which the conditions engine and ground registry
# already share. Reusing them (rather than inventing a second pitch table)
# keeps /cipl's surfaces behaving the way players already expect, and keeps
# the realism model honest about what is empirical: the *level* of scoring is
# fitted to real data, the relative ordering of surfaces is CCM26's existing
# calibrated model.
_PITCH_SHAPE = {
    # run_factor is applied to scoring outcomes, wicket_factor to Wicket,
    # with dots taking the complement so the distribution does not drift.
    "Flat":   {"runs": 1.08, "wicket": 0.88},
    "Dead":   {"runs": 1.10, "wicket": 0.85},
    "Hard":   {"runs": 1.04, "wicket": 0.96},
    "Even":   {"runs": 1.00, "wicket": 1.00},
    "Bouncy": {"runs": 1.00, "wicket": 1.08},
    "Dry":    {"runs": 0.95, "wicket": 1.08},
    "Dusty":  {"runs": 0.92, "wicket": 1.12},
    "Green":  {"runs": 0.92, "wicket": 1.15},
}


def _scale(bound: float, mult: float, strength: float) -> float:
    """Apply *strength* to a multiplier in log space, then bound it."""
    if strength <= 0.0:
        return 1.0
    v = math.exp(math.log(max(1e-9, mult)) * strength)
    return max(1.0 / bound, min(bound, v))


def _run_shape(runs_mult: float, wicket_mult: float, bound: float,
               strength: float) -> dict:
    out = dict(_ONE)
    r = _scale(bound, runs_mult, strength)
    w = _scale(bound, wicket_mult, strength)
    # Boundaries move more than singles for the same change in scoring rate:
    # a flatter surface mostly buys fours and sixes, not extra twos.
    out["Four"] = r ** 1.6
    out["Six"] = r ** 2.0
    out["Double"] = r ** 0.8
    out["Three"] = r ** 0.4
    out["Single"] = r ** 0.2
    out["Dot"] = r ** -1.0
    out["Wicket"] = w
    return {k: max(1.0 / bound, min(bound, v)) for k, v in out.items()}


def pitch_multipliers(pitch: str, pitch_wear: float = 0.0,
                      bowling_type: str = None, cfg: dict = None) -> dict:
    """Surface effect, including progressive wear."""
    c = (cfg or get_config())["conditions"]
    bound = float(c["bound"])
    strength = float(c.get("pitch_strength", 1.0))
    shape = _PITCH_SHAPE.get(str(pitch or "Even"), _PITCH_SHAPE["Even"])
    out = _run_shape(shape["runs"], shape["wicket"], bound, strength)

    # Wear: a used surface grips and slows. Spinners gain more than pace,
    # which is the one wear asymmetry the engine already models and real
    # cricket unambiguously shows.
    wear = max(0.0, min(1.0, float(pitch_wear or 0.0)))
    if wear > 0.0 and strength > 0.0:
        is_spin = styles.bowling_family(bowling_type) == styles.SPIN
        w_runs = 1.0 - (0.10 if is_spin else 0.04) * wear
        w_wkt = 1.0 + (0.22 if is_spin else 0.06) * wear
        wear_mults = _run_shape(w_runs, w_wkt, bound, strength)
        out = {k: out[k] * wear_mults[k] for k in OUTCOMES}
    return {k: max(1.0 / bound, min(bound, v)) for k, v in out.items()}


# --------------------------------------------------------------------------
# Weather / dew
# --------------------------------------------------------------------------
def weather_multipliers(dew: float = 0.0, is_day_night: bool = False,
                        innings: int = 1, phase: str = "middle",
                        cfg: dict = None) -> dict:
    """Dew effect. Second innings of a day/night game only.

    Dew makes the ball skid and the grip go: scoring rises, spinners lose
    their bite, and the fielding side's extras creep up. This is a well
    documented, large and directional effect in night T20, which is why it
    survives the "only where data supports it" filter — but the *size* here
    is CCM26's existing dew model, not a figure measured from Cricsheet
    (Cricsheet records no weather at all).
    """
    c = (cfg or get_config())["conditions"]
    bound = float(c["bound"])
    strength = float(c.get("weather_strength", 1.0))
    d = max(0.0, min(1.0, float(dew or 0.0)))
    if is_day_night and int(innings or 1) == 2 and d <= 0.0:
        # A night chase gets dew even when the caller has no explicit value,
        # ramping through the innings the way it does in reality.
        d = 0.35 if phase == "middle" else (0.55 if phase == "death" else 0.15)
    if d <= 0.0 or strength <= 0.0:
        return dict(_ONE)
    out = _run_shape(1.0 + 0.10 * d, 1.0 - 0.10 * d, bound, strength)
    out["Extras"] = _scale(bound, 1.0 + 0.18 * d, strength)
    return {k: max(1.0 / bound, min(bound, v)) for k, v in out.items()}


# --------------------------------------------------------------------------
# Tactics (existing CCM26 approaches)
# --------------------------------------------------------------------------
# Intent, expressed the way the existing approach system does, but as a
# single bounded family so it cannot stack with engine/approach_modifiers.
# When the caller leaves approach modifiers switched on (the default for
# /cipl, which has a whole UI for them), this family is disabled to avoid
# exactly the double count the brief warns about — see
# outcome_distribution.build().
_BAT_INTENT = {
    "defensive": {"runs": 0.86, "wicket": 0.80},
    "cautious":  {"runs": 0.90, "wicket": 0.84},
    "rotate":    {"runs": 0.96, "wicket": 0.94},
    "balanced":  {"runs": 1.00, "wicket": 1.00},
    "aggressive": {"runs": 1.12, "wicket": 1.22},
    "ultra":     {"runs": 1.22, "wicket": 1.48},
}
_BOWL_INTENT = {
    "defensive": {"runs": 0.92, "wicket": 0.92},
    "balanced":  {"runs": 1.00, "wicket": 1.00},
    "mixed":     {"runs": 1.00, "wicket": 1.02},
    "variation": {"runs": 1.02, "wicket": 1.06},
    "aggressive": {"runs": 1.08, "wicket": 1.12},
}


def _intent_key(value, table):
    s = str(value or "").strip().lower()
    if not s:
        return None
    for key in table:
        if key in s:
            return key
    return None


def tactics_multipliers(batting_approach=None, bowling_approach=None,
                        cfg: dict = None) -> dict:
    c = (cfg or get_config())["conditions"]
    bound = float(c["bound"])
    strength = float(c.get("tactics_strength", 1.0))
    if strength <= 0.0:
        return dict(_ONE)
    runs, wicket = 1.0, 1.0
    bk = _intent_key(batting_approach, _BAT_INTENT)
    if bk:
        runs *= _BAT_INTENT[bk]["runs"]
        wicket *= _BAT_INTENT[bk]["wicket"]
    wk = _intent_key(bowling_approach, _BOWL_INTENT)
    if wk:
        runs *= _BOWL_INTENT[wk]["runs"]
        wicket *= _BOWL_INTENT[wk]["wicket"]
    if runs == 1.0 and wicket == 1.0:
        return dict(_ONE)
    return _run_shape(runs, wicket, bound, strength)


# --------------------------------------------------------------------------
# Fatigue, wicket pressure, chase
# --------------------------------------------------------------------------
def fatigue_multipliers(bowler_fatigue: float = 1.0, cfg: dict = None) -> dict:
    """A tiring bowler. ``bowler_fatigue`` is the engine's 1.0 = fresh scale."""
    c = (cfg or get_config())["conditions"]
    bound = float(c["bound"])
    strength = float(c.get("fatigue_strength", 1.0))
    f = float(bowler_fatigue if bowler_fatigue is not None else 1.0)
    deficit = max(0.0, min(0.5, 1.0 - f))
    if deficit <= 0.0 or strength <= 0.0:
        return dict(_ONE)
    out = _run_shape(1.0 + 0.30 * deficit, 1.0 - 0.35 * deficit, bound, strength)
    out["Extras"] = _scale(bound, 1.0 + 0.50 * deficit, strength)
    return {k: max(1.0 / bound, min(bound, v)) for k, v in out.items()}


# THE SETTLING CURVE, AND WHY IT IS SOLVED RATHER THAN CHOSEN.
#
# Five bands of balls faced, with the share of an innings' deliveries that
# falls in each. Two of the five rows are stated and the "settled" row is
# SOLVED so the share-weighted mean of each column is exactly 1.0 — see
# wicket_pressure_multipliers for why that invariant matters.
#
# The shape is the real arc of a T20 innings, not a monotone bonus for
# surviving:
#
#   0-2 balls    most dangerous: a new batter has not seen the pace
#   3-5          still settling
#   6-10         nearly in
#   11-24        in. The safest a batter ever is, and the solved row.
#   25-44        accelerating: scoring faster AND getting out more.
#   45+          swinging at everything. A batter 45 balls into a T20 innings
#                is on seventy-odd and playing for the big total, not for
#                their average.
#
# How long a batter has been in is only half the story; what they have SCORED
# is the other half, and it is the half that decides whether a fifty becomes
# a hundred. See milestone_multipliers.
#
# The last two rows matter more than they look. A hazard that only ever falls
# as a batter settles has no way to end a long innings, which is how a model
# with a correct team total still produces four times too many centuries: the
# runs pile up in one player instead of spreading across five.
# Shares are MEASURED, not assumed: they are the observed fraction of
# deliveries in each band over 7,154 simulated CIPL deliveries
# (0-2: .188, 3-5: .146, 6-10: .181, 11-24: .304, 25-44: .161, 45+: .020).
#
# The 45+ band is only 2% of deliveries, which is exactly why it can carry a
# steep hazard almost for free: raising it barely moves the weighted mean, so
# the innings level is untouched while the century tail comes back to earth.
# Before this band existed the engine produced 0.14 centuries an innings
# against a real modern-IPL figure near 0.034 — a batter who got in was
# effectively unbowlable.
_SETTLE_BANDS = [
    # (upper bound on balls faced, measured share, runs mult, wicket mult)
    (2,            0.188, 0.90, 1.30),
    (5,            0.146, 0.95, 1.15),
    (10,           0.181, 0.98, 1.06),
    (24,           0.304, None, None),   # solved below
    (44,           0.161, 1.10, 1.20),
    (float("inf"), 0.020, 1.18, 1.80),
]


def _solve_neutral_band(bands):
    """Fill the one unsolved row so each column's weighted mean is 1.0.

    Solving rather than hand-setting is what keeps the shape of a hazard
    independent of the level: the bands can be re-shaped freely and the
    innings total stays on its measured anchor. Shared by both of the
    batter-state families below.
    """
    idx = next(i for i, b in enumerate(bands)
               if b[2] is None or b[3] is None)
    share = bands[idx][1]
    row = list(bands[idx])
    for col in (2, 3):
        if row[col] is not None:
            continue    # a column that is pinned everywhere needs no solve
        known = sum(b[1] * b[col] for i, b in enumerate(bands) if i != idx)
        row[col] = (1.0 - known) / share
    out = list(bands)
    out[idx] = tuple(row)
    return out


_SETTLE_BANDS = _solve_neutral_band(_SETTLE_BANDS)

# Share of balls bowled with 7 or more wickets down, and the same solve, so
# the tail-end pressure row does not shift the innings level either.
_TAIL_SHARE = 0.12
_TAIL_NEUTRAL_RUNS = (1.0 - _TAIL_SHARE * 0.94) / (1.0 - _TAIL_SHARE)
_TAIL_NEUTRAL_WICKET = (1.0 - _TAIL_SHARE * 1.10) / (1.0 - _TAIL_SHARE)


def wicket_pressure_multipliers(wickets: int, balls_faced: int = 99,
                                cfg: dict = None) -> dict:
    """Where a batter is in their innings, and how deep the side is.

    MEAN-NEUTRAL BY CONSTRUCTION. This is the subtle part, and getting it
    wrong is what makes a "realistic" model produce 8.4 wickets an innings.

    The baseline is an *innings average*: the measured 5.3% dismissal rate
    already contains every new batter, every collapse and every tail-ender,
    because those balls were in the data that produced it. So a layer that
    only ever raises the wicket chance for a new batter does not add
    information — it adds the new-batter effect a second time, on top of the
    average that already included it.

    The fix is the invariant ``engine/rating_duel.DUEL_MULTS`` already uses:
    the multipliers average to 1.0 over the balls they apply to. A new batter
    is more vulnerable *and* a set batter is correspondingly less so, and the
    innings total comes out where the anchor says it should. See
    :data:`_SETTLE_BANDS` for the bands and which row is solved.
    """
    c = (cfg or get_config())["conditions"]
    bound = float(c["bound"])
    strength = float(c.get("pressure_strength", 1.0))
    if strength <= 0.0:
        return dict(_ONE)
    bf = int(balls_faced or 0)
    runs, wkt = _SETTLE_BANDS[-1][2], _SETTLE_BANDS[-1][3]
    for upper, _share, r, w in _SETTLE_BANDS:
        if bf <= upper:
            runs, wkt = r, w
            break
    out = _run_shape(runs, wkt, bound, strength)

    # Wickets in hand. The baseline already has a wickets bucket as its own
    # hierarchy level, so this only carries the residual inside the deepest
    # bucket — the difference between seven down and nine down — and it is
    # neutralised the same way against the share of balls faced at 7+ down.
    if int(wickets or 0) >= 7:
        tail = _run_shape(0.94, 1.10, bound, strength)
    else:
        tail = _run_shape(_TAIL_NEUTRAL_RUNS, _TAIL_NEUTRAL_WICKET, bound,
                          strength)
    out = {k: out[k] * tail[k] for k in OUTCOMES}
    return {k: max(1.0 / bound, min(bound, v)) for k, v in out.items()}


# WHAT THE BATTER HAS SCORED, as opposed to how long they have been in.
#
# Shares are MEASURED: the observed fraction of deliveries faced by a batter
# on each score, over 7,174 simulated CIPL deliveries. Almost two thirds of
# all deliveries are faced by someone yet to reach 20.
#
# The band that matters is 75-99. With the settling curve alone this engine
# produced a correct TEAM total and a wrong distribution of it: conditional on
# reaching fifty, a batter went on to a hundred three times too often
# (100s/50s of 0.119 against a real 0.039). Balls faced could not fix that,
# because the 60+ ball band is 0.4% of deliveries — too rare to be the lever —
# and because it cannot tell a batter on 70 off 40 from one on 30 off 40. The
# first is playing shots that get people out; the second is not.
#
# `batter_runs` is live match state, read from the innings in progress and
# discarded with it. It is not a stored player attribute, not form, and not
# confidence: two cards with the same ratings on the same score get the same
# treatment. Mean-neutral like the settling curve, so the innings level is
# untouched.
# WICKET-ONLY, deliberately. An earlier version also raised the SCORING rate
# for a set batter and that version is instructive: it is a feedback loop.
# Scoring faster moves the batter into a higher band, which raises their
# scoring rate again, which moves them higher still. Mean-neutrality is
# computed against the MEASURED band shares, and the loop shifts those shares
# upward, so the invariant quietly stops holding — first-innings totals went
# from 183.5 to 187.9 and the centuries it was meant to curb did not move.
#
# Acceleration is already carried by the settling curve (which is keyed on
# balls faced, a quantity nothing in the model can inflate) and by the phase
# baseline. So this family does one job: it redistributes dismissal risk
# toward batters who have made runs, which is what caps a runaway individual
# innings without touching the team total.
_MILESTONE_BANDS = [
    # (upper bound on runs, measured share, runs mult, wicket mult)
    (19,           0.6195, 1.0, None),   # solved below
    (34,           0.1709, 1.0, 1.00),
    (49,           0.1006, 1.0, 1.05),
    (74,           0.0783, 1.0, 1.25),
    (99,           0.0219, 1.0, 1.90),
    (float("inf"), 0.0088, 1.0, 2.20),
]
_MILESTONE_BANDS = _solve_neutral_band(_MILESTONE_BANDS)


def milestone_multipliers(batter_runs: int = 0, cfg: dict = None) -> dict:
    """Risk and scoring by the batter's current score. Mean-neutral."""
    c = (cfg or get_config())["conditions"]
    bound = float(c.get("milestone_bound", 2.50))
    strength = float(c.get("milestone_strength",
                           c.get("pressure_strength", 1.0)))
    if strength <= 0.0:
        return dict(_ONE)
    runs = max(0, int(batter_runs or 0))
    r_mult, w_mult = _MILESTONE_BANDS[-1][2], _MILESTONE_BANDS[-1][3]
    for upper, _share, r, w in _MILESTONE_BANDS:
        if runs <= upper:
            r_mult, w_mult = r, w
            break
    return _run_shape(r_mult, w_mult, bound, strength)


def chase_multipliers(required_rate=None, par_rate=None,
                      balls_remaining: int = 0, wickets: int = 0,
                      cfg: dict = None) -> dict:
    """What the scoreboard demands of a chasing side, against its own par.

    This is the *only* place the realism engine looks at the target, and it
    looks at it as a required rate — a number the batting side would genuinely
    react to. It does not know, and cannot steer towards, a desired result.
    Compare ``engine/scenario_engine.py``, which scripts a finish: that layer
    is disabled in realism mode (§9).

    A chase band is also a baseline hierarchy level when ball-by-ball data was
    available to populate it, so this family stays modest.
    """
    c = (cfg or get_config())["conditions"]
    bound = float(c["bound"])
    strength = float(c.get("chase_strength", 1.0))
    if required_rate is None or strength <= 0.0:
        return dict(_ONE)
    rrr = float(required_rate)
    # "Par" is the rate the chase set out to score at, so ratio == 1.0 on the
    # first ball of any chase, large or small, and only moves as the side gets
    # ahead of or behind its own target. The obvious alternative — compare the
    # asking rate to the rate achieved so far — reads the first ball of a
    # 186-chase as already behind and then penalises the whole innings for it;
    # measured, that cost chases 14 percentage points of success.
    par = float(par_rate) if par_rate else None
    if not par or par <= 0:
        return dict(_ONE)
    ratio = rrr / par
    # The penalty for falling behind is deliberately milder than the obvious
    # choice. A chase that drops behind takes more risk, loses a wicket, and
    # is then further behind — so a strong behind-penalty compounds into a
    # spiral and chases stop succeeding. Measured at 1.14x/1.42x on the
    # wicket weight, chases came home 38.8% of the time against a real figure
    # near 50%. These values keep the direction and drop the spiral.
    if ratio <= 0.85:
        # Cruising: take fewer risks, milk it.
        out = _run_shape(0.94, 0.88, bound, strength)
    elif ratio <= 1.15:
        out = dict(_ONE)
    elif ratio <= 1.50:
        out = _run_shape(1.06, 1.08, bound, strength)
    else:
        # The ask has gone: swing from the hip, and get out doing it.
        out = _run_shape(1.14, 1.25, bound, strength)
    # Wickets in hand decide how freely a side can chase.
    if int(wickets or 0) >= 7 and ratio > 1.15:
        shield = _run_shape(0.94, 0.88, bound, strength)
        out = {k: out[k] * shield[k] for k in OUTCOMES}
    return {k: max(1.0 / bound, min(bound, v)) for k, v in out.items()}


def style_multipliers(matchup: str, cfg: dict = None) -> dict:
    """The handedness / pace-spin match-up.

    Deliberately small. Cricsheet has no line, length or turn, and published
    handedness splits are a point or two of strike rate, so anything larger
    would be invented detail dressed as data (§8 of the brief). ``spin_away``
    is the one effect large enough to be worth carrying, and it is carried at
    a fraction of the legacy engine's 1.15x wicket boost because it now sits
    on top of a fitted baseline rather than a hand-set matrix.
    """
    c = (cfg or get_config())["conditions"]
    bound = float(c["bound"])
    strength = float(c.get("style_strength", 1.0))
    if strength <= 0.0:
        return dict(_ONE)
    table = {
        "pace_same":  {"runs": 1.00, "wicket": 1.00},
        "pace_cross": {"runs": 0.99, "wicket": 1.04},
        "spin_into":  {"runs": 1.01, "wicket": 0.98},
        "spin_away":  {"runs": 0.98, "wicket": 1.06},
    }
    shape = table.get(matchup)
    if not shape or (shape["runs"] == 1.0 and shape["wicket"] == 1.0):
        return dict(_ONE)
    return _run_shape(shape["runs"], shape["wicket"], bound, strength)


def combine(families, total_bound: float) -> dict:
    """Multiply condition families together under a joint bound."""
    out = dict(_ONE)
    for fam in families:
        if not fam:
            continue
        for k in OUTCOMES:
            out[k] *= float(fam.get(k, 1.0))
    lo, hi = 1.0 / total_bound, total_bound
    return {k: max(lo, min(hi, v)) for k, v in out.items()}

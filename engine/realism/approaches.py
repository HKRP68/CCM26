"""Batting and bowling approaches as priced trade-offs.

Every approach keeps the name, emoji, label and button position it has today
(``engine.approach_modifiers.BATTING_APPROACHES`` / ``BOWLING_APPROACHES``
remain the single source of truth for those). What changes in realism mode is
what the numbers behind them do.

THE SHAPE OF THE PROBLEM
------------------------
The legacy tables are a hand-written grid: ``defensive`` scales Four by 0.45,
``ultra`` scales Six by 1.98, and so on. That grid has two failure modes the
module's own comments record. Separable tables collapse to one dominant answer
per side, which is why a counter matrix was bolted on; and because every cell
is set independently, nothing guarantees that buying runs costs wickets.

So realism mode does not use a grid. Each approach is a point on an explicit
**frontier**, and the frontier is monotone: moving along it buys scoring and
pays for it in dismissal risk, by construction rather than by inspection. An
approach is three numbers —

    runs            expected runs off the bat, relative
    boundary_share  how much of that comes in fours and sixes
    wicket          dismissal probability, relative

— and ``engine.realism.tilt`` turns the first two into per-outcome
multipliers by max-entropy tilting of the *baseline* run shape. That is what
makes "rotate the strike" expressible at all: more singles, fewer boundaries,
a similar run rate is a two-parameter move and a single scaling cannot do it.

MEAN-NEUTRALITY
---------------
The multipliers are normalised so the **mix-weighted mean is 1.0**, where the
mix is how often captains actually pick each approach
(``tools.pitch_calibration.REAL_BAT_MIX`` / ``REAL_BOWL_MIX``, read off
``/pitchstats``).

This matters and is easy to get wrong. The baseline distribution is measured
from real cricket, in which most captains attack most of the time. Treating
``balanced`` as the neutral point would therefore add the attacking premium a
second time, on top of a baseline that already contains it, and every innings
total would drift up with the mix the simulation happened to play. Normalising
on the mix means the innings lands on its measured anchor whatever the
captains choose, and the approach layer only decides how the runs are
distributed and what they cost. It is the same invariant the batter-state
families in ``engine.realism.conditions`` use.

NO DOMINANT CHOICE
------------------
The frontier alone is deliberately *not* enough. A monotone frontier has no
dominant point only if the opponent's choice changes which point is best, so
:data:`COUNTERS` carries bounded interaction terms — the cricket in which the
same plan is a match-winner against one intent and a gift against another.
``tests/test_realism_approaches.py`` checks every one of the 25 pairings on
every pitch in every phase and fails if any approach is best against all five
of the other side's options.

Pure functions, no RNG.
"""

import math

from engine.realism import tilt
from engine.realism.config import get_config

OUTCOMES = ("Dot", "Single", "Double", "Three", "Four", "Six", "Wicket",
            "Extras")

# How often captains actually pick each approach, from /pitchstats. Identical
# to tools.pitch_calibration.REAL_BAT_MIX / REAL_BOWL_MIX; duplicated here
# rather than imported because this module is on the delivery path and must
# not reach into tools/.
BAT_MIX = {"ultra": 32.0, "aggressive": 22.0, "balanced": 18.0,
           "rotate": 18.0, "defensive": 7.0}
BOWL_MIX = {"variation": 33.0, "mixed": 29.0, "aggressive": 20.0,
            "defensive": 11.0, "balanced": 7.0}

# ── The batting frontier ─────────────────────────────────────────────
# (runs, boundary_share, wicket), all relative and all pre-normalisation.
#
# The endpoints are the cricket, not round numbers. A batter playing purely
# for survival scores at roughly 0.63 of the par rate and is dismissed at
# roughly 0.60 of the par rate; all-out attack scores at about 1.42 and is
# dismissed at about 1.79. Those are the two ends of what a T20 batter can
# choose between, and the middle three interpolate geometrically along it.
#
# The boundary-share column is what separates two approaches that sit at a
# similar point on the runs/wicket frontier. Rotate and Defensive both score
# below par; Rotate does it off singles and twos and Defensive does it off
# almost nothing, and only the boundary share tells them apart.
BATTING_FRONTIER = {
    "defensive":  {"runs": 0.63, "boundary_share": 0.40, "wicket": 0.60},
    "rotate":     {"runs": 0.77, "boundary_share": 0.50, "wicket": 0.79},
    "balanced":   {"runs": 0.95, "boundary_share": 1.00, "wicket": 1.04},
    "aggressive": {"runs": 1.16, "boundary_share": 1.50, "wicket": 1.36},
    "ultra":      {"runs": 1.42, "boundary_share": 1.95, "wicket": 1.79},
}

# ── The bowling frontier ─────────────────────────────────────────────
# (runs conceded, boundary_share conceded, wicket), relative.
#
# The trade-off axis is **runs conceded against wickets taken**, and it is
# monotone: every plan that takes more wickets than Balanced also concedes
# more runs than Balanced. That ordering is asserted at import, because the
# first version of this table got it wrong in a way that is easy to miss —
# Variation was set to concede *fewer* runs and take *more* wickets than
# Balanced, which made Balanced strictly dominated. The calibration tool
# caught it immediately (``balanced`` was the best reply in 0 of 600 cells:
# a button nobody should ever press) but no amount of reading the table would
# have.
#
# The boundary_share column is the second dimension, and it is what makes
# these five genuinely different rather than five points on one line: it says
# *how* the runs are conceded. Defensive, Mixed and Variation all give up
# singles and deny the big hit; Aggressive gives up fours.
#
# The magnitudes here are deliberately modest, and that is a finding rather
# than a choice. At realistic sizes — a containment plan saving a tenth of the
# runs for a sixth of the wickets, an attacking one buying a quarter more
# wickets for a seventh more runs — the five plans sit very nearly on a
# straight line, and a captain maximising a linear value will always pick one
# of its ends. Which is exactly how bowling works: the plan is chosen for the
# batter in front of you, not off a chart. So the frontier sets the honest
# magnitudes and :data:`COUNTERS` does the deciding.
BOWLING_FRONTIER = {
    # Tight lines, boundary denial, fields back. Concedes the single.
    "defensive":  {"runs": 0.90, "boundary_share": 0.74, "wicket": 0.84},
    "balanced":   {"runs": 1.00, "boundary_share": 1.00, "wicket": 1.00},
    # Keep the batter guessing. Smothers the big hit; the batter who reads it
    # still milks a single, so it is not free.
    "mixed":      {"runs": 1.04, "boundary_share": 0.84, "wicket": 1.08},
    # Pace off, slower balls, cutters. Hardest to get under and easiest to
    # misread — and the hardest to execute, so it leaks when it misses.
    "variation":  {"runs": 1.06, "boundary_share": 0.82, "wicket": 1.12},
    # At the stumps, catchers in. Wickets, and the bad ball goes for four.
    "aggressive": {"runs": 1.15, "boundary_share": 1.30, "wicket": 1.30},
}

# ── Counters ─────────────────────────────────────────────────────────
# Bounded interaction terms, applied on top of both frontiers. Each line is
# the cricket it describes, and the magnitudes are small on purpose: these
# exist to stop a dominant choice, not to decide matches on their own. The
# tests check the *consequence* (nobody wins every column) rather than any
# individual number here.
#
# Keys are (batting approach, bowling approach) → relative multipliers on
# runs / boundary_share / wicket, in the same units as the frontiers.
COUNTERS = {
    # Change of pace is the answer to a slogger — and only to a slogger.
    ("ultra", "variation"):       {"runs": 0.88, "boundary_share": 0.80, "wicket": 1.22},
    ("aggressive", "variation"):  {"runs": 0.93, "boundary_share": 0.88, "wicket": 1.14},
    ("ultra", "mixed"):           {"runs": 0.92, "boundary_share": 0.86, "wicket": 1.14},
    ("aggressive", "mixed"):      {"runs": 0.96, "boundary_share": 0.93, "wicket": 1.10},
    # Pace on a length to someone swinging through the line is a gift.
    ("ultra", "aggressive"):      {"runs": 1.10, "boundary_share": 1.10, "wicket": 0.94},
    ("aggressive", "aggressive"): {"runs": 1.08, "boundary_share": 1.08, "wicket": 0.96},
    # Attacking fields at a batter with no intent of scoring: all upside.
    ("defensive", "aggressive"):  {"runs": 0.94, "boundary_share": 0.90, "wicket": 1.34},
    ("rotate", "aggressive"):     {"runs": 0.97, "boundary_share": 0.92, "wicket": 1.20},
    # Boundary denial concedes the single, so milking it beats it.
    ("rotate", "defensive"):      {"runs": 1.12, "boundary_share": 0.88, "wicket": 0.94},
    ("balanced", "defensive"):    {"runs": 1.06, "boundary_share": 0.94, "wicket": 0.96},
    ("aggressive", "defensive"):  {"runs": 1.06, "boundary_share": 1.10, "wicket": 1.00},
    ("ultra", "defensive"):       {"runs": 1.08, "boundary_share": 1.14, "wicket": 1.02},
    # Two captains both refusing any risk: nobody scores, nobody gets out.
    ("defensive", "defensive"):   {"runs": 0.94, "boundary_share": 0.94, "wicket": 0.86},
    # The stock plan against a batter playing on merit. There is no pattern to
    # read and no premeditation to punish, so the contest is pure execution
    # and the side with control wins it. Without this line Balanced bowling was
    # the best reply in 0 of 600 measured cells — a dominated button — because
    # every other plan had a match-up it was built for and the stock plan had
    # none. Its match-up is the absence of one.
    ("balanced", "balanced"):     {"runs": 0.93, "boundary_share": 0.95, "wicket": 1.09},
    ("defensive", "balanced"):    {"runs": 0.96, "boundary_share": 0.96, "wicket": 1.07},
    # Mixed and Variation are built to strangle the single — which is exactly
    # what the strike-rotator needs, and nothing the blocker can use.
    ("rotate", "mixed"):          {"runs": 0.90, "boundary_share": 0.96, "wicket": 1.10},
    ("rotate", "variation"):      {"runs": 0.94, "boundary_share": 0.98, "wicket": 1.04},
    ("balanced", "mixed"):        {"runs": 0.96, "boundary_share": 0.92, "wicket": 1.12},
    ("defensive", "mixed"):       {"runs": 1.00, "boundary_share": 1.00, "wicket": 1.06},
    ("defensive", "variation"):   {"runs": 1.00, "boundary_share": 1.00, "wicket": 1.07},
}

_AXES = ("runs", "boundary_share", "wicket")


def _validate():
    """Structural checks, at import, for the things a comment cannot enforce."""
    for name, table, mix in (("batting", BATTING_FRONTIER, BAT_MIX),
                             ("bowling", BOWLING_FRONTIER, BOWL_MIX)):
        assert set(table) == set(mix), (
            "%s frontier and mix must cover the same approaches" % name)
        for key, row in table.items():
            assert set(row) == set(_AXES), "%s[%r] axes" % (name, key)
            for axis, v in row.items():
                assert 0.2 <= float(v) <= 3.0, "%s[%r][%r]=%r" % (name, key, axis, v)

    # Requirement: every batting approach is a trade-off. Scoring more must
    # cost dismissal risk, monotonically along the intent ladder.
    ladder = ("defensive", "rotate", "balanced", "aggressive", "ultra")
    for a, b in zip(ladder, ladder[1:]):
        for axis in ("runs", "boundary_share", "wicket"):
            assert BATTING_FRONTIER[b][axis] > BATTING_FRONTIER[a][axis], (
                "batting frontier must increase in %s from %s to %s"
                % (axis, a, b))

    # Requirement: bowling is a trade-off too. No plan may concede fewer runs
    # than Balanced AND take more wickets — that plan is the only correct
    # answer and the other four are decoration. Checked as a strict pairwise
    # dominance test over all five plans, not just against Balanced, because
    # a plan dominated by any other plan is equally dead.
    for a, b in ((x, y) for x in BOWLING_FRONTIER for y in BOWLING_FRONTIER
                 if x != y):
        ra, rb = BOWLING_FRONTIER[a], BOWLING_FRONTIER[b]
        assert not (ra["runs"] <= rb["runs"] and ra["wicket"] >= rb["wicket"]
                    and (ra["runs"] < rb["runs"] or ra["wicket"] > rb["wicket"])), (
            "bowling plan %r dominates %r: it concedes no more and takes no "
            "fewer wickets, so %r would never be worth picking" % (a, b, b))

    assert set(COUNTERS) <= {(b, w) for b in BATTING_FRONTIER
                             for w in BOWLING_FRONTIER}, \
        "COUNTERS is keyed on an approach pair that does not exist"
    for key, row in COUNTERS.items():
        assert set(row) <= set(_AXES), "COUNTERS[%r] axes" % (key,)
        for axis, v in row.items():
            assert 0.7 <= float(v) <= 1.45, (
                "COUNTERS[%r][%r]=%r is outside the bounded range; counters "
                "break ties, they do not decide matches" % (key, axis, v))


_validate()


def _mix_normalise(table, mix):
    """Scale each axis so the mix-weighted mean is exactly 1.0."""
    total = sum(mix.values()) or 1.0
    shares = {k: v / total for k, v in mix.items()}
    means = {axis: sum(shares[k] * table[k][axis] for k in table)
             for axis in _AXES}
    return {k: {axis: table[k][axis] / (means[axis] or 1.0) for axis in _AXES}
            for k in table}


BATTING_NORMALISED = _mix_normalise(BATTING_FRONTIER, BAT_MIX)
BOWLING_NORMALISED = _mix_normalise(BOWLING_FRONTIER, BOWL_MIX)


def targets(batting_approach, bowling_approach, cfg=None):
    """The (runs, boundary_share, wicket) this pairing is aiming at.

    Relative to the baseline, after mix-normalisation, counters and the
    configured strength dial. Exposed because the AI captain's payoff matrix
    and the calibration tool both need the same numbers the delivery uses.
    """
    c = (cfg or get_config())["conditions"]
    strength = float(c.get("approach_strength", 1.0))
    bat = _norm_bat(batting_approach)
    bowl = _norm_bowl(bowling_approach)
    out = {}
    counter = COUNTERS.get((bat, bowl)) or {}
    for axis in _AXES:
        raw = (BATTING_NORMALISED[bat][axis] * BOWLING_NORMALISED[bowl][axis]
               * float(counter.get(axis, 1.0)))
        # Strength is applied in log space so 0 is a clean no-op and the
        # trade-off structure is preserved at any dial setting.
        out[axis] = math.exp(math.log(max(1e-9, raw)) * strength)
    return out


def _norm_bat(approach):
    a = str(approach or "").strip().lower()
    return a if a in BATTING_FRONTIER else "balanced"


def _norm_bowl(approach):
    a = str(approach or "").strip().lower()
    return a if a in BOWLING_FRONTIER else "balanced"


# ── The residual mix correction ──────────────────────────────────────
# Normalising the frontier tables makes their mix-weighted mean 1.0, but the
# multipliers the engine actually applies are not the tables: they come out of
# a nonlinear tilt and then pick up the counters, and neither of those
# preserves the mean. Measured, the 25-cell mix-weighted mean came out 3.3%
# low on runs and 7.4% high on wickets — small, but it would move every
# innings total and dismissal rate off the anchor the baseline was fitted to.
#
# So the residual is measured and divided out, per baseline node, at runtime.
# It is NOT a hand-set constant and NOT a config value, deliberately: it is a
# pure function of the baseline and the tables, so computing it where it is
# used means it cannot go stale when either changes. Twenty-five cheap
# evaluations per distinct node, cached, and the whole reference has 86 nodes.
_MIX_CACHE = {}
_MIX_CACHE_MAX = 256


def _baseline_key(baseline):
    return tuple(round(float(baseline.get(o, 0.0)), 6) for o in OUTCOMES)


def mix_correction(baseline, cfg=None):
    """``{"runs": f, "wicket": f}`` dividing out the residual mix drift."""
    key = _baseline_key(baseline)
    hit = _MIX_CACHE.get(key)
    if hit is not None:
        return hit
    bt = sum(BAT_MIX.values()) or 1.0
    wt = sum(BOWL_MIX.values()) or 1.0
    # Measure the reference the SAME way the cells are measured. The tilt's
    # own mean is conditional on the ball being a run outcome, while a cell's
    # expected runs is unconditional over all eight — they differ by the
    # wicket and extras mass, about 9%, and comparing one to the other put the
    # correction 10 points out and overshot every innings total.
    unit = {o: 1.0 for o in OUTCOMES}
    reference = _from_multipliers(unit, baseline)
    base_runs = reference["runs"]
    base_wkt = reference["wicket"]
    runs = wkt = 0.0
    for b, bshare in BAT_MIX.items():
        for w, wshare in BOWL_MIX.items():
            p = (bshare / bt) * (wshare / wt)
            e = _expected_raw(b, w, baseline, None, cfg)
            runs += p * e["runs"]
            wkt += p * e["wicket"]
    out = {
        "runs": (base_runs / runs) if runs > 0 else 1.0,
        "wicket": (base_wkt / wkt) if wkt > 0 else 1.0,
    }
    if len(_MIX_CACHE) >= _MIX_CACHE_MAX:
        _MIX_CACHE.clear()
    _MIX_CACHE[key] = out
    return out


def clear_cache():
    _MIX_CACHE.clear()


def multipliers(batting_approach, bowling_approach, baseline,
                bowler_rating=None, cfg=None, explain=False):
    """Per-outcome multipliers for this pairing against *baseline*.

    *baseline* is the smoothed reference distribution for the delivery's
    context, which is what makes the result context-aware without a phase
    table: tilting a death-overs shape toward "more boundaries" moves
    different mass than tilting a powerplay shape by the same amount.
    """
    c = (cfg or get_config())["conditions"]
    bound = float(c.get("approach_bound", 2.60))
    if float(c.get("approach_strength", 1.0)) <= 0.0:
        return {o: 1.0 for o in OUTCOMES}

    want = dict(targets(batting_approach, bowling_approach, cfg))
    fix = mix_correction(baseline, cfg)
    want["runs"] *= fix["runs"]
    want["wicket"] *= fix["wicket"]
    prior = {k: float(baseline.get(k, 0.0)) for k in tilt.RUN_OUTCOMES}
    base_mean = tilt.mean_runs(tilt.tilted(prior))
    base_boundary = tilt.boundary_share(tilt.tilted(prior))

    run_mults, info = tilt.multipliers_for(
        prior,
        base_mean * want["runs"],
        min(0.92, base_boundary * want["boundary_share"]))

    out = {o: 1.0 for o in OUTCOMES}
    out.update(run_mults)
    out["Wicket"] = want["wicket"]
    # Mixed bowling's rating-scaled six suppression is a real effect the
    # legacy engine models and the frontier cannot: a top bowler's slower ball
    # is far harder to get under than a weak bowler's. Kept, bounded, and
    # applied to sixes only.
    if _norm_bowl(bowling_approach) == "mixed":
        out["Six"] *= _mixed_six_mult(bowler_rating)
    out = {o: max(1.0 / bound, min(bound, v)) for o, v in out.items()}
    if explain:
        return out, dict(info, targets=want)
    return out


def _mixed_six_mult(bowler_rating):
    try:
        r = float(bowler_rating)
    except (TypeError, ValueError):
        return 0.88
    # Flatter than the legacy 0.55/0.70/0.90 ladder, because the frontier's
    # boundary_share term already suppresses the big hit for this plan and
    # stacking both was a double count.
    if r >= 80:
        return 0.80
    if r >= 65:
        return 0.88
    return 0.97


def _from_multipliers(mult, baseline):
    weights = {o: max(0.0, float(baseline.get(o, 0.0)) * mult.get(o, 1.0))
               for o in OUTCOMES}
    total = sum(weights.values()) or 1.0
    dist = {o: w / total for o, w in weights.items()}
    return {
        "runs": sum(tilt.RUN_VALUE[o] * dist[o] for o in tilt.RUN_OUTCOMES)
                + dist.get("Extras", 0.0),
        "wicket": dist.get("Wicket", 0.0),
        "boundary": dist.get("Four", 0.0) + dist.get("Six", 0.0),
        "dot": dist.get("Dot", 0.0),
        "dist": dist,
    }


def _expected_raw(batting_approach, bowling_approach, baseline, bowler_rating,
                  cfg):
    """`expected` without the mix correction — what the correction measures."""
    c = (cfg or get_config())["conditions"]
    bound = float(c.get("approach_bound", 2.60))
    want = targets(batting_approach, bowling_approach, cfg)
    prior = {k: float(baseline.get(k, 0.0)) for k in tilt.RUN_OUTCOMES}
    base_mean = tilt.mean_runs(tilt.tilted(prior))
    base_boundary = tilt.boundary_share(tilt.tilted(prior))
    run_mults, _info = tilt.multipliers_for(
        prior, base_mean * want["runs"],
        min(0.92, base_boundary * want["boundary_share"]))
    mult = {o: 1.0 for o in OUTCOMES}
    mult.update(run_mults)
    mult["Wicket"] = want["wicket"]
    if _norm_bowl(bowling_approach) == "mixed":
        mult["Six"] *= _mixed_six_mult(bowler_rating)
    mult = {o: max(1.0 / bound, min(bound, v)) for o, v in mult.items()}
    return _from_multipliers(mult, baseline)


def expected(batting_approach, bowling_approach, baseline, bowler_rating=None,
             cfg=None):
    """Expected runs and wicket probability for one delivery of this pairing.

    The payoff the AI captain reasons about, and what the dominance tests
    rank. Includes extras at their baseline rate because a captain's choice
    does not change how often the bowler sprays one.
    """
    return _from_multipliers(
        multipliers(batting_approach, bowling_approach, baseline,
                    bowler_rating, cfg),
        baseline)

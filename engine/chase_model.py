"""Calibrated win-probability model for a chase.

One model owns the chase: it steers the closing overs and supplies the live
win% for the whole second innings.

Two things it replaced, both measured rather than assumed.

**The closing overs were not cricket.** They used to be steered by hand-tuned
multiplier hooks that were never checked against an outcome distribution. An
elite pair needing 24 off 6 won a third of the time and needing 18 off 6 won
two thirds — real T20 is about 5% and 20% — while a tailender needing 5 off 6
won only 70% against a real ~87%. /letsplay and Challenge League also disagreed
with each other on identical situations, because only one of them ran the
clutch amplifier. The thing the old hooks got backwards, and the single biggest
correction here: **aggression costs dot balls and wickets.** A batter swinging
for six misses more often, so intent raises the Dot weight along with the Six.
The clutch hook raised boundaries while *lowering* dots, which is free runs.

**The old runs x wickets matrix could not see the clock.** ``engine.chase_chance``
keys on runs needed and wickets lost and only feels the balls left through a
feasibility scale, so it flattened at 86% for any ask up to 15 runs however many
balls remained (it read 20 off 6 as better than even money) and collapsed every
ask above 51 runs into one row (a side chasing 161 with ten wickets standing
read as a 20% underdog, and stayed pinned there for fifteen overs). This surface
is keyed on balls left throughout, so it answers both ends of the innings.

Public API
----------
``target_probabilities(runs_needed, balls_left, **factors) -> dict``
    The authority: ``{"win", "tie", "lose"}`` in percent, for the batting side.

``make_chase_hook(inputs, free_hit=False, window=None) -> callable``
    A ``weight_hook`` for ``engine.ball_outcome.calculate_outcome``. It sees the
    fully-composed natural weights (ratings, traits, pitch, conditions, approach
    and pressure are already in them) and tilts them until THIS delivery's
    one-ball outlook against the model equals what the model asked for.

``PAR_TABLE`` / ``TIE_TABLE``
    ``[edge_bucket][balls_left][runs_needed] -> percent``, spanning the whole
    second innings. Inducted on first use, not at import (see ``tables()``).

How the surface is built
------------------------
The hand-written anchors (``WIN_ANCHORS_6``) calibrate the FINAL OVER and
nothing else. Every longer horizon comes out of the backward induction on its
own, which is why the table is generated rather than typed: against twenty real
reference points from 6 to 114 balls it lands within about 3 points RMS. Two
things had to be right for that to hold — the induction has to price a batting
side thinning out (``WICKET_DEGRADE``), or a full-innings chase reads as 83%
instead of ~50%, and the anchor correction must NOT be carried forward at the
same required rate, because a short horizon gets to be lucky and a long one has
to be good (carrying it made 24 off 12 read as 55% against a real ~33%).

Why one ball and not the whole over
-----------------------------------
The controller steers a single delivery and lets the model carry the rest,
rather than rolling the whole over forward under the current tilt. That is not
a shortcut, it is the thing that makes the loop converge. Every later ball will
itself be re-steered onto the model, so a delivery that plans against a frozen
tilt is planning against a future that will not happen: a suppressed ball
expects a suppressed continuation, the next ball lifts it back to target, and
the over finishes well clear of the number it was aimed at. Solving one ball
against the model's own continuation makes the realised rate equal the target
by induction, at every ball and therefore over the whole over.

Nothing here draws from ``random``. A single extra draw would shift the stream
for every seeded test in the suite.
"""

import math
import os

# ── The eight outcomes the ball engine samples over ───────────────────────
# Same keys, same run values as engine.ball_outcome.calculate_outcome.
OUTCOMES = ("Dot", "Single", "Double", "Three", "Four", "Six", "Wicket", "Extras")
OUTCOME_RUNS = {"Dot": 0, "Single": 1, "Double": 2, "Three": 3,
                "Four": 4, "Six": 6, "Wicket": 0, "Extras": 1}

# Mirrors the safety valve at engine/ball_outcome.py, which runs *after* the
# weight hook. The DP has to price the post-cap distribution or it would plan
# against wicket weights the draw is never going to see.
WICKET_SHARE_CAP = 0.12

# Share of Extras that are wides / no-balls, which score a run without
# consuming a ball. Matches the extra-type weights in
# engine.ball_outcome.calculate_outcome (Wide 0.40 + No Ball 0.25); the rest are
# byes / leg-byes, which are legal deliveries.
EXTRA_FREE_BALL_SHARE = 0.65

# Runs axis of the tables. Beyond this the chance decays geometrically.
# 220 covers any total this format can set.
MAX_RUNS = 220
TAIL_DECAY = 0.72
FLOOR_PCT = 0.1

# The table spans the whole second innings: it steers the death (see
# CHASE_MODEL_BALLS in services.cipl_match) and supplies the live win% for
# every over before that.
MAX_BALLS = 120

# The horizon the hand-written anchors are defined at, which is NOT the size of
# the table. Everything else is generated from them.
ANCHOR_BALLS = 6

# The most that can come off a ball is a six, and off the last ball a no-ball
# six. Past that the chase is not unlikely, it is arithmetically gone — the same
# "gone, not unlikely" line engine.chase_chance.apply_feasibility draws.
def max_possible(balls_left):
    return 6 * max(0, int(balls_left)) + 1


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _logit(p):
    p = _clamp(float(p), 1e-4, 1.0 - 1e-4)
    return math.log(p / (1.0 - p))


def _sigmoid(x):
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


# ══════════════════════════════════════════════════════════════════════
# 1. The spec — hand-written balls_left=6 anchors
# ══════════════════════════════════════════════════════════════════════
# Win% for the batting side needing 1..30 off the final over with wickets in
# hand, for three reference contests. These three rows ARE the specification;
# everything else in this module exists to reproduce them and to interpolate
# sensibly around them. Tuned against IPL / T20I last-over history.
#
#   edge = effective batting rating - bowler rating
#     +15  ~ a 95-rated pair against an 80-rated death bowler
#       0  ~ rating parity at 75
#     -25  ~ a 60-rated pair against an 85-rated death bowler

ANCHOR_EDGES = (15, 0, -25)

WIN_ANCHORS_6 = {
    15: [99.5, 99.0, 98.5, 98.0, 98.0, 98.0, 93.0, 88.0, 85.0, 81.0,
         76.0, 71.0, 56.0, 49.0, 42.0, 35.0, 30.0, 25.0, 20.0, 16.0,
         13.0, 11.0, 8.5, 6.5, 5.0, 4.0, 3.5, 3.0, 2.0, 1.5],
    0:  [98.5, 97.5, 96.0, 95.0, 94.0, 94.0, 88.0, 83.0, 78.0, 72.0,
         66.0, 60.0, 50.0, 43.0, 36.0, 30.0, 25.0, 20.0, 16.0, 13.0,
         10.0, 8.0, 6.5, 5.0, 4.0, 3.0, 2.5, 2.0, 1.3, 1.0],
    -25: [96.0, 93.0, 90.0, 88.0, 87.0, 87.0, 76.0, 66.0, 60.0, 52.0,
          46.0, 40.0, 32.0, 26.0, 20.0, 18.0, 14.0, 11.0, 8.0, 6.0,
          4.0, 3.0, 2.0, 1.5, 1.2, 1.0, 0.8, 0.5, 0.4, 0.3],
}

# Tie% (which becomes a Super Over) for the same rows, by runs-needed band.
#
# These are conditional on a LIVE last over, which is a much narrower and more
# tie-prone population than "all T20 matches". The often-quoted 2-3% is the
# unconditional figure; conditioned on a side arriving at the 20th with a real
# ask, finishing exactly one short is common, because the ask frequently comes
# down to two or three off the final ball and the single that ties it is the
# shot most likely to come off. The curve therefore peaks where the contest is
# closest and falls away at both ends, where the over is decided early.
_TIE_BANDS = {
    15: ((3, 1.0), (6, 1.5), (12, 4.0), (15, 5.5), (20, 5.5), (28, 2.5), (99, 0.5)),
    0: ((3, 1.5), (6, 2.5), (12, 5.0), (15, 7.5), (20, 5.0), (28, 2.0), (99, 0.4)),
    -25: ((3, 2.5), (6, 4.0), (12, 7.0), (15, 7.0), (20, 3.5), (28, 1.0), (99, 0.2)),
}

# Edge buckets the table is actually stored at. The two outer ones are
# extrapolated in logit space from the nearest anchored pair, so a 99-rated
# pair against a part-timer and a rabbit against prime Bumrah both have
# somewhere to land instead of clipping to an anchor.
TABLE_EDGES = (-40, -25, 0, 15, 30)


def _extrapolated_anchor_row(near, far, edge):
    """A 6-ball anchor row for an edge bucket nobody hand-wrote, in logit space.

    The outer two buckets used to be produced by extrapolating the FINISHED
    surfaces, which meant their 6-ball column came from anchored values and
    their 7-ball column from raw ones. Extrapolation amplified the difference
    between the two constructions and opened a 26-point step across the seam in
    the +30 bucket. Extrapolating the anchor row instead means all five buckets
    go through exactly the same build, so the join behaves the same in each.
    """
    span = (edge - near) / float(near - far)
    row = []
    for a, b in zip(WIN_ANCHORS_6[near], WIN_ANCHORS_6[far]):
        la, lb = _logit(a / 100.0), _logit(b / 100.0)
        row.append(_clamp(_sigmoid(la + (la - lb) * span) * 100.0, FLOOR_PCT, 99.9))
    for i in range(1, len(row)):          # more to get is never easier
        row[i] = min(row[i], row[i - 1])
    return row


def _extrapolated_tie_bands(near, far, edge):
    span = (edge - near) / float(near - far)
    return tuple((hi, _clamp(pct + (pct - _TIE_BANDS[far][i][1]) * span, 0.05, 12.0))
                 for i, (hi, pct) in enumerate(_TIE_BANDS[near]))


WIN_ANCHORS_6[30] = _extrapolated_anchor_row(15, 0, 30)
WIN_ANCHORS_6[-40] = _extrapolated_anchor_row(-25, 0, -40)
_TIE_BANDS[30] = _extrapolated_tie_bands(15, 0, 30)
_TIE_BANDS[-40] = _extrapolated_tie_bands(-25, 0, -40)


def _tie_anchor(edge, runs):
    for hi, pct in _TIE_BANDS[edge]:
        if runs <= hi:
            return pct
    return _TIE_BANDS[edge][-1][1]


def _win_anchor(edge, runs):
    row = WIN_ANCHORS_6[edge]
    if runs <= len(row):
        return row[runs - 1]
    return max(FLOOR_PCT, row[-1] * (TAIL_DECAY ** (runs - len(row))))


# ══════════════════════════════════════════════════════════════════════
# 2. Per-ball distribution: reference, intent, execution
# ══════════════════════════════════════════════════════════════════════
# A par death-over delivery. Only used to GENERATE the table — the live
# controller always works from the real weights the engine hands it.
_REFERENCE_BALL = {
    "Dot": 0.310, "Single": 0.280, "Double": 0.110, "Three": 0.012,
    "Four": 0.115, "Six": 0.085, "Wicket": 0.058, "Extras": 0.030,
}

# ── Intent: how hard the batting side is swinging ────────────────────────
# A function of the ask alone, not of who is batting. This is the realism fix:
# going for it buys boundaries with DOT BALLS and wickets. Negative intent is a
# side cruising home — milking singles, protecting wickets, no need to slog.
INTENT_K = {"Six": 0.80, "Four": 0.45, "Dot": 0.40, "Wicket": 0.55,
            "Single": -0.55, "Double": -0.25, "Three": -0.15, "Extras": 0.0}
INTENT_MIN, INTENT_MAX = -0.35, 1.0

# ── Execution: who is winning the contest ────────────────────────────────
# The solved correction. Strictly monotone in win probability by construction
# (every batting-good outcome moves one way, every bowling-good outcome the
# other), which is what lets the controller bisect on it.
EXEC_K = {"Six": 0.70, "Four": 0.50, "Dot": -0.45, "Wicket": -0.85,
          "Single": 0.20, "Double": 0.15, "Three": 0.05, "Extras": 0.0}
EXEC_LIMIT = 2.6

# What bounds the controller is not the search range but this: no single
# outcome's weight may move by more than these factors, whatever the solver
# asks for. The engine's natural death-overs weights are harsh on a low-rated
# pair and generous to an elite one relative to real last-over results, so the
# correction genuinely needs room — but a delivery whose six weight has been
# multiplied by five is no longer the game underneath it.
EXEC_MULT_FLOOR = 0.25
EXEC_MULT_CEIL = 3.4


def intent_for(runs_needed, balls_left):
    """Chase intent 0..1 (negative when cruising) from the required rate."""
    if balls_left <= 0:
        return INTENT_MAX
    rpb = max(0.0, float(runs_needed)) / balls_left
    return _clamp((rpb - 0.9) / 1.6, INTENT_MIN, INTENT_MAX)


def _exec_multiplier(o, execution):
    return _clamp(math.exp(EXEC_K[o] * execution), EXEC_MULT_FLOOR, EXEC_MULT_CEIL)


def tilt(dist, intent=0.0, execution=0.0, free_hit=False):
    """Apply the intent and execution tilts to a per-ball distribution.

    Returns a NORMALISED distribution with the engine's wicket-share cap
    already applied, so it is exactly what the draw will see.
    """
    out = {}
    for o in OUTCOMES:
        w = float(dist.get(o, 0.0) or 0.0)
        if w <= 0.0:
            out[o] = 0.0
            continue
        out[o] = w * math.exp(INTENT_K[o] * intent) * _exec_multiplier(o, execution)
    return _normalise_capped(out, free_hit=free_hit)


def _normalise_capped(weights, free_hit=False):
    """Normalise to probabilities, applying the same 12% wicket-share cap the
    ball engine applies immediately after the weight hook — including the
    engine's exemption for a free hit, where only a run-out is possible and the
    wicket weight has already been crushed upstream."""
    total = sum(weights.values())
    if total <= 0.0:
        return {o: (1.0 if o == "Dot" else 0.0) for o in OUTCOMES}
    w = dict(weights)
    other = total - w.get("Wicket", 0.0)
    if not free_hit and other > 0.0:
        cap = (WICKET_SHARE_CAP / (1.0 - WICKET_SHARE_CAP)) * other
        if w.get("Wicket", 0.0) > cap:
            w["Wicket"] = cap
            total = sum(w.values())
    return {o: w.get(o, 0.0) / total for o in OUTCOMES}


# ══════════════════════════════════════════════════════════════════════
# 3. Backward induction over the rest of the over
# ══════════════════════════════════════════════════════════════════════
# State is (balls left, runs still needed to WIN, wickets in hand). An innings
# that ends with runs_needed == 1 is a TIE — the scores are level — which is
# how the Super Over rate falls out of the same model as the win rate.
#
# Simplification worth naming: one distribution is used for the whole over even
# though the strike rotates. The controller only ever receives the *current*
# striker's weights, and it re-solves every ball, so the partner enters through
# ``effective_batting`` in the target rather than through the DP.

MAX_DP_WICKETS = 10

# How much worse the batting gets as it thins out, as an execution tilt, and
# the point at which it starts to bite. Losing your third wicket barely changes
# how a side bats; losing your eighth changes everything, so this is flat until
# DEGRADE_FROM and steep after. Without it the surface bats identically two
# down and eight down — invisible over six balls, and badly wrong over a full
# innings, where nobody ever runs out of partners and a chase of 160 read as
# 83% instead of ~50%.
WICKET_DEGRADE = 0.70
DEGRADE_FROM = 5

# Scoring outcomes and their run values, pulled out of the DP's inner loop.
# Dot and Wicket are handled separately (they move a different axis) and
# Extras is split into its ball-consuming and ball-free branches.
_SCORING = tuple((o, OUTCOME_RUNS[o])
                 for o in ("Single", "Double", "Three", "Four", "Six"))
_SCORING_WITH_DOT = (("Dot", 0),) + _SCORING


def solve_over(dist_for, runs_needed, balls_left, wickets_in_hand):
    """P(win), P(tie) for the batting side over the rest of the over, under a
    distribution that is NOT re-steered ball to ball.

    This builds the table (see ``_raw_surface``); the live controller uses
    :func:`one_ball_outlook` instead, for the reason in the module docstring.

    ``dist_for(runs, balls)`` returns the per-ball distribution to use in that
    state, so the intent tilt can vary as the ask changes mid-over.

    Rolled forward one ball at a time over flat per-wicket rows rather than a
    three-dimensional table: the solver calls this eight or nine times per
    delivery, so the allocation is worth avoiding.
    """
    runs_needed = int(runs_needed)
    balls_left = int(balls_left)
    wkts = int(_clamp(wickets_in_hand, 0, MAX_DP_WICKETS))
    if runs_needed <= 0:
        return 1.0, 0.0
    if balls_left <= 0 or wkts <= 0:
        return (0.0, 1.0) if runs_needed == 1 else (0.0, 0.0)

    cap = min(runs_needed, MAX_RUNS)
    nr = cap + 1

    # The innings-over row, shared by "no balls left" and "no wickets left":
    # nothing more to get is a win, one short is a TIE (the scores are level,
    # which is where the Super Over rate comes from), anything else a loss.
    term_win = [1.0] + [0.0] * cap
    term_tie = [0.0] * nr
    term_tie[1] = 1.0

    prev_win = [term_win] * (wkts + 1)
    prev_tie = [term_tie] * (wkts + 1)

    for b in range(1, balls_left + 1):
        cur_win = [term_win]
        cur_tie = [term_tie]
        for w in range(1, wkts + 1):
            nw, nt = prev_win[w], prev_tie[w]          # ball gone, wicket intact
            lw, lt = prev_win[w - 1], prev_tie[w - 1]  # ball gone, wicket down
            row_w = [1.0] + [0.0] * cap
            row_t = [0.0] * nr
            for r in range(1, nr):
                d = dist_for(r, b)
                extras = d["Extras"]
                free = extras * EXTRA_FREE_BALL_SHARE
                # A bye / leg-bye is a legal ball; a wide or no-ball is not, so
                # its successor sits at the SAME ball count. Every extra scores,
                # so that self-loop always steps down the runs axis and resolves
                # against this row's already-final r-1 entry.
                legal_extra = extras - free
                p_dot = d["Dot"]
                pw = p_dot * nw[r] + d["Wicket"] * lw[r]
                pt = p_dot * nt[r] + d["Wicket"] * lt[r]
                for key, runs in _SCORING:
                    p = d[key]
                    if p:
                        i = r - runs
                        if i < 0:
                            i = 0
                        pw += p * nw[i]
                        pt += p * nt[i]
                if legal_extra:
                    i = r - 1
                    pw += legal_extra * nw[i]
                    pt += legal_extra * nt[i]
                if free:
                    pw += free * row_w[r - 1]
                    pt += free * row_t[r - 1]
                row_w[r] = pw
                row_t[r] = pt
            cur_win.append(row_w)
            cur_tie.append(row_t)
        prev_win, prev_tie = cur_win, cur_tie

    win = prev_win[wkts][cap]
    tie = prev_tie[wkts][cap]
    win = 0.0 if win < 0.0 else (1.0 if win > 1.0 else win)
    hi = 1.0 - win
    tie = 0.0 if tie < 0.0 else (hi if tie > hi else tie)
    return win, tie


# ══════════════════════════════════════════════════════════════════════
# 4. Build the table
# ══════════════════════════════════════════════════════════════════════

# The execution tilt each edge bucket's column is generated at, fitted so the
# RAW induction at the anchor horizon reproduces that bucket's anchor row.
#
# This used to be a hand-set linear map (edge / 25 * 0.62) spanning -0.99 to
# +0.74. The fit is far flatter — the induction's sensitivity to the rating edge
# was about 70% stronger than the anchors actually call for. Below the anchor
# horizon the correction hid that, but above it the raw sensitivity took over
# and opened a 25-point step across the seam in the outer buckets. Fitting the
# map means the correction has almost nothing left to do, which is the point:
# the two halves of the surface now agree on what a rating edge is worth.
_EDGE_EXECUTION = {-40: -0.598, -25: -0.383, 0: 0.0, 15: 0.208, 30: 0.430}


def _edge_execution(edge):
    """The execution tilt used to generate this edge bucket's column."""
    if edge in _EDGE_EXECUTION:
        return _EDGE_EXECUTION[edge]
    keys = sorted(_EDGE_EXECUTION)
    e = _clamp(float(edge), keys[0], keys[-1])
    for lo_k, hi_k in zip(keys, keys[1:]):
        if lo_k <= e <= hi_k:
            f = (e - lo_k) / float(hi_k - lo_k)
            return (_EDGE_EXECUTION[lo_k]
                    + (_EDGE_EXECUTION[hi_k] - _EDGE_EXECUTION[lo_k]) * f)
    return _EDGE_EXECUTION[keys[-1]]


def _reference_ball_cache(edge):
    """Tilted reference distributions for this edge, keyed on rounded intent.

    Intent takes only a handful of distinct values across the whole surface, and
    ``tilt`` costs eight ``math.exp`` plus a normalise, so this is the difference
    between a build you notice and one you do not. Each entry is unpacked into a
    fixed tuple so the induction's inner loop never probes a dict.
    """
    ex = _edge_execution(edge)
    cache = {}

    def _at(runs, balls, wickets):
        thin = max(0, DEGRADE_FROM - wickets)
        key = (round(intent_for(runs, balls), 3), thin)
        hit = cache.get(key)
        if hit is None:
            d = tilt(_REFERENCE_BALL, key[0], ex - WICKET_DEGRADE * thin)
            extras = d["Extras"]
            free = extras * EXTRA_FREE_BALL_SHARE
            hit = (d["Dot"], d["Single"], d["Double"], d["Three"], d["Four"],
                   d["Six"], d["Wicket"], free, extras - free)
            cache[key] = hit
        return hit

    return _at


def _raw_surface(edge):
    """DP win/tie for every (balls, runs) at this edge, before correction.

    One rolling induction over the whole surface, not one full induction per
    cell. The per-cell form (a ``solve_over`` call for each of the B x R cells,
    each rebuilding its own B x R x W table) is quartic in balls x runs: it cost
    2.1s at 6 balls x 40 runs and 17.6s at 12 x 60, and never finished at a
    full-innings grid. Keeping each layer as it is produced makes it linear.
    """
    dist_at = _reference_ball_cache(edge)
    wkts = MAX_DP_WICKETS
    nruns = MAX_RUNS + 1

    # The innings-over layer, shared by "no balls left" and "no wickets left":
    # nothing more to get is a win, one short is a TIE, anything else a loss.
    term_win = [1.0] + [0.0] * MAX_RUNS
    term_tie = [0.0] * nruns
    term_tie[1] = 1.0

    prev_win = [term_win] * (wkts + 1)
    prev_tie = [term_tie] * (wkts + 1)

    win = {}
    tie = {}
    for b in range(1, MAX_BALLS + 1):
        cur_win = [term_win]
        cur_tie = [term_tie]
        for w in range(1, wkts + 1):
            nw, nt = prev_win[w], prev_tie[w]          # ball gone, wicket intact
            lw, lt = prev_win[w - 1], prev_tie[w - 1]  # ball gone, wicket down
            row_w = [1.0] + [0.0] * MAX_RUNS
            row_t = [0.0] * nruns
            for r in range(1, nruns):
                (p_dot, p_1, p_2, p_3, p_4, p_6, p_wkt,
                 p_free, p_legal) = dist_at(r, b, w)
                i1 = r - 1
                i2 = r - 2 if r > 2 else 0
                i3 = r - 3 if r > 3 else 0
                i4 = r - 4 if r > 4 else 0
                i6 = r - 6 if r > 6 else 0
                row_w[r] = (p_dot * nw[r] + p_wkt * lw[r]
                            + p_1 * nw[i1] + p_2 * nw[i2] + p_3 * nw[i3]
                            + p_4 * nw[i4] + p_6 * nw[i6]
                            + p_legal * nw[i1]
                            + p_free * row_w[i1])
                row_t[r] = (p_dot * nt[r] + p_wkt * lt[r]
                            + p_1 * nt[i1] + p_2 * nt[i2] + p_3 * nt[i3]
                            + p_4 * nt[i4] + p_6 * nt[i6]
                            + p_legal * nt[i1]
                            + p_free * row_t[i1])
            cur_win.append(row_w)
            cur_tie.append(row_t)
        prev_win, prev_tie = cur_win, cur_tie
        top_w, top_t = cur_win[wkts], cur_tie[wkts]
        for r in range(1, nruns):
            pw = top_w[r]
            pw = 0.0 if pw < 0.0 else (1.0 if pw > 1.0 else pw)
            pt = top_t[r]
            hi = 1.0 - pw
            win[(b, r)] = pw
            tie[(b, r)] = 0.0 if pt < 0.0 else (hi if pt > hi else pt)
    return win, tie


def _equivalent_anchor_ask(runs, balls):
    """The ``ANCHOR_BALLS``-ball ask at the same required rate, for reading the
    anchors from a column of a different length."""
    return int(_clamp(round(runs * ANCHOR_BALLS / float(balls)), 1, MAX_RUNS))


def _build_anchored(edge):
    """One edge column: the DP surface pulled onto the hand-written anchors.

    The correction is a per-ask logit offset measured at ``balls_left = 6``
    (where the anchors are defined) and tapered to zero as the over runs out —
    a single delivery is almost entirely determined by the distribution and
    needs no help. Applying it at the *equivalent ask* rather than at the raw
    runs value keeps the shorter columns on the same curve.
    """
    raw_win, raw_tie = _raw_surface(edge)

    win_off = {}
    tie_scale = {}
    for r in range(1, MAX_RUNS + 1):
        win_off[r] = (_logit(_win_anchor(edge, r) / 100.0)
                      - _logit(raw_win[(ANCHOR_BALLS, r)]))
        raw_t = raw_tie[(ANCHOR_BALLS, r)]
        tie_scale[r] = (_tie_anchor(edge, r) / 100.0 / raw_t) if raw_t > 1e-6 else 1.0

    win_tbl = {}
    tie_tbl = {}
    for b in range(1, MAX_BALLS + 1):
        # The hand-written anchors correct the FINAL OVER and nothing else.
        # Below the anchor horizon the correction fades out, because a single
        # delivery is almost entirely determined by the distribution and needs
        # no help; above it the correction is simply off, because the induction
        # is already right out there.
        #
        # That is a fitted result, not an assumption. Against twenty real
        # reference points from 6 to 114 balls, the raw induction lands within
        # about 6 points everywhere from 12 balls out, and carrying the 6-ball
        # correction forward at the same required rate actively broke it (24 off
        # 12 went to 55% against a real ~33%) — the two asks are not equivalent,
        # because a short horizon gets to be lucky and a long one has to be good.
        taper = (b / float(ANCHOR_BALLS)) if b <= ANCHOR_BALLS else 0.0
        wrow = {}
        trow = {}
        for r in range(1, MAX_RUNS + 1):
            eq = _equivalent_anchor_ask(r, b)
            p = _sigmoid(_logit(raw_win[(b, r)]) + win_off[eq] * taper)
            # The tie correction is NOT tapered the way the win correction is.
            # The win offset closes a gap that shrinks as the horizon does — a
            # single delivery is almost fully determined by the distribution.
            # The tie ratio is different in kind: it says the reference ball is
            # tie-happy relative to real cricket, which is just as true of the
            # last ball as of the first. Tapering it left every short-horizon
            # continuation aiming high, and the over integrated to two or three
            # times the real Super Over rate.
            t = raw_tie[(b, r)] * tie_scale[eq]
            wrow[r] = _clamp(p * 100.0, FLOOR_PCT, 99.9)
            # Ties are NOT rare on the closing balls and must not be clipped
            # as though they were: needing one off one, a dot ball IS the tie.
            # The 2-3% figure is a whole-over number, not a per-state one.
            trow[r] = _clamp(t * 100.0, 0.0, 45.0)
        win_tbl[b] = wrow
        tie_tbl[b] = trow
    return _monotonise(win_tbl), tie_tbl


def _monotonise(tbl):
    """Enforce both monotonicities in one pass: more runs to get can never be
    easier, and another delivery can never hurt.

    Done as a single sweep with b and r both ascending, clamping each cell
    between the value one ball earlier (its floor) and the value one run easier
    (its ceiling). Those two bounds can never cross — the floor is already at or
    below the previous row's easier ask, which is already at or below this row's
    — so one pass is enough and the two rules cannot fight each other, which is
    what a pair of separate passes does.
    """
    for b in range(1, MAX_BALLS + 1):
        for r in range(1, MAX_RUNS + 1):
            v = tbl[b][r]
            if b > 1:
                v = max(v, tbl[b - 1][r])
            if r > 1:
                v = min(v, tbl[b][r - 1])
            tbl[b][r] = v
    return tbl


def _build_tables():
    """Every edge bucket through the same pipeline — the outer two differ only
    in that their 6-ball anchor row was extrapolated rather than hand-written."""
    win = {}
    tie = {}
    for edge in TABLE_EDGES:
        win[edge], tie[edge] = _build_anchored(edge)
    return win, tie


# Built on first use, not at import. The surface spans the whole second
# innings (120 balls x 220 runs x 10 wickets x 5 edge buckets) and costs a
# couple of seconds to induct, which is not something every process start, test
# run and CLI invocation should pay for. It is deterministic, so building it
# once per process on the first chase is enough — and that happens inside
# ``asyncio.to_thread`` with the rest of the over, off the event loop.
_TABLES = None


def tables():
    """``(PAR_TABLE, TIE_TABLE)``, inducting them on first use."""
    global _TABLES
    if _TABLES is None:
        _TABLES = _build_tables()
    return _TABLES


def __getattr__(name):
    # PEP 562: lets ``chase_model.PAR_TABLE`` keep working as an attribute for
    # callers and tests without forcing the build at import. Module-internal
    # references go through tables() directly — a bare global name does not
    # reach here.
    if name == "PAR_TABLE":
        return tables()[0]
    if name == "TIE_TABLE":
        return tables()[1]
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


# ══════════════════════════════════════════════════════════════════════
# 5. The target model
# ══════════════════════════════════════════════════════════════════════
# Everything the edge scalar cannot express arrives as a small logit shift.
# The total is clamped so the table, not the modifiers, decides what kind of
# finish this is — the same discipline engine.chase_chance applies to its own
# matrix.

SHIFT_CLAMP = 0.8

SET_BATTER_SHIFT = {10: 0.15, 5: 0.07}
NEW_BATTER_SHIFT = -0.25
WICKETS_SHIFT = {3: -0.15, 2: -0.35, 1: -0.70, 0: -1.20}

BAT_CLUTCH_TRAITS = ("bat_finisher", "bat_power_hitter", "bat_clutch", "bat_power_surge")
BOWL_DEATH_TRAITS = ("bowl_death", "bowl_yorker")
TRAIT_SHIFT_PER = 0.12
TRAIT_SHIFT_CAP = 0.30

PART_TIME_SHIFT = 0.45
PITCH_SHIFT_PER_POINT = 0.04     # chase_chance.pitch_modifier is +/-3
MPI_SHIFT_PER_POINT = 0.012      # chase_chance.mpi_modifier is roughly +/-16
MPI_SHIFT_CAP = 0.20
DEW_SHIFT = 0.28
# The same strength ladder services.pitch_report already uses for dew.
DEW_STRENGTH = {"Light": 0.4, "Moderate": 0.7, "Heavy": 1.0}
FIELDING_SHIFT = 0.10

BAT_BLEND_STRIKER = 0.72


def effective_batting(striker_bat, non_striker_bat):
    """The pair's batting strength — the striker dominates, the partner still
    matters because the strike rotates."""
    s = float(striker_bat or 50)
    ns = float(non_striker_bat if non_striker_bat is not None else s)
    return BAT_BLEND_STRIKER * s + (1.0 - BAT_BLEND_STRIKER) * ns


def _trait_count(traits, keys):
    if not traits:
        return 0
    n = 0
    for t in traits:
        key = (t or {}).get("effect_key") if isinstance(t, dict) else t
        if key in keys:
            n += 1
    return n


def _lookup(table, edge, balls_left, runs_needed):
    """Interpolate the table on edge, in logit space."""
    b = int(_clamp(balls_left, 1, MAX_BALLS))
    r = int(max(1, runs_needed))
    edges = TABLE_EDGES
    e = _clamp(float(edge), edges[0], edges[-1])
    lo = edges[0]
    hi = edges[-1]
    for i in range(len(edges) - 1):
        if edges[i] <= e <= edges[i + 1]:
            lo, hi = edges[i], edges[i + 1]
            break
    frac = 0.0 if hi == lo else (e - lo) / float(hi - lo)

    def _at(edge_key):
        row = table[edge_key][b]
        if r <= MAX_RUNS:
            return row[r]
        return max(FLOOR_PCT, row[MAX_RUNS] * (TAIL_DECAY ** (r - MAX_RUNS)))

    a, c = _at(lo), _at(hi)
    if a <= 0.0 or c <= 0.0:
        return max(FLOOR_PCT, a + (c - a) * frac)
    return _sigmoid(_logit(a / 100.0) + (_logit(c / 100.0) - _logit(a / 100.0)) * frac) * 100.0


def situational_shift(wickets_in_hand=5, striker_balls=0, striker_traits=None,
                      bowler_traits=None, pitch=None, momentum=0.0, pressure=0.0,
                      conditions=None, fielding_quality=None,
                      part_time_bowler=False, **_rest):
    """Sum of the small logit shifts, clamped to +/-SHIFT_CLAMP.

    Takes the same input vocabulary as :func:`target_probabilities` and ignores
    the parts it has no use for (the ask itself, and the ratings, which the
    table is keyed on), so a caller can hand it the whole set.
    """
    shift = 0.0

    wih = int(max(0, wickets_in_hand))
    if wih in WICKETS_SHIFT:
        shift += WICKETS_SHIFT[wih]

    bf = int(striker_balls or 0)
    if bf >= 10:
        shift += SET_BATTER_SHIFT[10]
    elif bf >= 5:
        shift += SET_BATTER_SHIFT[5]
    elif bf == 0:
        shift += NEW_BATTER_SHIFT

    shift += min(TRAIT_SHIFT_CAP,
                 TRAIT_SHIFT_PER * _trait_count(striker_traits, BAT_CLUTCH_TRAITS))
    shift -= min(TRAIT_SHIFT_CAP,
                 TRAIT_SHIFT_PER * _trait_count(bowler_traits, BOWL_DEATH_TRAITS))

    if part_time_bowler:
        shift += PART_TIME_SHIFT

    if pitch is not None:
        from engine import chase_chance
        shift += chase_chance.pitch_modifier(pitch) * PITCH_SHIFT_PER_POINT

    if momentum or pressure:
        from engine import chase_chance
        mpi = chase_chance.mpi_modifier(momentum, pressure)
        shift += _clamp(mpi * MPI_SHIFT_PER_POINT, -MPI_SHIFT_CAP, MPI_SHIFT_CAP)

    if conditions:
        # Dew means the ball skids on and the bowlers cannot grip it — the one
        # condition that reliably decides a last over. Day matches never carry
        # a level, so there is nothing to gate on beyond its presence.
        dew = (conditions or {}).get("dew")
        if dew:
            shift += DEW_SHIFT * DEW_STRENGTH.get(dew, 0.7)

    if fielding_quality is not None:
        shift -= FIELDING_SHIFT * _clamp((float(fielding_quality) - 75.0) / 25.0, -1.0, 1.0)

    return _clamp(shift, -SHIFT_CLAMP, SHIFT_CLAMP)


def target_probabilities(runs_needed, balls_left, striker_bat=75,
                         non_striker_bat=None, bowler_rating=75, next_bat=None,
                         **factors):
    """Win / tie / lose percentages for the batting side, this ball onward.

    ``factors`` are the keyword arguments of :func:`situational_shift`.
    ``next_bat`` belongs to the same input vocabulary — it is who walks in on a
    wicket — but only :func:`continuation` has any use for it, so it is
    accepted and ignored here rather than forcing every caller to strip it.
    """
    runs_needed = int(runs_needed)
    balls_left = int(balls_left)
    if runs_needed <= 0:
        return {"win": 100.0, "tie": 0.0, "lose": 0.0}
    if balls_left <= 0:
        return {"win": 0.0, "tie": 100.0 if runs_needed == 1 else 0.0,
                "lose": 0.0 if runs_needed == 1 else 100.0}
    if runs_needed > max_possible(balls_left):
        return {"win": 0.0, "tie": 0.0, "lose": 100.0}

    edge = effective_batting(striker_bat, non_striker_bat) - float(bowler_rating or 50)
    par_table, tie_table = tables()
    win = _lookup(par_table, edge, balls_left, runs_needed)
    tie = _lookup(tie_table, edge, balls_left, runs_needed)

    shift = situational_shift(**factors)
    if shift:
        win = _sigmoid(_logit(win / 100.0) + shift) * 100.0

    win = _clamp(win, FLOOR_PCT, 99.9)
    tie = _clamp(tie, 0.0, max(0.0, 99.95 - win))
    return {"win": win, "tie": tie, "lose": max(0.0, 100.0 - win - tie)}


# ══════════════════════════════════════════════════════════════════════
# 6. The controller
# ══════════════════════════════════════════════════════════════════════

# ── Drama ────────────────────────────────────────────────────────────────
# Honest odds are the point of this model, but a flat simulation is not a game.
# Widening the steer window from one over to five also retires the scenario
# engine's scripted finale across those overs, and that theatre has to be paid
# back deliberately rather than just lost.
#
# This buys it back as VARIANCE, never as bias. It uses the same shape as
# INTENT_K — Six, Four, Dot and Wicket up, Single and Double down, so more
# happens per ball in both directions — and it is applied BEFORE
# solve_execution, exactly where the tie shaping goes. The controller then
# re-solves against the shaped distribution, so P(win) still lands on target.
#
# It works because the two vectors are not parallel: drama moves along the
# "events versus nudged singles" axis while EXEC_K trades batting-good against
# bowling-good outcomes. The solve puts the odds back without undoing the spread.
#
# Measured at 0.25 against the dial off, paired on identical seeds: the win rate
# moves +0.12 points (noise), sixes and wickets both rise in every cell. The tie
# rate does NOT rise with it — it drifts slightly DOWN over short windows, which
# was not the prediction and is worth knowing: the extra spread is as likely to
# carry a side past the target as to leave it exactly one short.
# One constant to retune, and CHASE_MODEL_DRAMA=0 to kill it.
DRAMA = _clamp(float(os.getenv("CHASE_MODEL_DRAMA", "0.25")), 0.0, 1.0)
DRAMA_FULL_BALLS = 6      # full strength inside the final over
DRAMA_MIN_FACTOR = 0.4    # and this much of it at the far edge of the window


def drama_strength(balls_left, window):
    """Ramp the drama with the phase, so the noise builds toward the finish."""
    if DRAMA <= 0.0 or balls_left <= 0:
        return 0.0
    if balls_left <= DRAMA_FULL_BALLS or window <= DRAMA_FULL_BALLS:
        return DRAMA
    over = (balls_left - DRAMA_FULL_BALLS) / float(window - DRAMA_FULL_BALLS)
    return DRAMA * max(DRAMA_MIN_FACTOR, 1.0 - (1.0 - DRAMA_MIN_FACTOR) * over)


BISECTION_STEPS = 12
# How close the natural distribution has to be before the controller stands
# down entirely, in probability.
EXEC_DEADBAND = 0.01
# How far the levelling run value may be nudged, by balls left. On the final
# delivery a tie is exactly one outcome, so the nudge is precise and can be
# given real authority; with two to go it is a crude proxy for a two-ball path
# and stays gentle.
TIE_NUDGE_MAX = {1: 0.70, 2: 0.25}


def continuation(inputs):
    """``(runs, balls, wickets) -> (P(win), P(tie))`` from the calibrated model.

    This is what the controller steers against, and using the model rather than
    a rollout of the current ball's weights is the whole reason the closed loop
    converges. Every future ball will itself be re-steered onto the model, so a
    ball that plans against anything else is planning against a future that will
    not happen: solving the over as one frozen distribution made a suppressed
    delivery expect a suppressed continuation, then the next ball lifted it back
    to target, and the over finished well above the number it was aimed at.

    A wicket brings a new batter in, so the continuation after one is priced on
    the next batter's rating with nothing on the board.

    Balls faced has to advance with the over, not stay frozen at whatever it was
    on this delivery. Leaving it fixed quietly made every continuation carry the
    current striker's new-batter penalty while the ball that actually follows
    does not — a standing gap between the future the controller planned for and
    the one it got, which pushed a whole over well past the number it was
    steered onto.
    """
    factors = {k: v for k, v in inputs.items()
               if k not in ("runs_needed", "balls_left", "next_bat")}
    faced0 = int(factors.get("striker_balls", 0) or 0)
    balls0 = int(inputs.get("balls_left", 0) or 0)
    after_wicket = dict(factors, striker_balls=0)
    if inputs.get("next_bat") is not None:
        after_wicket["striker_bat"] = inputs["next_bat"]
    cache = {}

    def _at(runs, balls, wickets, new_batter=False):
        if runs <= 0:
            return 1.0, 0.0
        if balls <= 0 or wickets <= 0:
            return (0.0, 1.0) if runs == 1 else (0.0, 0.0)
        key = (runs, balls, wickets, new_batter)
        hit = cache.get(key)
        if hit is None:
            base = after_wicket if new_batter else factors
            faced = 0 if new_batter else faced0 + max(0, balls0 - balls)
            p = target_probabilities(runs, balls,
                                     **dict(base, wickets_in_hand=wickets,
                                            striker_balls=faced))
            hit = (p["win"] / 100.0, p["tie"] / 100.0)
            cache[key] = hit
        return hit

    return _at


def one_ball_outlook(cont, dist, runs_needed, balls_left, wickets_in_hand):
    """P(win), P(tie) for this delivery: one ball, then the model takes over."""
    win = tie = 0.0
    extras = dist["Extras"]
    free = extras * EXTRA_FREE_BALL_SHARE
    legal_extra = extras - free

    def _add(p, w, t):
        nonlocal win, tie
        win += p * w
        tie += p * t

    for o, runs in _SCORING_WITH_DOT:
        p = dist[o]
        if p:
            _add(p, *cont(runs_needed - runs, balls_left - 1, wickets_in_hand))
    p = dist["Wicket"]
    if p:
        _add(p, *cont(runs_needed, balls_left - 1, wickets_in_hand - 1,
                      new_batter=True))
    if legal_extra:
        _add(legal_extra, *cont(runs_needed - 1, balls_left - 1, wickets_in_hand))
    if free:
        # A wide or a no-ball scores without using up a delivery.
        _add(free, *cont(runs_needed - 1, balls_left, wickets_in_hand))
    return win, tie


def solve_execution(dist, cont, runs_needed, balls_left, wickets_in_hand,
                    target_win, free_hit=False):
    """Bisect the execution tilt until this delivery's outlook hits the target.

    Monotone in ``execution`` by construction — every batting-good outcome moves
    one way and every bowling-good outcome the other (see EXEC_K) — which is
    what makes plain bisection valid here. Intent is deliberately NOT part of
    the search: it is a function of the ask alone, so the search axis stays
    monotone whether the side is slogging or milking singles.
    """
    intent = intent_for(runs_needed, balls_left)
    base = {o: dist[o] * math.exp(INTENT_K[o] * intent) for o in OUTCOMES}

    def _tilted(ex):
        return _normalise_capped(
            {o: base[o] * _exec_multiplier(o, ex) for o in OUTCOMES},
            free_hit=free_hit)

    def _p(ex):
        return one_ball_outlook(cont, _tilted(ex), runs_needed, balls_left,
                                wickets_in_hand)[0]

    # Deadband: when the natural weights already land where the model wants
    # them, leave the ball alone.
    if abs(_p(0.0) - target_win) < EXEC_DEADBAND:
        return 0.0, intent
    lo_ex, hi_ex = -EXEC_LIMIT, EXEC_LIMIT
    if _p(lo_ex) >= target_win:
        return lo_ex, intent
    if _p(hi_ex) <= target_win:
        return hi_ex, intent
    for _ in range(BISECTION_STEPS):
        mid = (lo_ex + hi_ex) / 2.0
        if _p(mid) < target_win:
            lo_ex = mid
        else:
            hi_ex = mid
    return (lo_ex + hi_ex) / 2.0, intent


def _tie_shape(weights, cont, runs_needed, balls_left, wickets_in_hand, want_tie):
    """Bounded two-sided nudge on the run value that levels the scores.

    A tie is only ever made on the closing balls — the scores end level exactly
    when the last delivery leaves the side one short — so this is the only place
    the Super Over rate can actually be set, and it has to push down as readily
    as up. Never touches the wicket: manufacturing a tie out of a dismissal is
    not how scores end up level.

    Applied to the distribution BEFORE the execution solve, so the win target is
    still hit exactly afterwards and only the split between a defeat and a
    Super Over moves.
    """
    if balls_left not in TIE_NUDGE_MAX:
        return weights
    level = runs_needed - 1
    key = None
    for o, runs in _SCORING_WITH_DOT:
        if runs == level:
            key = o
            break
    if key is None or weights.get(key, 0.0) <= 0.0:
        return weights
    have = one_ball_outlook(cont, _normalise_capped(weights), runs_needed,
                            balls_left, wickets_in_hand)[1]
    if have <= 1e-9 and want_tie <= 1e-9:
        return weights
    bound = TIE_NUDGE_MAX.get(balls_left, 0.0)
    ratio = _clamp(want_tie / max(have, 1e-6), 1.0 - bound, 1.0 + bound)
    out = dict(weights)
    out[key] *= ratio
    return out


def make_chase_hook(inputs, free_hit=False, window=None):
    """Build the ``weight_hook`` that steers this delivery onto the target.

    ``inputs`` is the keyword set :func:`target_probabilities` takes, plus an
    optional ``next_bat`` for the batter who would come in on a wicket.

    The hook receives the engine's fully-composed natural weights, so ratings,
    traits, pitch, conditions, approach and pressure are already priced in.
    What it solves for is the residual between what those weights would produce
    and what real cricket produces — a correction onto the calibrated number,
    not a replacement for any of it.
    """
    runs_needed = int(inputs.get("runs_needed", 0))
    balls_left = int(inputs.get("balls_left", 0))
    wickets = int(inputs.get("wickets_in_hand", 0))
    if runs_needed <= 0 or balls_left <= 0 or wickets <= 0:
        return None
    if runs_needed > max_possible(balls_left):
        return None

    cont = continuation(inputs)
    want = target_probabilities(**inputs)
    tw = _clamp(want["win"] / 100.0, 0.0005, 0.9995)
    tt = _clamp(want["tie"] / 100.0, 0.0, 0.5)
    drama = drama_strength(balls_left, window or balls_left)

    def _hook(raw_weights):
        base = {o: max(0.0, float(raw_weights.get(o, 0.0) or 0.0)) for o in OUTCOMES}
        if sum(base.values()) <= 0.0:
            return raw_weights
        # Shape the Super Over rate and widen the spread first, then solve the
        # win rate on the shaped distribution, so the win target is the one
        # that lands exactly and drama costs nothing in accuracy.
        shaped = _tie_shape(base, cont, runs_needed, balls_left, wickets, tt)
        if drama:
            shaped = {o: shaped[o] * math.exp(INTENT_K[o] * drama)
                      for o in OUTCOMES}
        dist = _normalise_capped(shaped, free_hit=free_hit)
        ex, intent = solve_execution(dist, cont, runs_needed, balls_left,
                                     wickets, tw, free_hit=free_hit)

        scale = {o: (shaped[o] / base[o]) if base[o] > 0.0 else 1.0
                 for o in OUTCOMES}
        out = dict(raw_weights)
        for o in OUTCOMES:
            if o in out:
                out[o] = (max(0.0, float(out[o] or 0.0)) * scale[o]
                          * math.exp(INTENT_K[o] * intent)
                          * _exec_multiplier(o, ex))
        return out

    return _hook

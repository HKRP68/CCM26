"""Momentum, Chasing Pressure, and the Momentum-Pressure Index.

Sections 8A, 8B and 8C of the Unified T20 Engine v3.0.

A T20 innings is not a sequence of independent overs. A side that has just taken
28 off two overs bats differently from a side that has just been strangled for
seven, and a chase that needs 14 an over with four down is a different game from
the same total needed at seven an over. Both feelings are tracked here as two
bounded accumulators, and their *net* value — the MPI — is what the ball
actually feels.

    momentum   +2.0 unstoppable ················ -2.0 collapsed
    pressure   +2.0 choking ···················· -2.0 dominant   (chases only)
    MPI        momentum - pressure

The Net Value Rule in the doc is that subtraction: a side with the momentum of a
100-run stand and the pressure of a 13-an-over ask is not "in flow", it is
roughly level, and the engine should treat it that way.

Two deliberate readings, both places where the doc is ambiguous and taking it
literally would misbehave:

* **"Wicket falls: reset toward 0, then -0.4."** Read as ``min(momentum, 0) - 0.4``.
  Read literally — snap to zero, then subtract — a wicket falling at -1.6
  momentum would *improve* the batting side's position to -0.4, so a collapsing
  side could stop its own collapse by losing another wicket. A wicket never helps
  the batting side.

* **"+12% RPO / +2 runs"** in the MPI table is one effect written twice, not two.
  The percentage is used, because it is the one that composes with the engine's
  other layers without needing the per-over run unit — and because at
  ``_RUN_UNIT``, "+2 runs" would be a 24% shift, twice what the same row's own
  percentage asks for.

Everything here is pure: no state, no config, no I/O. The caller owns the two
numbers and passes them back in.

Public API:
    update_momentum(momentum, over) -> float
    update_pressure(pressure, chase) -> float
    mpi(momentum, pressure) -> float
    mpi_effects(net) -> dict          (outcome multipliers)
    mpi_band(net) -> str              (label, for the analysis report)
"""

from engine.approach_modifiers import (
    merge_multipliers,
    runs_factor,
    wicket_factor,
)

MOMENTUM_CAP = 2.0
PRESSURE_CAP = 2.0

# ── Section 8A event weights ─────────────────────────────────────────
MOM_BOUNDARY = 0.2          # per four or six
MOM_BIG_OVER = 0.3          # an over of 12+
MOM_BIG_OVER_RUNS = 12
MOM_PARTNERSHIP_50 = 0.5    # crossing 50 together
MOM_PARTNERSHIP_100 = 0.3   # crossing 100, on top of the 50
MOM_WICKET = -0.4           # after the reset toward 0
MOM_MAIDEN = -0.3
MOM_DOT_RUN = -0.2          # three or more consecutive dots
MOM_DOT_RUN_BALLS = 3
MOM_QUIET_OVER = -0.1       # an over of fewer than 4
MOM_QUIET_OVER_RUNS = 4

# ── Section 8B event weights ─────────────────────────────────────────
PR_RRR_GAP = 0.3            # required rate 2.0+ above the current rate
PR_RRR_GAP_THRESHOLD = 2.0
PR_STEEP_ASK = 0.5          # 12+ required in the last 5 overs
PR_STEEP_ASK_RRR = 12.0
PR_STEEP_ASK_OVERS = 5
PR_DOT = 0.1               # per dot in the last 6 balls
PR_RELIEF_BOUNDARY = -0.3   # a boundary that pulls the ask back by 1.0+
PR_WICKET = 0.4
PR_FINISH_LINE = -0.5       # under 36 needed with 4+ in hand
PR_FINISH_LINE_RUNS = 36
PR_FINISH_LINE_WICKETS = 4


def _clamp(v, cap):
    """Hold an accumulator inside its symmetric hard cap."""
    return max(-cap, min(cap, float(v)))


def update_momentum(momentum, over):
    """Batting momentum after one over. ``over`` is a dict of that over's facts:

        runs, wickets, boundaries, is_maiden, longest_dot_run,
        partnership_before, partnership_after

    Every key is optional; a missing one contributes nothing, so a caller that
    cannot see an event simply does not get its swing.
    """
    m = float(momentum or 0.0)
    o = over or {}

    m += MOM_BOUNDARY * int(o.get("boundaries") or 0)

    before = int(o.get("partnership_before") or 0)
    after = int(o.get("partnership_after") or 0)
    if before < 50 <= after:
        m += MOM_PARTNERSHIP_50
    if before < 100 <= after:
        m += MOM_PARTNERSHIP_100

    # An absent ``runs`` contributes nothing; an explicit ``runs=0`` is a quiet
    # over and costs. The distinction matters because every other key here is
    # optional in the same way, and reading "no information" as "nothing was
    # scored" would let a caller that cannot see the runs drift the accumulator
    # downward every over.
    if o.get("runs") is not None:
        runs = int(o["runs"] or 0)
        if runs >= MOM_BIG_OVER_RUNS:
            m += MOM_BIG_OVER
        elif runs < MOM_QUIET_OVER_RUNS:
            m += MOM_QUIET_OVER

    if o.get("is_maiden"):
        m += MOM_MAIDEN
    if int(o.get("longest_dot_run") or 0) >= MOM_DOT_RUN_BALLS:
        m += MOM_DOT_RUN

    # A wicket wipes out the ascendancy and then costs on top of it. See the
    # module note: the reset is a ceiling, not an assignment, so a wicket can
    # never lift a collapsing side.
    for _ in range(int(o.get("wickets") or 0)):
        m = min(m, 0.0) + MOM_WICKET

    return _clamp(m, MOMENTUM_CAP)


def update_pressure(pressure, chase):
    """Chasing pressure after one over. Second innings only — the caller decides
    that; this function just applies the table. ``chase`` is a dict:

        required_rr, current_rr, overs_left, dots_last_6, relief_boundaries,
        wickets, runs_needed, wickets_in_hand
    """
    p = float(pressure or 0.0)
    c = chase or {}

    rrr = float(c.get("required_rr") or 0.0)
    crr = float(c.get("current_rr") or 0.0)
    if rrr - crr >= PR_RRR_GAP_THRESHOLD:
        p += PR_RRR_GAP
    if rrr > PR_STEEP_ASK_RRR and int(c.get("overs_left") or 0) <= PR_STEEP_ASK_OVERS:
        p += PR_STEEP_ASK

    p += PR_DOT * int(c.get("dots_last_6") or 0)
    p += PR_RELIEF_BOUNDARY * int(c.get("relief_boundaries") or 0)
    p += PR_WICKET * int(c.get("wickets") or 0)

    runs_needed = c.get("runs_needed")
    if (runs_needed is not None and int(runs_needed) < PR_FINISH_LINE_RUNS
            and int(c.get("wickets_in_hand") or 0) >= PR_FINISH_LINE_WICKETS):
        p += PR_FINISH_LINE

    return _clamp(p, PRESSURE_CAP)


def mpi(momentum, pressure=0.0):
    """The Net Value Rule: momentum and pressure net off against each other."""
    return _clamp(float(momentum or 0.0) - float(pressure or 0.0), MOMENTUM_CAP)


# ── Section 8C: what the index does to a ball ────────────────────────
#
# ``(lower_bound, scoring_factor, wicket_nudge, label)``, scanned from the top.
# The scoring column is the doc's RPO percentage; the wicket column is a
# relative nudge on the wicket weight, the same reading every other percentage
# line in this engine gets.
MPI_BANDS = (
    (1.5,  1.12, -0.05, "Unstoppable"),
    (0.5,  1.06, -0.02, "In the ascendancy"),
    (-0.4, 1.00,  0.00, "Even"),
    (-1.4, 0.92, +0.10, "Under the pump"),
    (-2.0, 0.85, +0.25, "Collapsing"),
)


def _band(net):
    """The MPI_BANDS row this index falls in, scanned from the top."""
    for lower, scoring, wkt, label in MPI_BANDS:
        if float(net) >= lower:
            return lower, scoring, wkt, label
    return MPI_BANDS[-1]


def mpi_effects(net):
    """Outcome multipliers for the current MPI. Empty dict in the neutral band."""
    _lower, scoring, wkt, _label = _band(net)
    if scoring == 1.0 and wkt == 0.0:
        return {}
    return merge_multipliers(runs_factor(scoring), wicket_factor(wkt))


def mpi_band(net):
    """Human label for the current MPI, for the match analysis report."""
    return _band(net)[3]


# The bands must be contiguous and descending, or a net value could fall into a
# gap and silently take the collapse row.
for _i in range(len(MPI_BANDS) - 1):
    assert MPI_BANDS[_i][0] > MPI_BANDS[_i + 1][0], "MPI_BANDS must descend"
assert MPI_BANDS[-1][0] <= -MOMENTUM_CAP, "MPI_BANDS must cover the full range"

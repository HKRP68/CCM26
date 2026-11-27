"""The drinks interval: one plan from each captain, halfway through an innings.

A long innings needs a decision that a short one cannot carry. Over by over,
both captains already pick an approach for the next six balls
(``engine.realism.approaches``); that is a tactical choice with a tactical
horizon. What an ODI has and a T20 does not is a *session* — twenty-five overs
in which a side commits to batting deep or to going after it, and the other
side commits to drying it up or to buying wickets.

So at the interval both captains are asked for a plan that lasts the rest of
the innings. It is not a summary card and it is not cosmetic:

  • Each side picks privately — the bowling captain first, then the batting
    captain, exactly as the per-over flow does, so neither reads the other.
  • Both plans are revealed together, with the match-up named.
  • The pair applies a bounded multiplier to runs and to dismissal risk for
    every remaining delivery of the innings.

The magnitudes are small on purpose. Spread over a session a few per cent is
worth a dozen runs or a wicket, which is the weight a session plan should
carry; it must not be able to decide a match on its own, and it must never
move the measured innings anchor far. :data:`RUN_BOUND` / :data:`WICKET_BOUND`
are the hard clamps, and ``tests/test_drinks_break.py`` checks that no plan on
either side is the best answer to everything — the same non-dominance
property the per-over frontier has.

A state with no ``drinks`` entry behaves exactly as it did before this module
existed, so matches already in flight, every T20, and The Hundred are
untouched: :func:`modifier` returns the identity.

Pure functions, no Telegram and no RNG.
"""

from __future__ import annotations

# ── The two menus ────────────────────────────────────────────────────
# (key, emoji, label, what it means). The order is the button order.
BAT_PLANS = (
    ("consolidate", "🛡️", "Consolidate",
     "Bat deep. Wickets in hand matter more than the run rate."),
    ("build", "⚙️", "Build",
     "Keep the board moving and take what is offered."),
    ("accelerate", "🚀", "Accelerate",
     "Put the foot down now and take the risk that comes with it."),
)
BOWL_PLANS = (
    ("squeeze", "🎯", "Squeeze",
     "Dry the runs up and let the pressure do the work."),
    ("rotate", "🔄", "Rotate",
     "Keep changing the bowling and never let them settle."),
    ("strike", "🔥", "Strike",
     "Attacking fields and wicket balls. The bad one costs four."),
)

BAT_KEYS = tuple(p[0] for p in BAT_PLANS)
BOWL_KEYS = tuple(p[0] for p in BOWL_PLANS)

# ── The frontier ─────────────────────────────────────────────────────
# Each plan on its own: a multiplier on expected runs and on dismissal risk.
# Batting is monotone — asking for more runs costs more wickets — so the
# choice is a priced trade-off rather than a free pick.
BAT_LEVELS = {
    "consolidate": {"runs": 0.94, "wicket": 0.90},
    "build":       {"runs": 1.00, "wicket": 1.00},
    "accelerate":  {"runs": 1.07, "wicket": 1.14},
}
BOWL_LEVELS = {
    "squeeze": {"runs": 0.95, "wicket": 0.98},
    "rotate":  {"runs": 1.00, "wicket": 1.03},
    "strike":  {"runs": 1.05, "wicket": 1.10},
}

# ── The match-ups ────────────────────────────────────────────────────
# Applied on top of both frontiers. Each line is the cricket it describes; a
# frontier on its own would have a single best answer, and these are what make
# the opponent's plan matter. Keys are (batting plan, bowling plan).
MATCHUPS = {
    # Pressure beats premeditated attack: the boundary dries up and the big
    # shot is still being played.
    ("accelerate", "squeeze"): {"runs": 0.94, "wicket": 1.12},
    # Both sides going for it. Runs and wickets, which is the trade both
    # captains signed up for.
    ("accelerate", "strike"):  {"runs": 1.06, "wicket": 1.08},
    # Changing the bowling every over denies a settled batter their rhythm —
    # and a side that has already pre-committed to attacking was not relying
    # on rhythm. Rotation is the plan acceleration is least afraid of.
    ("accelerate", "rotate"):  {"runs": 1.02, "wicket": 0.96},
    # Blocking into attacking fields with catchers in is how a side that was
    # ten down for nothing got there.
    ("consolidate", "strike"): {"runs": 0.97, "wicket": 1.16},
    # Two sides both refusing risk. Nothing happens, which is a result in
    # itself for whoever that suits.
    ("consolidate", "squeeze"): {"runs": 0.96, "wicket": 0.90},
    ("consolidate", "rotate"):  {"runs": 0.98, "wicket": 0.98},
    # Boundary denial concedes the single, and a side taking what is offered
    # is built to milk it.
    ("build", "squeeze"): {"runs": 1.05, "wicket": 0.96},
    # Pace on a length at a batter playing properly is runs.
    ("build", "strike"):  {"runs": 1.04, "wicket": 1.00},
    # The stock contest: no pattern to read, and control wins it.
    ("build", "rotate"):  {"runs": 0.96, "wicket": 1.05},
}

#: How much the interval is worth overall. The tables above are written as
#: cricket — the full strength of each effect as it would read over a single
#: over — and a session plan that moved a 25-over block by that much would be
#: deciding matches on one tap. This dial pulls every composed cell toward the
#: identity before normalisation, so one number says how heavy the interval is
#: and the tables stay readable. It is the same device
#: ``config/odi_approach.json``'s ``counter_strength`` is for the per-over
#: counters. 0.0 turns the interval into pure flavour; 1.0 is the raw tables.
SESSION_STRENGTH = 0.6

#: Hard clamps on the normalised pair. These are a rail against a future table
#: or strength edit, not part of the shaping: no cell of the grid reaches them,
#: which is what lets :func:`_normalisers` be exact rather than approximate.
#: ``tests/test_drinks_break.py`` holds both properties.
RUN_BOUND = (0.88, 1.15)
WICKET_BOUND = (0.80, 1.25)

#: The identity, for an innings with no plan.
NEUTRAL = (1.0, 1.0)


def _clamp(value, bounds):
    return max(bounds[0], min(bounds[1], value))


def _raw(bat_plan, bowl_plan):
    """The composed pair at :data:`SESSION_STRENGTH`, before normalisation."""
    bat, bowl = BAT_LEVELS[bat_plan], BOWL_LEVELS[bowl_plan]
    matchup = MATCHUPS.get((bat_plan, bowl_plan), {})
    runs = bat["runs"] * bowl["runs"] * matchup.get("runs", 1.0)
    wicket = bat["wicket"] * bowl["wicket"] * matchup.get("wicket", 1.0)
    return (1 + (runs - 1) * SESSION_STRENGTH,
            1 + (wicket - 1) * SESSION_STRENGTH)


def _normalisers():
    """Scale factors that make the mean cell of the grid the identity.

    The tables above are written as cricket: "pressure beats premeditated
    attack", "blocking into catchers gets you out". Read literally they do not
    average to nothing — the first draft of this grid added about 7% to
    dismissal risk across its nine cells, which would have pulled every ODI
    total down by a dozen runs the moment the interval existed, and quietly
    moved the innings off the anchor the measured reference was calibrated to.

    So the grid is normalised, on both axes, exactly as
    ``engine.realism.approaches`` normalises its frontier: the tables decide
    how a session is *shaped*, and the interval as a whole is worth nothing.
    The mean is taken over the nine cells evenly, because there is no measured
    distribution of captains' interval picks to weight it by — when
    ``/pitchstats`` has one, weight it the way the per-over mix is weighted.
    """
    cells = [_raw(bat, bowl) for bat in BAT_KEYS for bowl in BOWL_KEYS]
    runs = sum(c[0] for c in cells) / len(cells)
    wicket = sum(c[1] for c in cells) / len(cells)
    return 1.0 / runs, 1.0 / wicket


_RUN_NORM, _WICKET_NORM = _normalisers()


def payoff(bat_plan, bowl_plan):
    """``(run_multiplier, wicket_multiplier)`` for one pair of plans.

    Normalised so the mean cell is the identity, then clamped. Unknown keys
    fall back to the neutral pair rather than raising: a state written by a
    future version must never break a delivery.
    """
    if bat_plan not in BAT_LEVELS or bowl_plan not in BOWL_LEVELS:
        return NEUTRAL
    runs, wicket = _raw(bat_plan, bowl_plan)
    return (_clamp(runs * _RUN_NORM, RUN_BOUND),
            _clamp(wicket * _WICKET_NORM, WICKET_BOUND))


def grid_mean():
    """``(mean_runs, mean_wicket)`` over the nine cells — both 1.0 by design.

    Exposed so a test can hold the invariant rather than a comment claiming it.
    """
    cells = [payoff(bat, bowl) for bat in BAT_KEYS for bowl in BOWL_KEYS]
    return (sum(c[0] for c in cells) / len(cells),
            sum(c[1] for c in cells) / len(cells))


def label_for(key):
    """``"🚀 Accelerate"`` for either side's plan key, or the key itself."""
    for plans in (BAT_PLANS, BOWL_PLANS):
        for plan_key, emoji, text, _blurb in plans:
            if plan_key == key:
                return f"{emoji} {text}"
    return str(key or "")


def blurb_for(key):
    for plans in (BAT_PLANS, BOWL_PLANS):
        for plan_key, _emoji, _text, blurb in plans:
            if plan_key == key:
                return blurb
    return ""


# ── When the interval falls ──────────────────────────────────────────
def applies(state):
    """True for a match that takes a drinks interval at all — an ODI."""
    from services import odi_approach
    return bool(odi_approach.is_odi(state))


def interval_over(state):
    """The over after which drinks are taken, or ``None``.

    Halfway through the innings: after over 25 of fifty, after over 15 of
    thirty. Never the last over, so there is always a session left to plan.
    """
    if not applies(state):
        return None
    from services import odi_approach
    overs = odi_approach.innings_overs(state)
    return min(overs - 1, max(1, overs // 2))


def taken(state, innings=None):
    """The plans recorded for an innings, or ``None``."""
    innings = state.get("innings", 1) if innings is None else innings
    return (state.get("drinks") or {}).get(str(innings))


def due(state):
    """True when play should stop for drinks right now.

    Read between overs, after the interval over has been bowled and before the
    next bowler is chosen. False once the plans are in, and false for an
    innings that has already ended — there is no session left to plan.
    """
    over = interval_over(state)
    if over is None or taken(state) is not None:
        return False
    from services import cipl_match
    if cipl_match.is_innings_over(state):
        return False
    # ``current_over`` is the over about to be bowled, so the interval over is
    # complete when it has moved past it.
    return int(state.get("current_over") or 1) > over


def record(state, bat_plan, bowl_plan):
    """Store both captains' plans for the current innings. Returns the entry."""
    entry = {"bat": bat_plan, "bowl": bowl_plan,
             "over": interval_over(state),
             "runs": state.get("total_runs", 0),
             "wickets": state.get("total_wickets", 0)}
    state.setdefault("drinks", {})[str(state.get("innings", 1))] = entry
    return entry


def plan(state):
    """``(bat_plan, bowl_plan)`` in force this innings, or ``None``."""
    entry = taken(state)
    if not entry:
        return None
    return entry.get("bat"), entry.get("bowl")


def modifier(state):
    """The run and wicket multipliers in force, or :data:`NEUTRAL`.

    This is the one function the delivery path calls. An innings with no
    recorded plans — every T20, every Hundred match, every ODI before its
    interval and every match that predates this module — gets the identity
    pair, so the model is left exactly as it was.
    """
    pair = plan(state)
    return payoff(*pair) if pair else NEUTRAL


# ── What the chat says ───────────────────────────────────────────────
def reveal_lines(state, bat_team=None, bowl_team=None):
    """The reveal: both plans, and what the match-up does. ``[]`` with none."""
    entry = taken(state)
    if not entry:
        return []
    runs, wicket = payoff(entry.get("bat"), entry.get("bowl"))
    bat_team = bat_team or state.get("bat_team_name") or "Batting"
    bowl_team = bowl_team or state.get("bowl_team_name") or "Bowling"
    return [
        f"🏏 {bat_team}: {label_for(entry.get('bat'))}",
        f"🎳 {bowl_team}: {label_for(entry.get('bowl'))}",
        matchup_line(runs, wicket),
    ]


def matchup_line(runs, wicket):
    """One sentence on which way the session now leans."""
    if runs >= 1.03 and wicket >= 1.06:
        return "⚡ <i>A session that will not stay quiet — runs on offer, and chances.</i>"
    if runs >= 1.03:
        return "📈 <i>The runs are there for the taking this session.</i>"
    if wicket >= 1.06:
        return "🎯 <i>The bowling has the better of this match-up — wickets are coming.</i>"
    if runs <= 0.97 and wicket <= 0.97:
        return "🪨 <i>A session of attrition. Neither side is giving anything away.</i>"
    if runs <= 0.97:
        return "🔒 <i>Scoring is going to be hard work from here.</i>"
    return "⚖️ <i>Honours even — this one comes down to execution.</i>"


# ── The AI captain's call ────────────────────────────────────────────
def ai_plan(state, side):
    """The plan the AI captain commits to, as a key.

    Reads only what a captain can see: the score, the wickets standing, the
    overs left and the chase if there is one. Deterministic, so a replayed
    match makes the same call.
    """
    from services import cipl_match, odi_approach

    overs = odi_approach.innings_overs(state)
    over = int(state.get("current_over") or 1)
    left = max(1, overs - over + 1)
    hand = max(0, int(state.get("wicket_limit", 10)) - int(state.get("total_wickets", 0)))
    chase = None
    try:
        chase = cipl_match.chase(state)
    except Exception:
        chase = None

    if chase:
        # Second innings: the required rate decides it.
        rrr = float(chase.get("rrr") or 0)
        par = 6.0 if hand >= 5 else 5.0
        behind = rrr > par + 1.0
        ahead = rrr < par - 1.0
        if side == "bat":
            if hand <= 3 and not behind:
                return "consolidate"
            return "accelerate" if behind else ("build" if not ahead else "consolidate")
        if behind:
            return "squeeze"          # they must take risks; make them expensive
        return "strike" if ahead else "rotate"

    # First innings: judge the score against what the surface should produce.
    runs = int(state.get("total_runs") or 0)
    try:
        par = float(odi_approach.forecast(state))
    except Exception:
        par = runs + 5.5 * left
    rate = runs / max(1, over - 1) if over > 1 else 0.0
    projected = runs + rate * left
    strong = projected > par * 1.05
    weak = projected < par * 0.95
    if side == "bat":
        if hand <= 3:
            return "consolidate"
        if weak and hand >= 6:
            return "accelerate"
        return "build" if not strong else "accelerate"
    if strong:
        return "squeeze"
    return "strike" if weak else "rotate"

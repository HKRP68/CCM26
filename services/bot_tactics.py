"""The AI captain's *brain*: a game-theoretic model of one over of Approach cricket.

``services.bot_captain`` used to decide with hand-written weight tables — "it is
the death, so bowl Mixed more often". Tables like that are strong-ish on average
and hopeless in the specific: they never look at what an approach actually *does*
to the ball-outcome odds, and once a human works out the table the bot is solved
for good. That is exactly the complaint this module answers.

What happens here instead, every time the bot has to pick an approach:

1. **Model the next over for real.** :func:`ball_probabilities` rebuilds the
   per-ball outcome odds the way ``engine.ball_outcome`` does — the pitch's
   scoring matrix, the batter-vs-bowler rating gap, the powerplay/death boosts,
   how set the striker is — and then applies the *actual*
   ``engine.approach_modifiers`` multipliers. So the model of "what does Ultra
   Attack into Variation do" is the engine's own arithmetic, not a guess.

2. **Score the resulting over properly.** :func:`over_distribution` convolves
   those per-ball odds into the joint distribution of (runs, wickets) for the
   whole over, and :func:`over_value` turns that into one number:

     • chasing → the *win probability* of the position the over leaves behind
       (:func:`chase_win_prob`), so the endgame is played on win%, not on vibes;
     • batting first → runs minus the true price of the wickets it risks, where
       that price comes from how many balls the remaining wickets can actually
       survive (:func:`wicket_cost`) rather than a fixed penalty.

3. **Treat the over as what it is — a simultaneous, hidden-information game.**
   Both captains lock their approach in without seeing the other's, which makes
   each over a 5×5 zero-sum matrix game. :func:`solve_zero_sum` solves it with
   regret matching, giving the Nash mixed strategy. Playing that mix is what
   makes the bot *unexploitable*: there is no fixed counter to it, and it never
   picks one approach "most of the time" unless the situation genuinely has a
   dominant answer.

4. **Punish a readable opponent.** When the human's own picks show a pattern,
   :func:`best_response` computes the maximum-exploitation reply and
   ``bot_captain`` blends it in — capped, so the bot gains from reading you
   without ever becoming readable itself.

Everything is pure: state dict in, numbers out. No Telegram, no DB, no globals
beyond a small memo cache, so it is all directly unit-testable.
"""

import logging
import math

from engine.approach_modifiers import (
    BATTING_APPROACHES, BOWLING_APPROACHES, apply_approach_modifiers,
)
from engine.ball_outcome import DEFAULT_SCORING_MATRIX, PITCH_SCORING_MATRIX
from services import cipl_match

logger = logging.getLogger(__name__)

BAT_KEYS = [k for k, _e, _l in BATTING_APPROACHES]
BOWL_KEYS = [k for k, _e, _l in BOWLING_APPROACHES]

# Runs each outcome puts on the board. Extras is scored as a single (the engine
# samples wides/no-balls/byes around that mean) and a Wicket scores nothing.
RUN_VALUE = {"Dot": 0, "Single": 1, "Double": 2, "Three": 3,
             "Four": 4, "Six": 6, "Wicket": 0, "Extras": 1}

# Phase windows, scaled for short formats so a 5-over game still has all three.
POWERPLAY_UNITS = 6
DEATH_UNITS = 4

# Par scoring rate for a T20 innings, in runs per ball — the yardstick the
# value model measures a projected over against.
PAR_RPB = 8.0 / 6.0

# Balls each remaining wicket is worth, from the last recognised batter down to
# the number eleven. Read from the *end*: two wickets in hand is 7 + 5 = 12
# balls of survival, ten wickets is a full innings' worth. This is what makes a
# wicket cheap at 20/0 and ruinous at 150/8.
_WICKET_SURVIVAL = (22, 20, 18, 17, 15, 13, 11, 9, 7, 5)

# A wicket always costs something even with the shed full: a new batter starts
# again, and the engine's own "fresh batter" penalties bite for a few balls.
NEW_BATTER_COST = 5.0

# Standard deviation of runs per ball in T20 — drives how much a chase can
# swing over the balls that are left.
SIGMA_BALL = 1.75

# ── Ball-model coefficients, fitted against the engine ────────────────
# Not invented: these were least-squares fitted to ~170 situations sampled
# straight out of ``cipl_match.simulate_over`` (four pitches × seven rating
# match-ups × three phases × fresh/settled striker, 500 overs each), which is
# why the model tracks the real thing to about 14% RMS across a run-rate range
# from 2.9 to 20 an over. Re-fit them if the engine's scoring is retuned.
#
# The rating gap that drives everything. Batting and bowling ratings turn out to
# matter almost equally, which is not what the old hand-written tables assumed.
EDGE_BAT_WEIGHT = 0.768
EDGE_BOWL_WEIGHT = 0.712

# How hard each outcome responds to that gap. Sixes are wildly rating-sensitive
# (a mismatch is worth more than 7 e-folds of six probability), boundaries less
# so, and wickets swing the other way.
EDGE_FOUR = 3.91
EDGE_SIX = 7.76
EDGE_RUNNING = 2.03      # singles / twos / threes
EDGE_DOT = -2.54
EDGE_WICKET = -2.40

# Phase effects, as the engine actually produces them. The powerplay one is the
# surprise: net of everything the engine does, boundaries are *harder* in the
# first six than in the middle, so a captain who attacks on principle in the
# powerplay is wrong.
PP_BOUNDARY, PP_WICKET = 0.753, 0.672
DEATH_BOUNDARY, DEATH_WICKET = 2.02, 1.48

# A batter who has not got their eye in: boundaries roughly halve, but they are
# barely more likely to get out than a settled one — also not what the old
# tables assumed.
FRESH_BOUNDARY, FRESH_WICKET = 0.556, 1.06
FRESH_BALLS = 6

# A settled batter who is timing it.
SET_BOUNDARY, SET_WICKET = 1.12, 0.90
SET_BALLS, SET_STRIKE_RATE = 15, 130

# Regret-matching iterations for the 5×5 solve. 400 is comfortably enough for a
# matrix this small and costs well under a millisecond.
RM_ITERS = 400

# ════════════════════════════════════════════════════════════════════
# Match-reading helpers (shared with services.bot_captain)
# ════════════════════════════════════════════════════════════════════

def total_units(state):
    """Overs (or 5-ball sets) in this innings — 20 unless the format says else."""
    try:
        return max(1, int((state or {}).get("overs") or 20))
    except (TypeError, ValueError):
        return 20


def current_unit(state):
    """The over about to be bowled, 1-based."""
    try:
        return max(1, int((state or {}).get("current_over") or 1))
    except (TypeError, ValueError):
        return 1


def phase(state):
    """``"powerplay"`` / ``"middle"`` / ``"death"`` for the upcoming over."""
    total = total_units(state)
    over = current_unit(state)
    pp = POWERPLAY_UNITS if total >= 20 else max(1, round(total * 0.3))
    death = DEATH_UNITS if total >= 20 else max(1, round(total * 0.2))
    if over <= pp:
        return "powerplay"
    if over > total - death:
        return "death"
    return "middle"


def units_left(state):
    """Overs still to be bowled this innings, counting the upcoming one."""
    return max(0, total_units(state) - current_unit(state) + 1)


def death_units_left(state):
    """How many of the remaining overs fall in the death phase."""
    total = total_units(state)
    death = DEATH_UNITS if total >= 20 else max(1, round(total * 0.2))
    first_death_over = total - death + 1
    return max(0, total - max(current_unit(state), first_death_over) + 1)


def rating(player, *keys, default=50.0):
    """First usable numeric rating on ``player``, else ``default``."""
    for key in keys:
        try:
            value = (player or {}).get(key)
        except AttributeError:
            return default
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return default


def striker(state):
    """The batter on strike, or ``{}`` when the order cannot be read."""
    order = (state or {}).get("batting_order") or []
    idx = (state or {}).get("striker_idx", 0) or 0
    if 0 <= idx < len(order):
        return order[idx]
    return {}


def striker_form(state):
    """``(balls_faced, strike_rate)`` for the batter on strike."""
    bat = striker(state)
    stats = ((state or {}).get("bat_stats") or {}).get(str(bat.get("roster_id")), {})
    balls = int(stats.get("balls", 0) or 0)
    runs = int(stats.get("runs", 0) or 0)
    return balls, (runs / balls * 100.0) if balls else 0.0


def wickets_lost(state):
    """Wickets the batting side has already lost this innings."""
    try:
        return int((state or {}).get("total_wickets") or 0)
    except (TypeError, ValueError):
        return 0


def wickets_in_hand(state):
    """Wickets the batting side has left — the scarce resource the value model
    prices every aggressive approach against."""
    try:
        limit = int((state or {}).get("wicket_limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    return max(0, limit - wickets_lost(state))


def _balls_left(state):
    """Legal balls remaining in the innings, falling back to whole overs."""
    try:
        return max(0, cipl_match.total_balls(state) - cipl_match.balls_bowled(state))
    except Exception:
        return max(0, units_left(state) * 6)


def _balls_per_unit(state):
    """6 for T20, 5 for The Hundred — how long the over being modelled is."""
    try:
        return max(1, int(cipl_match.balls_per_unit(state)))
    except Exception:
        return 6


# ════════════════════════════════════════════════════════════════════
# The ball model — what one delivery actually looks like
# ════════════════════════════════════════════════════════════════════

def match_up_edge(state, bowler=None):
    """The batter-vs-bowler rating gap that drives the whole ball model.

    Positive means the batter is on top. Both ratings carry almost the same
    weight, which is the fitted answer rather than an assumed one.
    """
    bat = striker(state)
    bowl = bowler if bowler is not None else ((state or {}).get("current_bowler") or {})
    bat_rating = rating(bat, "bat_rating", "rating", default=50.0)
    bowl_rating = rating(bowl, "bowl_rating", default=60.0) if bowl else 60.0
    raw = (EDGE_BAT_WEIGHT * bat_rating - EDGE_BOWL_WEIGHT * bowl_rating) / 100.0
    return max(-1.4, min(1.4, raw))


def ball_probabilities(state, bowler=None):
    """Per-ball outcome probabilities *before* either captain's approach.

    A compact stand-in for ``engine.ball_outcome.calculate_outcome``: the
    pitch's own scoring matrix, tilted by the fitted rating gap, the phase
    effects the engine really produces, and how set the striker is. It does not
    reproduce the engine ball for ball, but it does reproduce its run and wicket
    rates closely enough that the 25 approach match-ups rank the way they
    actually play out — which is the only thing the solver needs.
    """
    pitch = (state or {}).get("pitch_type") or "Hard"
    base = dict(PITCH_SCORING_MATRIX.get(pitch) or DEFAULT_SCORING_MATRIX)

    edge = match_up_edge(state, bowler)
    mult = {
        "Four": math.exp(EDGE_FOUR * edge),
        "Six": math.exp(EDGE_SIX * edge),
        "Single": math.exp(EDGE_RUNNING * edge),
        "Double": math.exp(EDGE_RUNNING * edge),
        "Three": math.exp(EDGE_RUNNING * edge),
        "Dot": math.exp(EDGE_DOT * edge),
        "Wicket": math.exp(EDGE_WICKET * edge),
    }

    ph = phase(state)
    if ph == "powerplay":
        mult["Four"] *= PP_BOUNDARY
        mult["Six"] *= PP_BOUNDARY
        mult["Wicket"] *= PP_WICKET
    elif ph == "death":
        mult["Four"] *= DEATH_BOUNDARY
        mult["Six"] *= DEATH_BOUNDARY
        mult["Wicket"] *= DEATH_WICKET

    balls_faced, sr = striker_form(state)
    if balls_faced < FRESH_BALLS:
        mult["Four"] *= FRESH_BOUNDARY
        mult["Six"] *= FRESH_BOUNDARY
        mult["Wicket"] *= FRESH_WICKET
    elif balls_faced >= SET_BALLS and sr >= SET_STRIKE_RATE:
        mult["Four"] *= SET_BOUNDARY
        mult["Six"] *= SET_BOUNDARY
        mult["Wicket"] *= SET_WICKET

    bowl = bowler if bowler is not None else ((state or {}).get("current_bowler") or {})
    if bowl and cipl_match.is_part_time_bowler(bowl):
        mult["Wicket"] *= 0.80
        mult["Four"] *= 1.15
        mult["Six"] *= 1.15

    probs = {k: max(0.0, v * mult.get(k, 1.0)) for k, v in base.items()}
    return _normalized(probs)


def _normalized(probs):
    """Scale weights to sum to 1, falling back to uniform on a degenerate set."""
    total = sum(probs.values())
    if total <= 0:
        return {k: 1.0 / len(probs) for k in probs}
    return {k: v / total for k, v in probs.items()}


def approach_probabilities(base_probs, bat_approach, bowl_approach, bowler_rating):
    """``base_probs`` with both captains' approaches applied, renormalised.

    The multipliers come straight from ``engine.approach_modifiers``, so this is
    the same arithmetic the over itself will run.
    """
    return _normalized(apply_approach_modifiers(
        base_probs, batting_approach=bat_approach, bowling_approach=bowl_approach,
        bowler_rating=bowler_rating))


def over_distribution(probs, balls, wkts_available, run_cap=40, floor=1e-7):
    """Joint distribution of ``(runs, wickets)`` for a whole over.

    A straight convolution of ``balls`` independent deliveries, with the innings
    ending — an absorbing state — once ``wkts_available`` wickets have gone, so
    the model never scores runs off a side that is already all out. Branches
    below ``floor`` are dropped: they cannot move the ranking and pruning them
    keeps the whole 5×5 solve comfortably inside a tenth of a second.
    """
    cap_w = max(0, int(wkts_available))
    items = [(k, p) for k, p in probs.items() if p > 0.0]
    dist = {(0, 0): 1.0}
    for _ in range(max(0, int(balls))):
        nxt = {}
        for (runs, wkts), weight in dist.items():
            if wkts >= cap_w:
                nxt[(runs, wkts)] = nxt.get((runs, wkts), 0.0) + weight
                continue
            for outcome, p in items:
                share = weight * p
                if share < floor:
                    continue
                if outcome == "Wicket":
                    key = (runs, wkts + 1)
                else:
                    key = (min(run_cap, runs + RUN_VALUE.get(outcome, 0)), wkts)
                nxt[key] = nxt.get(key, 0.0) + share
        total = sum(nxt.values())
        dist = {k: v / total for k, v in nxt.items()} if total > 0 else dist
    return dist


# ════════════════════════════════════════════════════════════════════
# The value model — what an over is worth
# ════════════════════════════════════════════════════════════════════

def survivable_balls(wkts):
    """Balls the last ``wkts`` wickets can realistically survive.

    Read off the tail of :data:`_WICKET_SURVIVAL`, so the answer shrinks fast as
    a side runs out of recognised batters — which is the whole reason a wicket
    is worth 5 runs at 20/0 and 25 at 150/6.
    """
    w = max(0, min(len(_WICKET_SURVIVAL), int(wkts)))
    if w <= 0:
        return 0
    return sum(_WICKET_SURVIVAL[len(_WICKET_SURVIVAL) - w:])


def _projected_runs(balls, wkts):
    """Runs a side with ``wkts`` in hand can expect from ``balls`` at par tempo."""
    return PAR_RPB * min(max(0, balls), survivable_balls(wkts))


def wicket_cost(state, wkts_hand=None):
    """What the *next* wicket is worth, in runs, right now.

    The marginal loss of batting resource (how many more balls the side could
    have survived with that wicket) plus a flat charge for sending a new batter
    in. Batting first, this is the price the value model makes every aggressive
    approach pay.
    """
    balls = _balls_left(state)
    w = wickets_in_hand(state) if wkts_hand is None else max(0, int(wkts_hand))
    if w <= 0:
        return 0.0
    lost = _projected_runs(balls, w) - _projected_runs(balls, w - 1)
    return NEW_BATTER_COST + max(0.0, lost)


def chase_win_prob(runs_req, balls_left, wkts_hand, edge=0.0):
    """Probability the chasing side gets there from ``runs_req`` off ``balls_left``.

    A normal approximation on the run rate: compare the rate the batting side
    can sustain — capped by how many balls their remaining wickets can actually
    last — against the rate the chase demands, with the spread of a T20 innings
    narrowing as balls run out. That last part is what makes the model play the
    endgame properly: 60 off 60 with wickets in hand is close to a formality,
    while 10 off 6 is still a real contest.
    """
    if runs_req <= 0:
        return 1.0
    if balls_left <= 0 or wkts_hand <= 0:
        return 0.0

    usable = min(balls_left, survivable_balls(wkts_hand))
    if usable <= 0:
        return 0.0

    # Rate a side pushing for a win can hold, scaled by the wickets backing it
    # and by how good this batting line-up is against this attack.
    sustain = 1.55 * min(1.0, 0.72 + 0.028 * wkts_hand) * math.exp(0.45 * edge)
    # Balls the wickets cannot cover are balls that score nothing.
    effective = sustain * (usable / float(balls_left))
    required = runs_req / float(balls_left)

    sigma = SIGMA_BALL / math.sqrt(balls_left)
    z = (effective - required) / max(1e-6, sigma)
    return 1.0 / (1.0 + math.exp(-1.7 * max(-40.0, min(40.0, z))))


def over_value(state, dist, *, chase=None, edge=0.0):
    """Value of an over, from the *batting* side's point of view.

    Chasing, that is the win probability of the position the over leaves behind
    — the only objective that gets a run chase right, because 6 runs an over is
    a triumph at 30 off 60 and a disaster at 30 off 12. Batting first it is the
    expected runs minus what the wickets risked are genuinely worth, with the
    second wicket in an over priced higher than the first.
    """
    hand = wickets_in_hand(state)
    balls = _balls_per_unit(state)

    if chase and chase.get("balls_remaining", 0) > 0:
        req = chase["runs_required"]
        left = chase["balls_remaining"]
        value = 0.0
        for (runs, wkts), p in dist.items():
            if p <= 0:
                continue
            value += p * chase_win_prob(req - runs, left - balls, hand - wkts, edge)
        return value

    value = 0.0
    for (runs, wkts), p in dist.items():
        if p <= 0:
            continue
        penalty = 0.0
        for i in range(int(wkts)):
            penalty += wicket_cost(state, hand - i)
        value += p * (runs - penalty)
    return value


# ════════════════════════════════════════════════════════════════════
# The over as a 5×5 zero-sum game
# ════════════════════════════════════════════════════════════════════

# Memo of solved overs. One over is one distinct situation, so this is a single
# entry per decision in a live match; it exists so that sampling the same
# position a thousand times (a re-render, or a test) stays instant.
#
# Deliberately unlocked. Callers run this off the event loop via
# ``asyncio.to_thread``, so two matches can touch it at once — but every entry
# is a pure function of its key, so the worst a race can do is recompute a value
# or briefly overshoot the cap. Neither is worth a lock on the decision path.
_SOLVE_CACHE = {}
_SOLVE_CACHE_MAX = 128


def _fingerprint(state, bowler):
    """Everything the payoff matrix depends on, and nothing that it doesn't."""
    bat = striker(state)
    bowl = bowler if bowler is not None else ((state or {}).get("current_bowler") or {})
    balls_faced, sr = striker_form(state)
    ch = _chase(state)
    return (
        (state or {}).get("match_id"), (state or {}).get("innings"),
        (state or {}).get("pitch_type"), total_units(state), current_unit(state),
        (state or {}).get("current_ball"), (state or {}).get("ball_format"),
        int((state or {}).get("total_runs") or 0), wickets_lost(state),
        wickets_in_hand(state),
        bat.get("roster_id"), round(rating(bat, "bat_rating", "rating"), 1),
        (bowl or {}).get("roster_id"), round(rating(bowl, "bowl_rating", default=60.0), 1),
        balls_faced, round(sr, 1),
        None if not ch else (ch["runs_required"], ch["balls_remaining"]),
    )


def _chase(state):
    """The live chase maths, or ``None`` when this is not a second innings."""
    try:
        return cipl_match.chase(state)
    except Exception:
        return None


def payoff_matrix(state, bowler=None):
    """The 5×5 over payoffs, ``matrix[bat_index][bowl_index]``, batting side's view.

    Every cell is a full simulation of the over under that pair of approaches,
    scored by :func:`over_value`. This is the table the whole tactical layer
    reasons over — and it is rebuilt from the live match situation each time, so
    the same approach is a good idea in one over and a terrible one in the next.
    """
    base = ball_probabilities(state, bowler)
    bowl = bowler if bowler is not None else ((state or {}).get("current_bowler") or {})
    bowl_rating = rating(bowl, "bowl_rating", default=60.0) if bowl else 60.0
    edge = match_up_edge(state, bowler)

    ch = _chase(state)
    hand = wickets_in_hand(state)
    balls = _balls_per_unit(state)

    matrix = []
    for bat_key in BAT_KEYS:
        row = []
        for bowl_key in BOWL_KEYS:
            probs = approach_probabilities(base, bat_key, bowl_key, bowl_rating)
            dist = over_distribution(probs, balls, hand)
            row.append(over_value(state, dist, chase=ch, edge=edge))
        matrix.append(row)
    return matrix


def solved_over(state, bowler=None, iters=RM_ITERS):
    """``(matrix, batting_mix, bowling_mix, value)`` for the upcoming over, memoised.

    The build and the solve are both pure functions of the situation, so they
    are cached together on it. A live match hits this once per over; the cache
    is what keeps sampling the same position repeatedly — a re-render, or a test
    drawing a thousand decisions from one scoreline — effectively free.
    """
    key = _fingerprint(state, bowler) + (iters,)
    hit = _SOLVE_CACHE.get(key)
    if hit is not None:
        return hit
    matrix = payoff_matrix(state, bowler)
    bat_mix, bowl_mix, value = solve_zero_sum(matrix, iters=iters)
    if len(_SOLVE_CACHE) >= _SOLVE_CACHE_MAX:
        _SOLVE_CACHE.clear()
    _SOLVE_CACHE[key] = (matrix, bat_mix, bowl_mix, value)
    return _SOLVE_CACHE[key]


# Below this spread the 25 match-ups are effectively the same bet, and any
# "solution" is numerical noise amplified to look like a decision.
DECISIVE_SPAN = 0.02


def matrix_is_decisive(matrix):
    """True when the choice of approach actually changes the over's worth.

    A chase that is already gone, or a target already out of reach, produces a
    payoff matrix where every cell is the same number — win probability 0 or 1
    whatever anybody picks. Solving that returns an arbitrary mix, so callers
    check here first and fall back to their own judgement instead.
    """
    flat = [v for row in (matrix or []) for v in row]
    if len(flat) < 2:
        return False
    span = max(flat) - min(flat)
    scale = max(abs(max(flat)), abs(min(flat)), 1e-9)
    return span > DECISIVE_SPAN and span / scale > 0.005


def _regret_strategy(regret):
    """Regret matching's next mix: play each action in proportion to how much
    not having played it has cost so far. Uniform while nothing has regret yet."""
    pos = [r if r > 0.0 else 0.0 for r in regret]
    total = sum(pos)
    if total <= 0.0:
        return [1.0 / len(regret)] * len(regret)
    return [p / total for p in pos]


def solve_zero_sum(matrix, iters=RM_ITERS):
    """Nash mixed strategies for the over, by regret matching.

    Returns ``(batting_mix, bowling_mix, value)`` — two probability vectors and
    the game's value in the matrix's own units. Regret matching converges to a
    Nash equilibrium of a zero-sum game, and a Nash mix is precisely the thing a
    human opponent cannot find a counter to: any pattern they try to exploit is
    already priced in. It is also, for free, the fix for "the bot always picks
    the same approach" — an equilibrium mixes by construction.
    """
    rows = len(matrix)
    cols = len(matrix[0]) if rows else 0
    if not rows or not cols:
        return [], [], 0.0

    flat = [v for row in matrix for v in row]
    lo, hi = min(flat), max(flat)
    span = (hi - lo) or 1.0
    norm = [[(v - lo) / span for v in row] for row in matrix]

    regret_r = [0.0] * rows
    regret_c = [0.0] * cols
    sum_r = [0.0] * rows
    sum_c = [0.0] * cols
    value = 0.0

    for _ in range(max(1, int(iters))):
        pr = _regret_strategy(regret_r)
        pc = _regret_strategy(regret_c)
        for i in range(rows):
            sum_r[i] += pr[i]
        for j in range(cols):
            sum_c[j] += pc[j]

        # Row (batting) maximises, column (bowling) minimises.
        util_r = [sum(norm[i][j] * pc[j] for j in range(cols)) for i in range(rows)]
        util_c = [-sum(norm[i][j] * pr[i] for i in range(rows)) for j in range(cols)]
        value_r = sum(pr[i] * util_r[i] for i in range(rows))
        value_c = sum(pc[j] * util_c[j] for j in range(cols))
        for i in range(rows):
            regret_r[i] += util_r[i] - value_r
        for j in range(cols):
            regret_c[j] += util_c[j] - value_c
        value = value_r

    # Score the *average* strategies, which are what this returns. ``value``
    # inside the loop is the payoff of the last iterate, and the last iterate of
    # regret matching oscillates — reporting it alongside the averaged mixes
    # would put avoidable noise into the bowler ranking that reads it.
    tr = sum(sum_r) or 1.0
    tc = sum(sum_c) or 1.0
    avg_r = [x / tr for x in sum_r]
    avg_c = [x / tc for x in sum_c]
    value = sum(norm[i][j] * avg_r[i] * avg_c[j]
                for i in range(rows) for j in range(cols))
    return (avg_r, avg_c, lo + span * value)


def best_response(matrix, opponent, *, batting, sharpness=6.0):
    """The reply that exploits a predicted opponent mix, as a soft distribution.

    A hard arg-max would win the most against a *perfectly* predicted opponent
    and be trivially counter-exploited the moment the prediction is wrong, so
    the response is softmaxed over the payoff spread: clearly better answers
    dominate, marginally better ones stay a coin toss.
    """
    rows = len(matrix)
    cols = len(matrix[0]) if rows else 0
    if not rows or not cols:
        return []

    if batting:
        values = [sum(matrix[i][j] * opponent[j] for j in range(cols))
                  for i in range(rows)]
    else:
        values = [-sum(matrix[i][j] * opponent[i] for i in range(rows))
                  for j in range(cols)]

    top = max(values)
    span = top - min(values)
    if span <= 1e-9:
        return [1.0 / len(values)] * len(values)
    scale = span / sharpness
    weights = [math.exp((v - top) / scale) for v in values]
    total = sum(weights)
    return [w / total for w in weights]


# ════════════════════════════════════════════════════════════════════
# Public entry points used by services.bot_captain
# ════════════════════════════════════════════════════════════════════

def tactical_mixes(state, bowler=None):
    """``(batting_mix, bowling_mix, matrix)`` for the upcoming over.

    The two Nash mixes are what the bot plays when it has no read on its
    opponent; ``matrix`` is handed back so the caller can compute a best
    response once it does.
    """
    matrix, bat_mix, bowl_mix, _value = solved_over(state, bowler)
    return bat_mix, bowl_mix, matrix


def exploit_mix(matrix, opponent_probs, *, batting, keys):
    """Best response to a predicted opponent distribution, as a key→prob dict."""
    order = BOWL_KEYS if batting else BAT_KEYS
    vector = [max(0.0, float(opponent_probs.get(k, 0.0))) for k in order]
    total = sum(vector)
    vector = [v / total for v in vector] if total > 0 else [1.0 / len(order)] * len(order)
    response = best_response(matrix, vector, batting=batting)
    return {key: response[idx] for idx, key in enumerate(keys)}


def bowler_edge(state, player):
    """How well this bowler holds up in the upcoming over, in runs conceded.

    The *game value* of the over if this player bowls it — i.e. what the over is
    worth to the batting side once both captains pick optimally. Lower is
    better for the bowling side. Comparing candidates on this instead of on
    ``bowl_rating`` alone is what lets the bot notice that its off-spinner is
    the right answer to *this* batter in *this* phase, even though the quick is
    rated higher.
    """
    _matrix, _bat, _bowl, value = solved_over(state, player, iters=150)
    return value

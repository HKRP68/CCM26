"""Win probability, for a captain who is trying to win rather than to score.

``services.bot_tactics`` already evaluates a chase correctly: the value of an
over is the win probability of the position it leaves behind, which is the
only objective that knows 6 an over is a triumph at 30 off 60 and a disaster
at 30 off 12. Batting **first** it instead maximised expected runs minus a
wicket penalty — a proxy, and one that systematically prefers risk late in an
innings where the extra runs no longer buy much.

This module supplies the missing half: a first-innings win probability, so
both halves of a match are judged on the same currency (§10 of the brief).

HOW
---
Posting a total ``T`` wins if the opponent fails to chase ``T + 1``. So

    P(win | first innings ends on T) = 1 - P(chase of T+1 succeeds)

and the chase model is the one the bot already trusts, reused rather than
reinvented, so the two objectives cannot disagree about the same scoreboard.

Mid-innings, the total is not known yet: it is a distribution. This projects
the innings forward from the current position — the balls left, the wickets
left and the rate this batting side can sustain against this attack — and
integrates the win probability over a normal approximation to that total. The
spread is what gives the objective its shape: early on, almost any over
leaves the total distribution wide and the derivative of win probability with
respect to runs is nearly flat, so protecting wickets dominates. By the last
few overs the distribution is narrow and runs are worth almost all of it,
which is exactly when a captain should be swinging.

The model is deliberately coarse. It decides between five buttons; it does
not need to be a simulator, and the alternative it replaces was a linear
function of runs.
"""

import math

#: Runs per ball a side pushing for a win can sustain with a full shed, and
#: how much of that each remaining wicket is worth. Same shape and the same
#: numbers as ``services.bot_tactics``'s chase model, so a total is valued
#: identically whichever side of the innings break it is seen from.
SUSTAIN_RPB = 1.55
WICKET_FLOOR = 0.72
WICKET_STEP = 0.028
EDGE_SCALE = 0.45

#: Balls each remaining wicket is worth, read from the end: two wickets in
#: hand is 7 + 5 balls of survival. Mirrors ``bot_tactics._WICKET_SURVIVAL``.
WICKET_SURVIVAL = (22, 20, 18, 17, 15, 13, 11, 9, 7, 5)

#: Standard deviation of runs per ball in T20, which sets how wide a
#: projected total is with balls still to come.
SIGMA_BALL = 1.75

#: Logistic steepness for the chase. 1.7 turns a unit normal z into a
#: probability on roughly the same curve as the normal CDF.
LOGIT_K = 1.7


def survivable_balls(wickets_in_hand):
    w = max(0, min(len(WICKET_SURVIVAL), int(wickets_in_hand)))
    if w <= 0:
        return 0
    return sum(WICKET_SURVIVAL[-w:])


def sustainable_rpb(wickets_in_hand, edge=0.0):
    """Runs per ball this batting side can hold against this attack."""
    scale = min(1.0, WICKET_FLOOR + WICKET_STEP * max(0, int(wickets_in_hand)))
    return SUSTAIN_RPB * scale * math.exp(EDGE_SCALE * float(edge))


def chase_success(runs_required, balls_left, wickets_in_hand, edge=0.0):
    """Probability the chasing side gets there. Mirrors the bot's chase model."""
    if runs_required <= 0:
        return 1.0
    if balls_left <= 0 or wickets_in_hand <= 0:
        return 0.0
    usable = min(int(balls_left), survivable_balls(wickets_in_hand))
    if usable <= 0:
        return 0.0
    effective = sustainable_rpb(wickets_in_hand, edge) * (usable / float(balls_left))
    required = runs_required / float(balls_left)
    sigma = SIGMA_BALL / math.sqrt(balls_left)
    z = (effective - required) / max(1e-6, sigma)
    return 1.0 / (1.0 + math.exp(-LOGIT_K * max(-40.0, min(40.0, z))))


def win_with_total(total, innings_balls=120, opponent_wickets=10,
                   opponent_edge=0.0):
    """P(win) for a side that has posted *total*, before the chase starts."""
    return 1.0 - chase_success(float(total) + 1.0, int(innings_balls),
                               int(opponent_wickets), opponent_edge)


def projected_total(runs, balls_left, wickets_in_hand, edge=0.0):
    """Mean and standard deviation of where this first innings ends up."""
    balls_left = max(0, int(balls_left))
    if balls_left <= 0:
        return float(runs), 0.0
    usable = min(balls_left, survivable_balls(wickets_in_hand))
    rpb = sustainable_rpb(wickets_in_hand, edge)
    mean = float(runs) + rpb * usable
    # A side that cannot bat out the overs has a wider outcome, not just a
    # lower one: the innings might fold at any point in the balls its wickets
    # cannot cover.
    sigma = SIGMA_BALL * math.sqrt(balls_left)
    if usable < balls_left:
        sigma *= 1.0 + 0.5 * (1.0 - usable / float(balls_left))
    return mean, sigma


#: Gauss-Hermite style quadrature over a standard normal, as (offset, weight).
#: Five points is plenty for a smooth monotone integrand and keeps the
#: objective cheap enough to evaluate 25 times per over.
_QUAD = ((-1.75, 0.08), (-0.80, 0.22), (0.0, 0.40), (0.80, 0.22), (1.75, 0.08))


def first_innings_win_prob(runs, balls_left, wickets_in_hand,
                           innings_balls=120, edge=0.0, opponent_edge=0.0):
    """P(win) from a first-innings position, integrating over the final total.

    This is the objective that makes a first-innings captain behave like one.
    Early, the projected total is wide, so a few runs barely move it and
    keeping wickets is what matters. Late, it is narrow and runs are nearly
    the whole of it.
    """
    mean, sigma = projected_total(runs, balls_left, wickets_in_hand, edge)
    if sigma <= 0.0:
        return win_with_total(mean, innings_balls, 10, opponent_edge)
    total_w = 0.0
    acc = 0.0
    for offset, weight in _QUAD:
        total = max(0.0, mean + offset * sigma)
        acc += weight * win_with_total(total, innings_balls, 10, opponent_edge)
        total_w += weight
    return acc / (total_w or 1.0)


def over_win_delta(runs, balls_left, wickets_in_hand, innings_balls=120,
                   edge=0.0, opponent_edge=0.0):
    """How much one run and one wicket are worth here, in win probability.

    Returned as ``(per_run, per_wicket)``. Exposed because it is the clearest
    way to see the objective's shape — and to assert it in a test: the value
    of a run must rise through an innings while the cost of a wicket falls.
    """
    base = first_innings_win_prob(runs, balls_left, wickets_in_hand,
                                  innings_balls, edge, opponent_edge)
    per_run = (first_innings_win_prob(runs + 1, balls_left, wickets_in_hand,
                                      innings_balls, edge, opponent_edge)
               - base)
    per_wicket = (base
                  - first_innings_win_prob(runs, balls_left,
                                           max(0, wickets_in_hand - 1),
                                           innings_balls, edge, opponent_edge))
    return per_run, per_wicket

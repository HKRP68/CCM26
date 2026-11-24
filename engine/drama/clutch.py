"""Clutch Focus — a short-lived, team-level pressure event (Codex Prompt 05, §9).

A clutch event is a TEMPORARY exceptional-performance opportunity under
pressure. It is match-only state: no permanent clutch rating is ever written to
a player, and two identical cards in the identical situation get identical
treatment. It raises the ceiling (or the floor, for a bowling event) for at
most six legal deliveries — it does NOT guarantee a boundary or a wicket.

Eligibility (either):
  * the innings has at most ``eligibility_balls`` (18) legal deliveries left
    — the final three overs of a T20; or
  * the existing game state already flags a genuinely high-pressure situation.
It must never trigger once the match is already decided.

Activation probability, evaluated once at the start of an eligible over::

    closeness = 4 * p * (1 - p)                  # p = chasing win prob
    activation_probability = min(cap, 0.03 + 0.06 * closeness)

In the first innings there is no chase, so a lower, independently-scaled
low-pressure probability is used instead.

The event type — Batting or Bowling Clutch Focus — is a symmetric coin flip on
the seeded match RNG. It never favours the chasing side, the user's side, the
higher-rated side or any particular player.
"""

import math

ELIGIBILITY_BALLS = 18          # final three T20 overs
BATTING = "batting"
BOWLING = "bowling"

# Initial RELATIVE modifiers (§9). Converted to log space to feed the shared
# drama budget. Applied only to available legal outcomes.
BATTING_MODIFIERS = {"Four": 1.08, "Six": 1.12, "Dot": 0.96, "Wicket": 1.08}
BOWLING_MODIFIERS = {"Dot": 1.08, "Wicket": 1.10, "Four": 0.94, "Six": 0.92}

# First-innings clutch is a low-pressure event, scaled down from the chase base.
_FIRST_INNINGS_BASE = 0.015
_FIRST_INNINGS_SLOPE = 0.03


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def eligible(balls_remaining, high_pressure=False, decided=False,
             eligibility_balls=ELIGIBILITY_BALLS):
    """May a clutch event be considered at the start of this over?"""
    if decided:
        return False
    return int(balls_remaining) <= int(eligibility_balls) or bool(high_pressure)


def activation_probability(win_prob, cap=0.12, first_innings=False):
    """Probability of a clutch activation at the start of an eligible over."""
    p = _clamp(float(win_prob), 0.0, 1.0)
    closeness = 4.0 * p * (1.0 - p)
    if first_innings:
        raw = _FIRST_INNINGS_BASE + _FIRST_INNINGS_SLOPE * closeness
    else:
        raw = 0.03 + 0.06 * closeness
    return _clamp(raw, 0.0, float(cap))


def choose_type(rng):
    """Symmetric batting/bowling choice on the seeded match RNG."""
    # 0.5 split, nothing in the inputs biases it.
    return BATTING if rng.random() < 0.5 else BOWLING


def log_deltas(kind):
    """Per-outcome LOG adjustments for an active clutch event of ``kind``."""
    mods = BATTING_MODIFIERS if kind == BATTING else BOWLING_MODIFIERS
    return {k: math.log(v) for k, v in mods.items()}

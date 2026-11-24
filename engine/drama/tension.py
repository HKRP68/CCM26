"""The Tension Index (Codex Prompt 05, §7).

A live 0-100 presentation value. It is used to choose commentary intensity and
to help classify highlight events. It NEVER directly chooses an outcome and is
never fed back into the delivery probabilities — the match loop reads it only
for presentation.

Second-innings chase (the exact brief formula)::

    p = chasing side's estimated win probability
    C = 4 * p * (1 - p)                      # closeness, peaks at p = 0.5
    U = clamp(1 - balls_remaining / innings_balls, 0, 1)   # how late it is
    S = 1 if final/knockout else 0
    recent_swing = largest |win-prob change| over the last 6 legal balls, 0..1
    tension = 100 * clamp(C*(0.30 + 0.55*U) + 0.10*S + 0.05*recent_swing, 0, 1)

First innings has no chase, so it gets a SEPARATE, clearly-labelled intensity
indicator built from recent scoring volatility and recent wickets. It does not
pretend to be a win probability.
"""


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


# Interpretation thresholds (§7). Presentation bands, not statistical claims.
def band(score):
    s = int(round(score))
    if s <= 24:
        return "low"
    if s <= 49:
        return "building"
    if s <= 74:
        return "high"
    if s <= 89:
        return "thriller"
    return "extreme"


def chase_tension(p, balls_remaining, innings_balls, is_knockout=False,
                  recent_swing=0.0):
    """Second-innings Tension Index, 0-100 (rounded int)."""
    p = _clamp(float(p), 0.0, 1.0)
    innings_balls = max(1, int(innings_balls))
    C = 4.0 * p * (1.0 - p)
    U = _clamp(1.0 - int(balls_remaining) / float(innings_balls), 0.0, 1.0)
    S = 1.0 if is_knockout else 0.0
    swing = _clamp(float(recent_swing), 0.0, 1.0)
    raw = C * (0.30 + 0.55 * U) + 0.10 * S + 0.05 * swing
    return int(round(100.0 * _clamp(raw, 0.0, 1.0)))


def first_innings_intensity(recent_runs, recent_balls, recent_wickets,
                            wickets_in_hand, is_knockout=False):
    """First-innings intensity, 0-100 (rounded int).

    NOT a win probability. It rises with recent scoring volatility (a flurry of
    boundaries OR a sudden dry spell both register as "something is happening")
    and with recent wickets, and is lifted slightly when the side is a few
    wickets down so a collapse reads as tense. Bounded and presentation-only.
    """
    balls = max(1, int(recent_balls))
    rpb = float(recent_runs) / balls
    # Volatility: distance of the recent run rate from a calm ~1.25 rpb (7.5
    # rpo), in either direction, normalised so a very hot or very cold spell
    # approaches 1.0.
    volatility = _clamp(abs(rpb - 1.25) / 1.25, 0.0, 1.0)
    wkt_rate = _clamp(float(recent_wickets) / balls / 0.12, 0.0, 1.0)
    # More wickets down => a wobble matters more.
    down = _clamp((10 - int(wickets_in_hand)) / 10.0, 0.0, 1.0)
    S = 1.0 if is_knockout else 0.0
    raw = 0.45 * wkt_rate + 0.30 * volatility + 0.15 * down + 0.10 * S
    return int(round(100.0 * _clamp(raw, 0.0, 1.0)))

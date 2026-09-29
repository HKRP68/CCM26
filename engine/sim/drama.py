"""The thriller engine: ``dramaSlider`` (0-100) in the config.

Each over has a ``dramaSlider / eventChanceDivisor`` chance of one scripted
event (0.05 per over at the default 50). Close chases get a boost, and the same
slider tilts the last quarter of a chase toward a close finish.
"""

from dataclasses import dataclass

EVENT_TEXT = {
    "droppedCatch": "Put down! A sitter spilled in the deep",
    "directHit": "Direct hit! Caught short by a yard",
    "stumping": "Lightning glovework — stumped in a flash",
    "hugeOver": "Carnage — two sixes in the over",
    "drsOverturn": "DRS overturns the decision!",
    "rainScare": "Rain scare — a few drops, the covers are on standby",
    "crunchMisfield": "Misfield at the worst moment — it runs away for four",
}


@dataclass(frozen=True)
class DramaEvent:
    kind: str
    ball: int          # 1-based legal ball of the over it lands on
    text: str


def chance_per_over(cfg, close_chase=False):
    d = cfg["drama"]
    p = d["dramaSlider"] / float(d["eventChanceDivisor"])
    if close_chase:
        p *= 1 + d["closeFinish"]["strength"] * 2
    return max(0.0, min(1.0, p))


def roll_over(rng, cfg, *, bowler_is_spin=False, close_chase=False, is_chase=False):
    """At most one :class:`DramaEvent` for this over, or ``None``."""
    if not rng.chance(chance_per_over(cfg, close_chase)):
        return None
    weights = dict(cfg["drama"]["events"])
    if not bowler_is_spin:
        weights.pop("stumping", None)
    if not (is_chase and close_chase):
        weights.pop("crunchMisfield", None)
    kind = rng.weighted(weights)
    return DramaEvent(kind=kind, ball=rng.randint(1, 6), text=EVENT_TEXT[kind])


def close_finish_tilt(cfg, *, runs_needed, balls_left, wickets_left, over, total_overs):
    """(batting_mult, bowling_mult) nudging a chase back toward a close finish.

    Only in the last ``1 - fromFraction`` of the innings. Compares the required
    rate with a par-ish scoring rate for the wickets left; the side running away
    with it is pulled back by up to ``strength * slider``.
    """
    d = cfg["drama"]
    if runs_needed is None or balls_left <= 0 or total_overs <= 0:
        return 1.0, 1.0
    if over / float(total_overs) < d["closeFinish"]["fromFraction"]:
        return 1.0, 1.0
    rrr = runs_needed * 6.0 / balls_left
    par_rate = 6.0 + 0.45 * max(0, wickets_left)       # what these wickets can make
    gap = max(-1.0, min(1.0, (rrr - par_rate) / par_rate))
    s = d["closeFinish"]["strength"] * d["dramaSlider"] / 100.0
    # gap > 0: chase is struggling → help the bat; gap < 0: cruising → help the ball.
    return 1 + s * max(0.0, gap), 1 + s * max(0.0, -gap)


def is_close_chase(runs_needed, balls_left):
    if runs_needed is None or not balls_left:
        return False
    return balls_left <= 36 and abs(runs_needed - balls_left * 1.4) <= 18

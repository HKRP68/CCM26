"""Rain interruptions and DLS targets.

Built on ``engine.dls`` (the Duckworth-Lewis resource curve the bot already
uses). Limited overs:

* 1st-innings stoppage: both sides lose the overs; the chase target is scaled
  by the ratio of resources (Standard Edition, with the G50 top-up when the
  chasing side ends up with *more* resources).
* 2nd-innings stoppage: ``engine.dls.revised_target``.

Overs lost are capped so each side keeps ``minOversPerSide`` — rain here never
washes a match out, it reshapes it. Tests just lose the time.
"""

from dataclasses import dataclass
from typing import Optional

from engine import dls


@dataclass(frozen=True)
class RainOutcome:
    overs_lost: int
    new_overs: int              # overs per side after the stoppage (limited overs)
    target: Optional[int]       # revised target (None when set later / Test)
    text: str


def cap_overs_lost(total_overs, overs_bowled, lost, min_overs):
    """Never cut an innings below ``min_overs`` or below what is already bowled."""
    floor = max(min_overs, overs_bowled if overs_bowled == int(overs_bowled) else int(overs_bowled) + 1)
    return max(0, min(lost, total_overs - floor))


def first_innings_target(score, total_overs, overs_bowled_at_stop, wickets_at_stop,
                         overs_lost, g50=245.0):
    """Target for team 2 when team 1's innings was cut by ``overs_lost``.

    Team 1 had ``total_overs``; the stoppage removed ``overs_lost`` from what
    remained, and team 2 now gets ``total_overs - overs_lost``.
    """
    left_before = total_overs - overs_bowled_at_stop
    left_after = max(0.0, left_before - overs_lost)
    r1 = 100.0 - (dls.resource_pct(left_before, wickets_at_stop, total_overs)
                  - dls.resource_pct(left_after, wickets_at_stop, total_overs))
    r2 = dls.resource_pct(total_overs - overs_lost, 0, total_overs)
    if r2 <= r1:
        par = score * r2 / r1 if r1 else score
    else:
        par = score + g50 * (total_overs / 50.0) * (r2 - r1) / 100.0
    return max(1, int(par) + 1)


def second_innings_target(team1_score, total_overs, overs_bowled, overs_lost, wickets_lost):
    left_before = total_overs - overs_bowled
    left_after = max(0.0, left_before - overs_lost)
    return dls.revised_target(team1_score, total_overs, left_before, left_after, wickets_lost)


def par_score(team1_score, total_overs, overs_bowled, wickets_lost):
    """DLS par for the chasing side at this point (used if play can't resume)."""
    used = 100.0 - dls.resource_pct(total_overs - overs_bowled, wickets_lost, total_overs)
    return int(team1_score * used / 100.0)

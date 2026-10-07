"""The match situation: pressure, momentum, settling in, the tail, choke, death.

Pure: a :class:`Situation` in, a :class:`SituationMods` out. The live engine
already models all of this (``engine.pressure_engine``, ``engine.momentum``),
so these rules are used only by the standalone simulator.
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass(frozen=True)
class Situation:
    fmt: str = "T20"
    innings: int = 1
    over: int = 0                    # 0-based over of the innings
    runs_needed: Optional[int] = None
    balls_left: Optional[int] = None
    wickets_down: int = 0
    batter_balls: int = 0            # balls the striker has faced
    batting_position: int = 1        # 1-based
    last_event: Optional[str] = None # "bat" (boundary) | "bowl" (wicket) | None
    bowler_death_specialist: bool = False
    is_chase: bool = False
    save_match: bool = False         # Test 4th innings: the target is gone, bat out time


@dataclass
class SituationMods:
    shot_quality: float = 1.0
    aggression_mult: float = 1.0
    aggression_set: Optional[float] = None
    wicket: float = 1.0
    bowler_edge: float = 1.0
    accuracy: float = 1.0
    tags: List[str] = field(default_factory=list)


def is_death_over(fmt, over, cfg):
    rng = cfg["situation"]["death"]["overs"].get(fmt)
    if not rng:
        return False
    return rng[0] <= over + 1 <= rng[1]


def required_rate(runs_needed, balls_left):
    if runs_needed is None or not balls_left:
        return None
    return runs_needed * 6.0 / balls_left


def evaluate(s, cfg):
    c = cfg["situation"]
    m = SituationMods()

    rrr = required_rate(s.runs_needed, s.balls_left) if s.is_chase else None
    p = c["pressure"]
    if rrr is not None and s.fmt in p["formats"] and rrr > p["rrrThreshold"]:
        ramp = min(1.0, (rrr - p["rrrThreshold"]) * p["rampPerRun"])
        m.aggression_mult *= 1 + (p["aggression"] - 1) * ramp
        m.wicket *= 1 + (p["wicket"] - 1) * ramp
        m.tags.append("pressure")

    mo = c["momentum"]
    if s.last_event == "bat":
        m.shot_quality *= mo["shotQuality"]
    elif s.last_event == "bowl":
        m.bowler_edge *= mo["bowling"]

    nb = c["newBatter"]
    if s.batter_balls < nb["balls"]:
        m.shot_quality *= nb["shotQuality"]
        m.tags.append("settling")

    tail = c["tail"]
    if s.batting_position > tail["positionAbove"]:
        m.shot_quality *= tail["shotQuality"]
        m.tags.append("tail")

    ch = c["choke"]
    if (s.is_chase and s.runs_needed is not None and 0 < s.runs_needed < ch["targetBelow"]
            and 10 - s.wickets_down >= ch["wicketsInHand"]):
        m.shot_quality *= ch["shotQuality"]
        m.accuracy *= ch["accuracy"]
        m.tags.append("choke")

    sv = c.get("saveMatch")
    if s.save_match and sv:
        # Blocking for the draw: shots shelved, the bat comes down straight, and
        # the bowlers have to earn every wicket.
        m.aggression_set = float(sv["aggression"])
        m.wicket *= sv["wicket"]
        m.tags.append("saving")

    d = c["death"]
    if is_death_over(s.fmt, s.over, cfg):
        m.aggression_set = float(d["aggression"])
        m.accuracy *= 1 + d["yorkerAccuracy"]
        if s.bowler_death_specialist:
            m.bowler_edge *= d["specialist"]
        m.tags.append("death")
    return m

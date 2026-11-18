"""The match situation: pressure, momentum, settling in, the tail, choke, death.

Pure: a :class:`Situation` in, a :class:`SituationMods` out. The live engine
already models all of this (``engine.pressure_engine``, ``engine.momentum``),
so these rules are used only by the standalone simulator.
"""

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional


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
    total_overs: Optional[int] = None  # current allocation after any rain reduction


@dataclass
class SituationMods:
    shot_quality: float = 1.0
    aggression_mult: float = 1.0
    aggression_set: Optional[float] = None
    wicket: float = 1.0
    bowler_edge: float = 1.0
    accuracy: float = 1.0
    tags: List[str] = field(default_factory=list)
    ball_weights: Dict[str, float] = field(default_factory=dict)


def is_death_over(fmt, over, cfg, total_overs=None):
    rng = cfg["situation"]["death"]["overs"].get(fmt)
    if not rng:
        return False
    if fmt == "ODI" and total_overs is not None:
        full_overs = cfg["formats"][fmt]["overs"]
        if total_overs != full_overs:
            death_overs = math.ceil((rng[1] - rng[0] + 1) * total_overs / full_overs)
            return total_overs - death_overs <= over < total_overs
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
    death = is_death_over(s.fmt, s.over, cfg, s.total_overs)
    pacing = c.get("odiPacing") if s.fmt == "ODI" else None
    if pacing:
        f = cfg["formats"][s.fmt]
        total_overs = s.total_overs or f["overs"]
        powerplay = math.ceil(f["powerplayOvers"] * total_overs / f["overs"])
        if death:
            m.ball_weights.update(pacing["death"])
        elif s.over < powerplay:
            m.ball_weights.update(pacing["powerplay"])
        else:
            m.ball_weights.update(pacing["middle"])

    if death:
        if pacing and rrr is not None:
            # Chasers accelerate only as needed. In a comfortable chase they
            # can keep rotating the strike even with a death bowler on.
            attack = max(0.0, min(1.0, (rrr - pacing["cruiseRate"]) * pacing["rampPerRun"]))
            middle, finish = pacing["middle"], pacing["death"]
            m.ball_weights = {
                k: (1 - attack) * middle.get(k, 1.0) + attack * finish.get(k, 1.0)
                for k in middle.keys() | finish.keys()
            }
            m.aggression_mult *= 1 + attack * (pacing["chaseAggression"] - 1)
        else:
            m.aggression_set = float(d["aggression"])
        m.accuracy *= 1 + d["yorkerAccuracy"]
        if s.bowler_death_specialist:
            m.bowler_edge *= d["specialist"]
        m.tags.append("death")
    return m

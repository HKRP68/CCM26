"""Compose every environmental factor for one delivery.

A :class:`FactorSet` holds one multiplier per *channel* (swing, seam, pace,
spin, bounce, bat_ease, ...). Each module — pitch, weather, time of day,
stadium, ball aging — multiplies the channels it owns and records an
:class:`Effect` saying why, which is what lets the match summary say "the dew
after 7pm killed the spinners" instead of just showing a number.

Channels are *potential*: they say how much help the conditions offer. Whether
a particular bowler can use it (a swing bowler and the cloud, a finger spinner
and the dew) is decided by ``engine.sim.outcome``.
"""

from dataclasses import dataclass
from typing import Dict, List

CHANNELS = (
    "swing", "seam", "pace", "spin", "bounce", "bat_ease",
    "pace_wkt", "spin_wkt", "six", "four_carry", "bat_speed",
    "stamina_drain",
)


@dataclass(frozen=True)
class Effect:
    key: str          # stable id for aggregation, e.g. "dew", "morning_swing"
    channel: str
    value: float      # the multiplier applied (or amount added, for additive)
    text: str         # human sentence for the summary


class FactorSet:
    def __init__(self):
        self.m: Dict[str, float] = {c: 1.0 for c in CHANNELS}
        self.drop_add = 0.0          # extra chance a catch goes down
        self.reverse = 0.0           # reverse-swing multiplier on offer (0 = none)
        self.outfield = 65.0         # effective outfield speed 0..100
        self.dew = 0.0               # 0..1 dew intensity at this delivery
        self.swing_window_extra = 0  # overs of extra conventional swing
        self.accuracy = 1.0
        self.effects: List[Effect] = []

    def mul(self, channel, value, key=None, text=None):
        if abs(value - 1.0) < 1e-9:
            return
        self.m[channel] *= value
        if key:
            self.effects.append(Effect(key, channel, value, text or key))

    def note(self, key, channel, value, text):
        self.effects.append(Effect(key, channel, value, text))

    def __getitem__(self, channel):
        return self.m[channel]

    def as_dict(self):
        d = dict(self.m)
        d.update(drop_add=self.drop_add, reverse=self.reverse,
                 outfield=self.outfield, dew=self.dew)
        return d


def compose(cond, ball, cfg, bowler=None, include_pitch=True):
    """All factors for one delivery. Pure: same inputs → same FactorSet."""
    from engine.sim import pitch, weather, time_of_day, stadium, ball_aging

    fs = FactorSet()
    if include_pitch:
        pitch.apply(fs, cond, cfg)
    pitch.apply_grass(fs, cond, cfg)
    weather.apply(fs, cond, cfg)
    time_of_day.apply(fs, cond, ball, cfg)
    stadium.apply(fs, cond, cfg)
    ball_aging.apply(fs, cond, ball, cfg, bowler=bowler)
    return fs


def top_effects(effects, n=5, pitch_slots=None):
    """Collapse per-ball effects into the ``n`` strongest influences.

    ``effects`` is an iterable of :class:`Effect` (one per ball it was active
    on). Strength is the summed |log| of the multiplier, so a 0.7 on sixty
    balls outranks a 1.5 on two.
    """
    import math

    agg = {}
    for e in effects:
        v = e.value if e.value > 0 else 1e-6
        w = abs(math.log(v)) if e.channel != "drop" else e.value
        a = agg.setdefault(e.key, {"key": e.key, "text": e.text, "weight": 0.0, "balls": 0,
                                   "value": e.value, "channel": e.channel, "_top": -1.0})
        a["weight"] += w
        a["balls"] += 1
        # Word it by its strongest single ball (the deepest crack, the full dew).
        if w >= a["_top"]:
            a["_top"], a["text"], a["value"], a["channel"] = w, e.text, e.value, e.channel
    for a in agg.values():
        a.pop("_top", None)
    ranked = sorted(agg.values(), key=lambda a: -a["weight"])
    if pitch_slots is None:
        return ranked[:n]
    # The surface is active on every ball, so it would otherwise crowd out the
    # weather and the clock; give it at most ``pitch_slots`` of the ``n``.
    pitch = [a for a in ranked if a["key"].startswith("pitch_")][:pitch_slots]
    rest = [a for a in ranked if not a["key"].startswith("pitch_")][:n - len(pitch)]
    return sorted(pitch + rest, key=lambda a: -a["weight"])

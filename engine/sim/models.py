"""Data models for the Conditions Engine.

Frozen dataclasses, so a factor function can never mutate the match it is
reading. The one piece of state that genuinely evolves ball to ball
(:class:`BallState`) is replaced, not changed: ``advance`` returns a new one.
"""

from dataclasses import dataclass, field, replace
from typing import Dict, Optional, Tuple

SESSIONS = ("Morning", "Afternoon", "Evening", "Night")
BALL_COLORS = ("Red", "White", "Pink")
FORMATS = ("T20", "The100", "ODI", "Test")
BOUNDARY_REGIONS = ("squareLeg", "fineLeg", "longOn", "longOff")


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


@dataclass(frozen=True)
class Weather:
    cloud_cover: float = 30.0       # %
    humidity: float = 55.0          # %
    rain_chance: float = 10.0       # %
    temperature_c: float = 28.0
    wind_kph: float = 8.0
    # Which end the wind blows TOWARD: "A", "B", or None for a crosswind. The
    # bowler running in from the other end has it at his back.
    wind_toward: Optional[str] = None

    def clamped(self):
        return replace(
            self,
            cloud_cover=_clamp(self.cloud_cover, 0, 100),
            humidity=_clamp(self.humidity, 0, 100),
            rain_chance=_clamp(self.rain_chance, 0, 100),
            temperature_c=_clamp(self.temperature_c, -5, 50),
            wind_kph=_clamp(self.wind_kph, 0, 80),
        )


@dataclass(frozen=True)
class MatchTime:
    session: str = "Afternoon"      # Morning | Afternoon | Evening | Night
    is_day_night: bool = False
    ball_color: str = "White"       # Red | White | Pink
    start_hour: float = 14.0        # local clock hour the match starts


@dataclass(frozen=True)
class Stadium:
    name: str = "Neutral Venue"
    city: str = ""
    country: str = ""
    altitude_m: float = 0.0
    boundary_m: Tuple[Tuple[str, float], ...] = (
        ("squareLeg", 70.0), ("fineLeg", 66.0), ("longOn", 75.0), ("longOff", 74.0))
    outfield_speed: float = 65.0    # 0 mud .. 100 lightning
    dew_factor: float = 0.0         # 0..100
    typical_pitch: str = "Even"
    avg_first_innings: int = 170
    slope: bool = False
    climate: Tuple[Tuple[str, float], ...] = ()
    aliases: Tuple[str, ...] = ()
    # The ground's own character on top of the formulas, e.g. (("spin", 1.1),)
    # — see engine.sim.stadium.MODIFIER_CHANNELS for the names.
    modifiers: Tuple[Tuple[str, float], ...] = ()

    @property
    def boundaries(self) -> Dict[str, float]:
        return dict(self.boundary_m)

    @property
    def avg_boundary(self) -> float:
        b = self.boundaries
        return sum(b.values()) / max(1, len(b))


@dataclass(frozen=True)
class BallState:
    overs_old: float = 0.0          # 0..90
    shine: float = 100.0            # 100 -> 0
    hardness: float = 100.0         # 100 -> 0
    roughness: float = 0.0          # 0 -> 100
    color: str = "White"
    number: int = 1                 # 1st, 2nd new ball...


@dataclass(frozen=True)
class Batter:
    id: str
    name: str
    technique: float = 50
    vs_pace: float = 50
    vs_spin: float = 50
    aggression: float = 50
    form: float = 55
    stamina: float = 75
    bat_hand: str = "Right"


@dataclass(frozen=True)
class Bowler:
    id: str
    name: str
    kind: str = "pace"              # pace | spin
    style: str = ""
    pace: float = 70
    swing: float = 50
    seam: float = 50
    spin: float = 20
    accuracy: float = 55
    stamina: float = 75
    is_death_specialist: bool = False

    @property
    def is_spin(self) -> bool:
        return self.kind == "spin"


@dataclass(frozen=True)
class Conditions:
    """Everything about the match environment for one delivery."""
    pitch: str = "Even"
    weather: Weather = field(default_factory=Weather)
    time: MatchTime = field(default_factory=MatchTime)
    stadium: Stadium = field(default_factory=Stadium)
    fmt: str = "T20"
    hour: float = 15.0              # clock hour at this delivery
    innings: int = 1
    over: int = 0                   # 0-based over of the innings
    test_day: int = 1
    test_session: int = 0           # sessions completed so far in the match
    afternoon_sessions: int = 0     # hot sessions completed (crack + spin)
    hot_sessions: int = 0           # sessions played above the heat threshold
    grass: Optional[str] = None     # Heavy | Medium | Little | No Grass
    wear: float = 0.0               # extra crack from pitch age / limited-overs 2nd innings
    dew_override: Optional[float] = None   # a measured dew level (0-100) beats the stadium's
    bowling_end: str = "A"
    rained: bool = False            # the square has had rain on it this match

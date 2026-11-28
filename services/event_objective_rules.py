"""Versioned, persistence-independent rules for progressive Live Event objectives."""

from dataclasses import asdict, dataclass
from enum import Enum
import json
import re
from types import MappingProxyType


METRIC_VERSION = 1
SUPPORTED_FORMATS = frozenset({"T10", "T20", "ODI"})
MAX_TARGET = 1_000_000  # Schema limit. Schedule-specific limits apply to previews.
MAX_STAGES = 4
_OBJECTIVE_ID = re.compile(r"[a-z][a-z0-9_]{0,47}")
_STAGE_ID = re.compile(r"[a-z][a-z0-9_]{0,63}")


class Accumulator(Enum):
    SUM = "sum"
    WIN_STREAK = "win_streak"


class Opportunity(Enum):
    BATTING_DELIVERY = "batting_delivery"
    BATTING_INNINGS = "batting_innings"
    BOWLING_DELIVERY = "bowling_delivery"
    BOWLING_OVER = "bowling_over"
    BOWLING_INNINGS = "bowling_innings"
    FIXTURE = "fixture"


@dataclass(frozen=True)
class Metric:
    label: str
    unit: str
    opportunity: Opportunity
    accumulator: Accumulator = Accumulator.SUM
    formats: frozenset[str] = SUPPORTED_FORMATS
    min_target: int = 1
    max_target: int = MAX_TARGET


METRICS = MappingProxyType({
    "runs": Metric("Runs", "runs", Opportunity.BATTING_DELIVERY),
    "boundaries": Metric("Boundaries", "boundaries", Opportunity.BATTING_DELIVERY),
    "fours": Metric("Fours", "fours", Opportunity.BATTING_DELIVERY),
    "sixes": Metric("Sixes", "sixes", Opportunity.BATTING_DELIVERY),
    "fifties": Metric("Fifties", "innings", Opportunity.BATTING_INNINGS),
    "centuries": Metric("Centuries", "innings", Opportunity.BATTING_INNINGS),
    "wickets": Metric("Bowler-credited wickets", "wickets", Opportunity.BOWLING_INNINGS),
    "dots": Metric("Dot balls", "balls", Opportunity.BOWLING_DELIVERY),
    "maidens": Metric("Maidens", "overs", Opportunity.BOWLING_OVER),
    "three_wicket_hauls": Metric("Three-wicket hauls", "hauls", Opportunity.BOWLING_INNINGS),
    "five_wicket_hauls": Metric("Five-wicket hauls", "hauls", Opportunity.BOWLING_INNINGS),
    "wins": Metric("Wins", "matches", Opportunity.FIXTURE),
    "chases": Metric("Successful chases", "matches", Opportunity.FIXTURE),
    "defenses": Metric("Successful defenses", "matches", Opportunity.FIXTURE),
    "win_streak": Metric("Longest win streak", "matches", Opportunity.FIXTURE, Accumulator.WIN_STREAK),
})


@dataclass(frozen=True)
class Stage:
    id: str
    target: int
    stars: int
    locked: bool = False


@dataclass(frozen=True)
class Objective:
    id: str
    metric: str
    stages: tuple[Stage, ...]
    optional_playoffs: bool = False


def positive_integer(value, label, maximum=None):
    if type(value) is not int or value < 1 or (maximum is not None and value > maximum):
        raise ValueError(f"Invalid {label}")
    return value


def _fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() <= set(required) | set(optional):
        raise ValueError("Invalid objective configuration fields")


def validate_identifier(value, *, stage=False):
    pattern = _STAGE_ID if stage else _OBJECTIVE_ID
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError("Invalid objective/stage ID")
    return value


def _unique_identifier(value, used, *, stage=False):
    validate_identifier(value, stage=stage)
    if value in used:
        raise ValueError("Invalid or duplicate objective/stage ID")
    used.add(value)
    return value


def _boolean(value):
    if type(value) is not bool:
        raise ValueError("Expected a boolean")
    return value


def _unique_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"Duplicate JSON key {key}")
        obj[key] = value
    return obj


def parse_objectives(payload, *, match_format, star_cap):
    """Validate an opt-in revision. Stage IDs are unique within a participant's revision."""
    positive_integer(star_cap, "star cap")
    if match_format not in SUPPORTED_FORMATS:
        raise ValueError("Unsupported objective format")
    config = json.loads(payload, object_pairs_hook=_unique_keys)
    _fields(config, ("metric_version", "objectives"))
    if type(config["metric_version"]) is not int or config["metric_version"] != METRIC_VERSION:
        raise ValueError("Unsupported metric version")
    if not isinstance(config["objectives"], list):
        raise ValueError("Expected an objectives list")
    objectives, objective_ids, stage_ids = [], set(), set()
    total_stars = 0
    for definition in config["objectives"]:
        _fields(definition, ("id", "metric", "stages"), ("optional_playoffs",))
        objective_id = _unique_identifier(definition["id"], objective_ids)
        metric_id = definition["metric"]
        metric = METRICS.get(metric_id) if isinstance(metric_id, str) else None
        if metric is None or match_format not in metric.formats:
            raise ValueError("Unknown metric or unsupported metric format")
        stages = definition["stages"]
        if not isinstance(stages, list) or not 1 <= len(stages) <= MAX_STAGES:
            raise ValueError("Invalid stage count")
        previous_target = 0
        parsed_stages = []
        for stage in stages:
            _fields(stage, ("id", "target", "stars"), ("locked",))
            stage_id = _unique_identifier(stage["id"], stage_ids, stage=True)
            target = positive_integer(stage["target"], "stage target", metric.max_target)
            if target < metric.min_target or target <= previous_target:
                raise ValueError("Stage targets must strictly increase")
            previous_target = target
            stars = positive_integer(stage["stars"], "stage stars", star_cap)
            total_stars += stars
            if total_stars > star_cap:
                raise ValueError("Star cap exceeded")
            parsed_stages.append(Stage(stage_id, target, stars, _boolean(stage.get("locked", False))))
        objectives.append(Objective(objective_id, metric_id, tuple(parsed_stages),
                                    _boolean(definition.get("optional_playoffs", False))))
    return tuple(objectives)


def eligible_stages(objective, total, entitled_stage_ids):
    """Return every threshold met. Settlement must enforce durable entitlement uniqueness."""
    return tuple(stage for stage in objective.stages
                 if stage.target <= total and stage.id not in entitled_stage_ids)


def export_objectives(objectives):
    """Export editable rules; publish storage must separately pin generator settings."""
    return json.dumps({
        "metric_version": METRIC_VERSION,
        "objectives": [asdict(objective) for objective in objectives],
    }, indent=2)

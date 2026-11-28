"""Draft ladder generation from the published league schedule, without persistence."""

from collections import Counter
from dataclasses import dataclass
from math import ceil, isfinite
from types import MappingProxyType

from services.event_objective_rules import (
    MAX_STAGES, METRICS, Objective, Stage, export_objectives, parse_objectives,
    positive_integer, validate_identifier,
)
from services.match_formats import FORMATS


GENERATOR_VERSION = 1
PRESETS = MappingProxyType({"Easy": 0.55, "Balanced": 0.75, "Hard": 0.95, "Extreme": 1.2})
STAGE_NAMES = ("bronze", "silver", "gold", "diamond")
# Planning rates for a full T20 league fixture, pending measured event data.
_T20_RATES = {
    "runs": 140, "boundaries": 16, "fours": 11, "sixes": 5,
    "fifties": 0.6, "centuries": 0.08, "wickets": 6, "dots": 44,
    "maidens": 0.2, "three_wicket_hauls": 0.4, "five_wicket_hauls": 0.08,
    "wins": 0.5, "chases": 0.25, "defenses": 0.25, "win_streak": 0.35,
}
_FIXTURE_METRICS = {"wins", "chases", "defenses", "win_streak"}


@dataclass(frozen=True)
class LeagueFixture:
    id: str
    home: str
    away: str


@dataclass(frozen=True)
class LadderPreview:
    objective: Objective
    guaranteed_matches: int
    per_match_rate: float
    rate_source: str
    difficulty: str
    planning_maximum: int
    star_liability: int
    estimated_completion_rates: tuple[float | None, ...]
    warnings: tuple[str, ...]
    generator_version: int = GENERATOR_VERSION


def guaranteed_matches(fixtures, team_ids, *, team_scope=None):
    """Count canonical league fixtures per team, including teams with none."""
    teams = set(team_ids)
    if not teams or (team_scope is not None and team_scope not in teams):
        raise ValueError("Invalid league team scope")
    counts = Counter(dict.fromkeys(teams, 0))
    seen = set()
    for fixture in fixtures:
        if (not fixture.id or fixture.id in seen or fixture.home == fixture.away
                or fixture.home not in teams or fixture.away not in teams):
            raise ValueError("Invalid or duplicate league fixture")
        seen.add(fixture.id)
        counts.update((fixture.home, fixture.away))
    return counts[team_scope] if team_scope is not None else min(counts.values())


def _per_fixture_maximum(metric, overs):
    # Batting totals use a finite planning limit based on legal deliveries.
    # Additional scoring deliveries can occur through no-balls.
    return {
        "runs": overs * 36, "boundaries": overs * 6, "fours": overs * 6,
        "sixes": overs * 6, "fifties": 11, "centuries": 11, "wickets": 10,
        "dots": overs * 6, "maidens": overs, "three_wicket_hauls": 3,
        "five_wicket_hauls": 2, "wins": 1, "chases": 1, "defenses": 1,
        "win_streak": 1,
    }[metric]


def generate_ladder(*, objective_id, metric, match_format, fixtures, team_ids,
                    star_cap, difficulty="Balanced", stage_count=4, team_scope=None,
                    measured_rate=None, observed_league_totals=(), existing=None, regenerate=False):
    """Preview one ladder. Pass the remaining revision-wide star budget as star_cap.

    The caller supplies league fixtures only and may supply a matching observed
    format/strength-band rate and totals from comparable league schedules.
    Existing stages survive by default. Explicit
    regeneration replaces unlocked stages while keeping locks in their slots.
    """
    validate_identifier(objective_id)
    if metric not in METRICS or match_format not in METRICS[metric].formats:
        raise ValueError("Unsupported metric or format")
    if difficulty not in PRESETS:
        raise ValueError("Unknown difficulty")
    positive_integer(star_cap, "star cap")
    positive_integer(stage_count, "stage count", MAX_STAGES)
    if existing and (existing.id != objective_id or existing.metric != metric):
        raise ValueError("Existing ladder identity does not match")
    if existing and existing.optional_playoffs and regenerate:
        raise ValueError("Optional playoff ladders require manual editing")

    matches = guaranteed_matches(fixtures, team_ids, team_scope=team_scope)
    overs = FORMATS[match_format]["overs"]
    maximum = min(matches * _per_fixture_maximum(metric, overs), METRICS[metric].max_target)
    if measured_rate is None:
        rate = _T20_RATES[metric]
        if metric not in _FIXTURE_METRICS:
            rate *= overs / 20
        source = "heuristic"
    else:
        if (type(measured_rate) not in (float, int)
                or not 0 <= measured_rate <= METRICS[metric].max_target
                or not isfinite(measured_rate)):
            raise ValueError("Invalid measured rate")
        rate, source = measured_rate, "measured"

    warnings = []
    if source == "heuristic":
        warnings.append("Targets use heuristic per-match rates.")
    if metric == "centuries":
        warnings.append("Centuries are rare and depend on batting opportunities.")
    if metric in {"runs", "boundaries", "fours", "sixes"}:
        warnings.append("Planning maximum assumes scoring on legal deliveries.")

    if existing and not regenerate:
        objective = existing
    else:
        previous_stages = existing.stages if existing else ()
        locked = {index: stage for index, stage in enumerate(previous_stages) if stage.locked}
        if locked and max(locked) >= stage_count:
            raise ValueError("Stage count would remove a locked stage")
        available_stars = star_cap - sum(stage.stars for stage in locked.values())
        if available_stars < 0:
            raise ValueError("Locked stages exceed the star cap")

        stages = []
        base_stars = max(1, available_stars // max(1, stage_count - len(locked)))
        for index in range(stage_count):
            if index in locked:
                stages.append(locked[index])
                continue
            lower = stages[-1].target + 1 if stages else 1
            next_lock = min((stage.target for slot, stage in locked.items() if slot > index),
                            default=maximum + 1)
            upper = min(maximum, next_lock - 1)
            estimate = matches * rate * PRESETS[difficulty] * (index + 1) / stage_count
            step = 5 if estimate >= 20 else 1
            target = min(ceil(estimate / step) * step, upper)
            if available_stars < base_stars or target < lower:
                continue
            stage_id = (previous_stages[index].id if index < len(previous_stages)
                        else f"{objective_id}_{STAGE_NAMES[index]}")
            stages.append(Stage(stage_id, target, base_stars))
            available_stars -= base_stars
        if len(stages) < stage_count:
            warnings.append("Some stages could not fit within the schedule and star budget.")
        objective = Objective(objective_id, metric, tuple(stages))

    if objective.stages:
        parse_objectives(export_objectives((objective,)), match_format=match_format, star_cap=star_cap)
    if objective.optional_playoffs:
        warnings.append("Optional playoff ladder. Preview maximum covers league fixtures only.")
    if any(stage.target > maximum for stage in objective.stages):
        warnings.append("A retained stage exceeds the league planning maximum.")
    observed_totals = tuple(observed_league_totals)
    if any(type(total) is not int or total < 0 for total in observed_totals):
        raise ValueError("Invalid observed league total")
    completion_rates = tuple(
        sum(total >= stage.target for total in observed_totals) / len(observed_totals)
        if observed_totals else None
        for stage in objective.stages
    )
    if objective.stages and not observed_totals:
        warnings.append("Stage completion rates require comparable historical league totals.")
    return LadderPreview(objective, matches, rate, source, difficulty, maximum,
                         sum(stage.stars for stage in objective.stages), completion_rates,
                         tuple(warnings))

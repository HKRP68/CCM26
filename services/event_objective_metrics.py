"""Metric v1 scorecard reduction for use by a future canonical settlement adapter."""

from dataclasses import dataclass
from enum import Enum

from services.event_objective_rules import Accumulator, METRICS


@dataclass(frozen=True)
class BattingLine:
    runs: int
    fours: int
    sixes: int


@dataclass(frozen=True)
class BowlingLine:
    """Use engine credits. Dots exclude all extras and include zero-run wickets."""

    wickets: int  # Excludes dismissals such as run-outs that the bowler did not earn.
    dots: int
    maidens: int


class Outcome(Enum):
    LOSS = "loss"
    TIE = "tie"
    CHASE = "chase"
    DEFENSE = "defense"
    TIEBREAK_WIN = "tiebreak_win"


_WINS = {Outcome.CHASE, Outcome.DEFENSE, Outcome.TIEBREAK_WIN}


def fixture_metrics(batting, bowling, *, outcome):
    """Reduce one team's regulation scorecards from an eligible official fixture.

    The caller must establish fixture/participant eligibility from committed state
    before using this reducer. Forfeit, void, AI-only and practice fixtures must
    never be passed here. Supply both human sides separately when applicable.
    """
    if not isinstance(outcome, Outcome):
        raise ValueError("Invalid fixture outcome")
    batting, bowling = tuple(batting), tuple(bowling)
    fours = sum(line.fours for line in batting)
    sixes = sum(line.sixes for line in batting)
    return {
        "runs": sum(line.runs for line in batting),
        "boundaries": fours + sixes,
        "fours": fours,
        "sixes": sixes,
        "fifties": sum(50 <= line.runs < 100 for line in batting),
        "centuries": sum(line.runs >= 100 for line in batting),
        "wickets": sum(line.wickets for line in bowling),
        "dots": sum(line.dots for line in bowling),
        "maidens": sum(line.maidens for line in bowling),
        "three_wicket_hauls": sum(line.wickets >= 3 for line in bowling),
        "five_wicket_hauls": sum(line.wickets >= 5 for line in bowling),
        "wins": int(outcome in _WINS),
        "chases": int(outcome is Outcome.CHASE),
        "defenses": int(outcome is Outcome.DEFENSE),
        "win_streak": int(outcome in _WINS),
    }


@dataclass(frozen=True)
class Totals:
    metrics: dict[str, int]
    current_win_streak: int


def accumulate_fixtures(contributions):
    """Reduce unique eligible contributions in canonical fixture order."""
    totals = dict.fromkeys(METRICS, 0)
    streak = 0
    for contribution in contributions:
        for metric_id, metric in METRICS.items():
            value = contribution[metric_id]
            if metric.accumulator is Accumulator.WIN_STREAK:
                streak = streak + 1 if value else 0
                totals[metric_id] = max(totals[metric_id], streak)
            else:
                totals[metric_id] += value
    return Totals(totals, streak)

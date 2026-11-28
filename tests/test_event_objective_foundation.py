import json

import pytest

from services.event_objective_generator import LeagueFixture, generate_ladder, guaranteed_matches
from services.event_objective_metrics import (
    BattingLine, BowlingLine, Outcome, accumulate_fixtures, fixture_metrics,
)
from services.event_objective_rules import eligible_stages, export_objectives, parse_objectives


def rules():
    return {"metric_version": 1, "objectives": [{
        "id": "boundary_master", "metric": "boundaries", "stages": [
            {"id": "boundary_bronze", "target": 50, "stars": 5, "locked": True},
            {"id": "boundary_silver", "target": 100, "stars": 10},
        ],
    }]}


def parse(config):
    return parse_objectives(json.dumps(config), match_format="T20", star_cap=15)


def test_progress_earns_both_stages_and_respects_prior_entitlements():
    objective, = parse(rules())
    assert parse_objectives(export_objectives((objective,)), match_format="T20", star_cap=15) == (objective,)
    prior = fixture_metrics([BattingLine(200, 30, 15)], [], outcome=Outcome.LOSS)
    result = fixture_metrics([BattingLine(250, 55, 2)], [], outcome=Outcome.DEFENSE)
    progress = accumulate_fixtures([prior, result]).metrics["boundaries"]
    assert progress == 102
    assert [stage.stars for stage in eligible_stages(objective, progress, set())] == [5, 10]
    assert [stage.stars for stage in eligible_stages(objective, progress, {"boundary_bronze"})] == [10]


@pytest.mark.parametrize("field,value", [
    ("id", "boundary_bronze"), ("target", 50), ("target", True), ("target", 0), ("stars", 11),
])
def test_invalid_stage(field, value):
    config = rules()
    config["objectives"][0]["stages"][1][field] = value
    with pytest.raises(ValueError):
        parse(config)


def test_unknown_metric_and_invalid_json():
    config = rules()
    config["objectives"][0]["metric"] = "eval_anything"
    with pytest.raises(ValueError):
        parse(config)
    with pytest.raises(ValueError):
        parse_objectives('{"metric_version":1,"metric_version":1}', match_format="T20", star_cap=10)
    with pytest.raises(ValueError):
        parse_objectives('{', match_format="T20", star_cap=10)


def test_regulation_milestones_and_streak():
    loss = fixture_metrics(
        [BattingLine(112, 8, 4), BattingLine(73, 6, 3)],
        [BowlingLine(5, 14, 1)], outcome=Outcome.LOSS,
    )
    win = fixture_metrics([], [], outcome=Outcome.CHASE)
    assert loss["centuries"] == loss["fifties"] == 1
    assert loss["wickets"] == 5
    assert loss["three_wicket_hauls"] == loss["five_wicket_hauls"] == 1
    assert loss["dots"] == 14
    assert loss["wins"] == 0
    totals = accumulate_fixtures([win, win, loss, win])
    assert totals.metrics["win_streak"] == 2
    assert totals.current_win_streak == 1
    assert totals.metrics["chases"] == 3


def test_small_imbalanced_league_and_manual_edits():
    fixtures = [LeagueFixture("1", "a", "b"), LeagueFixture("2", "a", "c")]
    teams = ["a", "b", "c"]
    assert guaranteed_matches(fixtures, teams) == 1
    assert guaranteed_matches(fixtures, teams, team_scope="a") == 2
    with pytest.raises(ValueError):
        guaranteed_matches(fixtures * 2, teams)
    preview = generate_ladder(objective_id="winner", metric="wins", match_format="T20",
                              fixtures=fixtures, team_ids=teams, star_cap=10)
    assert preview.planning_maximum == 1
    assert [stage.target for stage in preview.objective.stages] == [1]
    assert preview.rate_source == "heuristic"
    edited, = parse(rules())
    args = dict(objective_id=edited.id, metric=edited.metric, match_format="T20",
                fixtures=fixtures, team_ids=teams, star_cap=15, existing=edited)
    preserved = generate_ladder(**args, observed_league_totals=[40, 70, 110, 150])
    assert preserved.objective == edited
    assert preserved.estimated_completion_rates == (0.75, 0.5)
    regenerated = generate_ladder(**args, regenerate=True, measured_rate=20)
    assert regenerated.objective.stages == (edited.stages[0],)
    assert regenerated.rate_source == "measured"

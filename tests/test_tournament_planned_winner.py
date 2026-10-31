"""Chosen simulation outcomes must stay consistent with deliveries and stats."""

import itertools
import json
import random

import pytest

from services import cipl_match as cm
from services import config_service
from services import tournament_match_settings as settings
from tests.test_cipl_approach import _make_state


@pytest.fixture(autouse=True)
def stable_economy_configuration(monkeypatch):
    # The engine reads approach configuration; these tests need no database.
    monkeypatch.setattr(config_service, "get_config",
                        lambda *args, **kwargs: dict(config_service.DEFAULTS))


def scored(outcome):
    extra = outcome.get("is_extra") or outcome.get("type") == "extra"
    penalty = bool(extra and outcome.get("extra_type") in ("Wide", "No Ball"))
    return int(outcome.get("runs") or 0) + int(penalty), penalty


def assert_innings_accounting(state):
    assert state["total_runs"] == (
        sum(stat["runs"] for stat in state["bat_stats"].values())
        + state["extras_total"])
    assert sum(stat["balls"] for stat in state["bowl_stats"].values()) == cm.balls_bowled(state)
    assert sum(bool(stat["out"]) for stat in state["bat_stats"].values()) == state["total_wickets"]
    assert all(stat["runs"] >= 0 and stat["balls"] >= 0
               for stat in state["bat_stats"].values())


def finish_innings(state, *, restore=False):
    for _ in range(cm.total_balls(state) * 6):
        if cm.is_innings_over(state):
            return state
        if state.get("current_bowler") is None:
            state["current_bowler"] = cm.eligible_bowlers(state)[0]
        state["batting_approach"] = "ultra" if state["current_over"] % 2 else "defensive"
        state["bowling_approach"] = "variation" if state["current_over"] % 2 else "aggressive"
        before = state["total_runs"]
        cm.simulate_over(state, pause_after_delivery=restore)
        assert state["total_runs"] >= before, "a delivery must never rewrite an earlier total"
        assert_innings_accounting(state)
        if restore:
            state = json.loads(json.dumps(state))
    pytest.fail("the selected simulation did not complete within its delivery budget")


@pytest.mark.parametrize("winner_id", [10, 20])
@pytest.mark.parametrize("overs,ball_format,pitch", [
    (5, "T20", "Green"), (20, "T20", "Flat"), (20, "The100", "Hard"),
])
@pytest.mark.parametrize("seed", [3, 19, 41])
def test_selected_team_wins_a_close_full_match_with_consistent_scorecards(
        winner_id, overs, ball_format, pitch, seed):
    random.seed(seed)
    state = _make_state(overs=overs, ball_format=ball_format)
    state.update(_planned_winner_user_id=winner_id, pitch_type=pitch)
    state = finish_innings(state)
    cm.end_first_innings(state)
    first_score = state["inn1_runs"]
    state = finish_innings(state)
    result = cm.compute_result(state)
    assert not result["tie"]
    assert result["winner"] == {10: "Team A", 20: "Team B"}[winner_id]
    assert cm.balls_bowled(state) == cm.total_balls(state)
    assert state["inn1_runs"] == first_score
    if winner_id == state["bat_team_id"]:
        assert state["total_runs"] >= state["target"]
        assert state["total_runs"] - state["target"] <= 5
    else:
        assert 1 <= result["margin"] <= 3


@pytest.mark.parametrize("winner_id", [10, 20])
@pytest.mark.parametrize("natural", [
    dict(type="wide", runs=0), dict(type="noball", runs=6),
    dict(type="wicket", runs=0, wicket_type="Bowled"),
    dict(type="runs", runs=0), dict(type="runs", runs=6),
])
def test_pathological_natural_stream_still_completes_and_accounts_each_run(
        monkeypatch, winner_id, natural):
    monkeypatch.setattr(cm, "calculate_outcome", lambda **kwargs: dict(natural))
    random.seed(21)
    state = _make_state(overs=2)
    state["_planned_winner_user_id"] = winner_id
    state = finish_innings(state)
    cm.end_first_innings(state)
    state = finish_innings(state)
    result = cm.compute_result(state)
    assert result["winner"] == {10: "Team A", 20: "Team B"}[winner_id]
    assert cm.balls_bowled(state) == cm.total_balls(state)
    assert_innings_accounting(state)


@pytest.mark.parametrize("winner_id", [10, 20])
def test_chosen_outcome_survives_json_restores_between_deliveries(winner_id):
    random.seed(27)
    state = _make_state(overs=5)
    state["_planned_winner_user_id"] = winner_id
    state = finish_innings(state, restore=True)
    cm.end_first_innings(state)
    state = finish_innings(state, restore=True)
    assert state["_planned_winner_user_id"] == winner_id
    assert cm.compute_result(state)["winner"] == {10: "Team A", 20: "Team B"}[winner_id]
    assert cm.balls_bowled(state) == cm.total_balls(state)


@pytest.mark.parametrize("super_overs", [1, 2])
def test_planned_super_over_still_ties_regulation_before_chosen_winner(super_overs):
    random.seed(42)
    state = _make_state(overs=5)
    state.update(_planned_super_overs=super_overs, _planned_winner_user_id=20)
    state = finish_innings(state)
    cm.end_first_innings(state)
    state = finish_innings(state)
    assert cm.compute_result(state)["tie"]
    assert state["total_runs"] == state["inn1_runs"]
    assert cm.balls_bowled(state) == cm.total_balls(state)


@pytest.mark.parametrize("winner", [None, "absent"])
def test_normal_matches_do_not_run_the_planned_winner_controller(monkeypatch, winner):
    def unexpected(*args, **kwargs):
        pytest.fail("normal play must not condition a winner")
    monkeypatch.setattr(settings, "apply_winner_outcome", unexpected)
    random.seed(9)
    state = _make_state(overs=5)
    if winner is None:
        state["_planned_winner_user_id"] = None
    finish_innings(state)
    cm.end_first_innings(state)
    finish_innings(state)


@pytest.mark.parametrize("winner_is_batting", [False, True])
@pytest.mark.parametrize("target", [2, 3, 7, 37, 60, 180])
def test_controller_handles_extras_and_terminal_wickets_without_losing_selected_result(
        winner_is_batting, target):
    rng = random.Random(target)
    natural = itertools.cycle([
        dict(type="wicket", runs=0, batter_out=True, wicket_type="Bowled"),
        dict(type="extra", runs=6, is_extra=True, extra_type="No Ball"),
        dict(type="extra", runs=0, is_extra=True, extra_type="Wide"),
        dict(type="run", runs=6), dict(type="run", runs=0),
    ])
    runs, remaining, wickets = 0, 30, 9
    for _ in range(600):
        outcome = settings.apply_winner_outcome(
            next(natural), winner_is_batting=winner_is_batting,
            runs=runs, target=target, remaining=remaining, wickets=wickets,
            wicket_limit=10, innings_balls=30, rng=rng)
        gained, free_ball = scored(outcome)
        runs += gained
        remaining -= int(not free_ball)
        wickets += int(bool(outcome.get("batter_out") or outcome.get("type") == "wicket"))
        if remaining:
            assert runs < target, "the configured finish must remain undecided until the final ball"
            assert wickets < 10
        else:
            break
    assert remaining == 0
    if winner_is_batting:
        assert target <= runs <= target + 5
    else:
        assert 1 <= target - 1 - runs <= min(3, target - 1)


def test_one_run_chase_is_won_on_its_last_legal_ball():
    runs = 0
    rng = random.Random(6)
    for remaining in range(6, 0, -1):
        outcome = settings.apply_winner_outcome(
            dict(type="run", runs=6), winner_is_batting=True,
            runs=runs, target=1, remaining=remaining, wickets=0,
            wicket_limit=2, innings_balls=6, rng=rng)
        gained, free_ball = scored(outcome)
        assert not free_ball
        runs += gained
        assert runs == (0 if remaining > 1 else 1)


def test_defending_first_innings_cannot_finish_with_zero_runs():
    outcome = settings.apply_winner_outcome(
        dict(type="wicket", runs=0, batter_out=True, wicket_type="Bowled"),
        winner_is_batting=True, runs=0, target=None, remaining=1, wickets=9,
        wicket_limit=10, innings_balls=30, rng=random.Random(2))
    gained, _ = scored(outcome)
    assert gained >= 1


def test_first_innings_keeps_a_chosen_chasers_target_attainable():
    for outcome in [dict(type="run", runs=6),
                    dict(type="extra", runs=6, is_extra=True, extra_type="No Ball")]:
        controlled = settings.apply_winner_outcome(
            outcome, winner_is_batting=False, runs=35, target=None,
            remaining=1, wickets=0, wicket_limit=2, innings_balls=6,
            rng=random.Random(1))
        gained, _ = scored(controlled)
        assert 35 + gained <= 35


@pytest.mark.parametrize("winner_is_batting", [False, True])
def test_controller_preserves_feasible_natural_cricket_outcomes(winner_is_batting):
    outcomes = [dict(type="extra", runs=0, is_extra=True, extra_type="Wide"),
                dict(type="extra", runs=4, is_extra=True, extra_type="No Ball"),
                dict(type="extra", runs=1, is_extra=True, extra_type="LegByes"),
                dict(type="wicket", runs=0, batter_out=True, wicket_type="Bowled"),
                dict(type="run", runs=0), dict(type="run", runs=4),
                dict(type="run", runs=6)]
    for outcome in outcomes:
        assert settings.apply_winner_outcome(
            outcome, winner_is_batting=winner_is_batting,
            runs=35, target=171, remaining=96, wickets=2,
            wicket_limit=10, innings_balls=120, rng=random.Random(3)) is outcome


def test_replaced_dismissal_does_not_leave_wicket_or_extra_flags():
    outcome = settings.apply_winner_outcome(
        dict(type="wicket", runs=0, batter_out=True, wicket_type="Caught",
             disallowed_wicket="Caught", dropped_catch=True, misfield=True),
        winner_is_batting=True, runs=59, target=61, remaining=1, wickets=9,
        wicket_limit=10, innings_balls=30, rng=random.Random(4))
    assert outcome["type"] == "run"
    assert not outcome.get("batter_out")
    assert not outcome.get("is_extra")
    assert not outcome.get("wicket_type")
    assert not outcome.get("disallowed_wicket")
    assert not outcome.get("dropped_catch")
    assert not outcome.get("misfield")

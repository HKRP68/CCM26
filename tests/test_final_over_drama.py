"""Final-over delivery pacing, pressure extras and settled-batter finishes."""
import asyncio
import copy
import json
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from engine import batter_confidence, chase_model
from engine.ball_outcome import compute_weighted_prob
from handlers import cipl_play as cp
from services import cipl_match as cm, match_drama, bot_tactics
from services.match_state_store import A_PICK_BAT_APPROACH, A_PICK_CIPL_BOWLER, A_PICK_CIPL_NEW_BATSMAN
from tests.test_cipl_approach import _make_state, BASE_WEIGHTS


def ready(ball_format="T20", needed=24):
    s = _make_state(overs=20, ball_format=ball_format)
    s.update(innings=2, current_over=20, current_ball=0, total_runs=150,
             target=150 + needed, batting_approach="balanced",
             bowling_approach="balanced")
    s["current_bowler"] = cm.eligible_bowlers(s)[0]
    return s


def scripted(monkeypatch, marks, calls=None):
    marks = iter(marks)
    def outcome(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        mark = next(marks)
        if isinstance(mark, dict):
            return dict(mark)
        if mark == "W":
            return {"type": "wicket", "runs": 0, "wicket_type": "Bowled"}
        return {"type": "runs", "runs": mark}
    monkeypatch.setattr(cm, "calculate_outcome", outcome)


@pytest.mark.parametrize("ball_format", ["T20", "The100"])
def test_each_delivery_is_saved_and_json_resume_matches_whole_over(monkeypatch, ball_format):
    whole = ready(ball_format)
    paced = copy.deepcopy(whole)
    marks = [{"type": "noball", "runs": 0}, {"type": "wide"}, 4, 1, "W", 0, 2, 0]
    scripted(monkeypatch, marks)
    random.seed(321)
    expected = cm.simulate_over(whole)
    scripted(monkeypatch, marks)
    random.seed(321)
    steps = 0
    while True:
        previous = cm.balls_bowled(paced)
        summary = cm.simulate_over(paced, pause_after_delivery=True)
        steps += 1
        assert cm.balls_bowled(paced) - previous in (0, 1)
        if not summary.get("delivery_paused"):
            break
        assert not summary.get("paused")
        if steps in (1, 2):
            assert paced["free_hit"]  # a wide preserves the no-ball's free hit
        paced = json.loads(json.dumps(paced))
    assert steps == cm.balls_per_unit(paced) + 2
    assert summary == expected
    # JSON converts some ancillary tuple keys; compare the stored form.
    assert json.loads(json.dumps(paced)) == json.loads(json.dumps(whole))


def test_wicket_pick_takes_priority_over_the_delivery_pause(monkeypatch):
    s = ready()
    scripted(monkeypatch, ["W", 4])
    summary = cm.simulate_over(s, pause_on_wicket=True, pause_after_delivery=True)
    assert summary["paused"] and not summary.get("delivery_paused")
    assert cm.needs_new_batsman(s)
    incoming = cm.available_batsmen(s)[-1]
    cm.bring_in_batsman(s, incoming["roster_id"])
    summary = cm.simulate_over(s, pause_on_wicket=True, pause_after_delivery=True)
    assert summary["delivery_paused"]
    assert not cm.needs_new_batsman(s)
    assert s["bat_stats"][str(incoming["roster_id"])]["runs"] == 4


@pytest.mark.parametrize("runs,crossed,new_batter_faces", [
    (0, False, False), (0, True, True), (1, False, True), (1, True, False),
])
def test_non_striker_run_out_replaces_the_right_end_after_resume(
        monkeypatch, runs, crossed, new_batter_faces):
    from services import record_telemetry
    from services.crickidex_arena import _approach_payload
    from services.tournament_service import _collect_player_lines

    state = _make_state()
    bowler = state["current_bowler"] = cm.eligible_bowlers(state)[0]
    striker, partner = state["batting_order"][:2]
    wicket = dict(type="wicket", wicket_type="Run Out", runs=runs,
                  run_out_non_striker=True, run_out_crossed=crossed)
    scripted(monkeypatch, [wicket, 4])
    monkeypatch.setattr(cm, "_resolve_running_wicket", lambda outcome, needed: outcome)

    summary = cm.simulate_over(state, pause_on_wicket=True)
    assert summary["out_batsman"]["roster_id"] == partner["roster_id"]
    assert state["fow"][0][2] == partner["name"]
    assert state["bat_stats"][str(striker["roster_id"])]["balls"] == 1
    assert state["bat_stats"][str(striker["roster_id"])]["runs"] == runs
    assert state["bat_stats"][str(partner["roster_id"])]["balls"] == 0
    assert state["bowl_stats"][str(bowler["roster_id"])]["wickets"] == 0
    event = state["record_deliveries"][0]
    assert event["batter"] == striker["name"] and event["dismissed_batter"] == partner["name"]
    assert event["fielder"] == state["bat_stats"][str(partner["roster_id"])]["fielder"]

    state = json.loads(json.dumps(state))
    assert cm.needs_new_batsman(state)
    payload = _approach_payload(state, A_PICK_CIPL_NEW_BATSMAN, "batting")
    assert payload["outBatsman"]["name"] == partner["name"]
    incoming = cm.bring_in_batsman(state, cm.available_batsmen(state)[-1]["roster_id"])
    cm.simulate_over(state, pause_after_delivery=True)
    expected = incoming if new_batter_faces else striker
    assert state["record_deliveries"][-1]["batter"] == expected["name"]
    detail = record_telemetry.detail(state)
    assert "invalid_reason" not in record_telemetry.analyse(detail, _collect_player_lines(state))


def test_winning_completed_run_ends_play_before_a_run_out(monkeypatch):
    state = ready(needed=1)
    scripted(monkeypatch, [{"type": "wicket", "wicket_type": "Run Out", "runs": 1}])
    choices = random.choices
    monkeypatch.setattr(random, "choices", lambda population, *args, **kwargs:
                        [2] if population == (0, 1, 2) else choices(population, *args, **kwargs))
    summary = cm.simulate_over(state)
    assert summary["over_timeline"] == ["1"]
    assert state["total_runs"] == state["target"] and state["total_wickets"] == 0


def test_dot_pressure_stays_with_the_batter_across_overs(monkeypatch):
    state = _make_state()
    state["current_bowler"] = cm.eligible_bowlers(state)[0]
    streaks = []
    pressure_hook = cm._make_dot_pressure_hook

    def observe(streak, local_dots):
        streaks.append((streak, local_dots))
        return pressure_hook(streak, local_dots)

    monkeypatch.setattr(cm, "_make_dot_pressure_hook", observe)
    scripted(monkeypatch, [0] * 6 + [1, {"type": "wide"}, 4, 0])
    cm.simulate_over(state)
    state = json.loads(json.dumps(state))
    state["current_bowler"] = cm.eligible_bowlers(state)[0]
    for _ in range(4):
        cm.simulate_over(state, pause_after_delivery=True)
    assert streaks == [(n, n) for n in range(6)] + [(0, 0), (5, 0), (5, 0), (0, 0)]


def test_partnership_chemistry_ends_when_a_wicket_falls(monkeypatch):
    state = _make_state()
    state["current_bowler"] = cm.eligible_bowlers(state)[0]
    state["partnership_balls"] = 23
    calls = []
    scripted(monkeypatch, [0, "W", 4], calls)
    for _ in range(3):
        cm.simulate_over(state, pause_after_delivery=True)
    assert [call["approach_context"]["partnership_overs"] for call in calls] == [23 / 6, 4, 0]


def test_leg_byes_consume_a_free_hit_and_do_not_cost_a_maiden(monkeypatch):
    state = _make_state()
    bowler = state["current_bowler"] = cm.eligible_bowlers(state)[0]
    leg_bye = {"type": "legbye", "runs": 1}
    state["free_hit"] = True
    calls = []
    scripted(monkeypatch, [leg_bye, 0, 0, 0, 0, 0], calls)
    cm.simulate_over(state)
    assert [call["free_hit"] for call in calls] == [True, False, False, False, False, False]
    assert state["total_runs"] == state["extras_total"] == 1
    stats = state["bowl_stats"][str(bowler["roster_id"])]
    assert stats["runs"] == 0 and stats["maidens"] == 1


@pytest.mark.parametrize("mark", [4, {"type": "wide"}, {"type": "noball", "runs": 0}])
def test_winning_delivery_stops_immediately(monkeypatch, mark):
    s = ready(needed=1)
    scripted(monkeypatch, [mark])
    summary = cm.simulate_over(s, pause_after_delivery=True)
    assert cm.is_innings_over(s)
    assert len(summary["over_events"]) == 1
    assert not summary.get("delivery_paused")
    assert "over_in_progress" not in s


def test_big_ask_increases_both_free_extras_without_boosting_byes():
    normal_hook, normal_kw = match_drama.bowl_pressure_effects(0.4)
    big = match_drama.final_over_big_ask(2, 24, 6)
    big_hook, big_kw = match_drama.bowl_pressure_effects(0.4, big)
    normal = normal_hook(BASE_WEIGHTS)
    boosted = big_hook(BASE_WEIGHTS)
    normal_mass, big_mass = sum(normal.values()), sum(boosted.values())
    for i in (0, 1):
        assert (boosted["Extras"] * big_kw["extras_split"][i] / big_mass
                > normal["Extras"] * normal_kw["extras_split"][i] / normal_mass)
    assert big_kw["extras_split"][2] < normal_kw["extras_split"][2]
    assert sum(big_kw["extras_split"]) == pytest.approx(1.0)
    for args in ((1, 24, 6), (2, 24, 7), (2, 0, 6), (2, 6, 6), (2, 24, 0)):
        assert match_drama.final_over_big_ask(*args) == 0
    assert match_drama.final_over_big_ask(2, 20, 5, 5) == 1


def test_chase_outlook_prices_the_actual_free_extra_split():
    dist = dict.fromkeys(chase_model.OUTCOMES, 0.0)
    dist["Extras"] = 1.0
    cont = lambda runs, balls, wickets: (float(balls > 0), 0.0)
    assert chase_model.one_ball_outlook(cont, dist, 3, 1, 5, 0.9)[0] == pytest.approx(0.9)


def test_big_ask_extra_boost_survives_the_live_chase_controller(monkeypatch):
    captured = []
    def outcome(**kwargs):
        weights = kwargs["weight_hook"](dict(BASE_WEIGHTS))
        captured.append((weights, kwargs["extras_split"]))
        return {"type": "runs", "runs": 0}
    monkeypatch.setattr(cm, "calculate_outcome", outcome)
    random.seed(88)
    cm.simulate_over(ready(), pause_after_delivery=True)
    monkeypatch.setattr(match_drama, "final_over_big_ask", lambda *args: 0.0)
    random.seed(88)
    cm.simulate_over(ready(), pause_after_delivery=True)
    (big, big_split), (normal, normal_split) = captured
    for idx in (0, 1):
        assert (big["Extras"] * big_split[idx] / sum(big.values())
                > normal["Extras"] * normal_split[idx] / sum(normal.values()))


def test_low_rated_batter_gains_boundary_confidence_after_25_balls():
    def weights(balls):
        return {key: compute_weighted_prob(key, value, 50, 80, 75, "Even", "Fast",
                                           {}, batter_runs=20, balls_faced=balls)
                for key, value in BASE_WEIGHTS.items()}
    before, after = weights(24), weights(25)
    for key in ("Four", "Six"):
        assert after[key] / sum(after.values()) > before[key] / sum(before.values())
    assert batter_confidence.experience_bonus(59, 25) == 1
    assert batter_confidence.experience_bonus(60, 25) == 0
    assert batter_confidence.experience_bonus(45, 40) == 12
    assert batter_confidence.experience_bonus(45, 200) == 12
    effective = [r + batter_confidence.experience_bonus(r, 40) for r in range(101)]
    assert effective == sorted(effective)
    s = ready()
    bat = s["batting_order"][s["striker_idx"]]
    bat["bat_rating"] = 50
    stats = s["bat_stats"][str(bat["roster_id"])]
    stats.update(balls=24, runs=20)
    edge_before = bot_tactics.match_up_edge(s)
    chance_before = chase_model.target_probabilities(**cm._chase_inputs(s))["win"]
    stats["balls"] = 25
    assert bot_tactics.match_up_edge(s) > edge_before
    assert chase_model.target_probabilities(**cm._chase_inputs(s))["win"] > chance_before
    assert bat["bat_rating"] == 50


@pytest.mark.parametrize("needed", [1, 2, 3, 4])
def test_final_ball_prices_the_set_striker_and_can_still_be_lost(needed):
    state = ready(needed=needed)
    state["current_ball"] = 5
    inputs = cm._chase_inputs(state)
    fresh = chase_model.target_probabilities(**inputs)
    assert chase_model.target_probabilities(**dict(inputs, non_striker_balls=40)) == fresh
    settled = chase_model.target_probabilities(**dict(inputs, striker_balls=40))
    assert settled["win"] > fresh["win"]
    assert settled["lose"] > 0
    assert settled["win"] + settled["tie"] + settled["lose"] == pytest.approx(100)


@pytest.mark.parametrize("fmt,prior", [("T20", (14, 16, 18)), ("The100", (16, 17, 19))])
def test_bowler_fatigue_recovers_after_a_break_in_the_spell(fmt, prior):
    state = ready(ball_format=fmt)
    bowler = state["current_bowler"]
    rid = str(bowler["roster_id"])
    state["bowl_stats"][rid]["balls"] = 3 * cm.balls_per_unit(state)
    state["approach_log"] = [{"over": over, "bowler_rid": rid} for over in prior]
    assert cm.approach_context(state)["bowler_over_index"] == 4
    tired = chase_model.target_probabilities(**cm._chase_inputs(state))["win"]
    state["approach_log"] = [{"over": over, "bowler_rid": rid} for over in (12, 14, 16)]
    assert cm.approach_context(state)["bowler_over_index"] == 1
    rested = chase_model.target_probabilities(**cm._chase_inputs(state))["win"]
    assert tired > rested


def test_batter_matchup_changes_the_bot_and_chase_estimates():
    state = ready()
    batter = state["batting_order"][state["striker_idx"]]
    batter.update(bat_rating=80, vs_pace=85, vs_spin=74)
    pace = dict(state["current_bowler"], bowl_style="Fast")
    spin = dict(pace, bowl_style="Leg Spinner")
    assert bot_tactics.match_up_edge(state, pace) > bot_tactics.match_up_edge(state, spin)
    state["current_bowler"] = pace
    assert cm._chase_inputs(state)["striker_bat"] == 85
    state["current_bowler"] = spin
    assert cm._chase_inputs(state)["striker_bat"] == 74


def test_long_innings_fatigue_moves_boundary_weight_to_running():
    assert batter_confidence.rotate_after_long_innings(BASE_WEIGHTS, 30) == BASE_WEIGHTS
    settled = batter_confidence.rotate_after_long_innings(BASE_WEIGHTS, 40)
    tired = batter_confidence.rotate_after_long_innings(BASE_WEIGHTS, 55)
    assert tired["Six"] < settled["Six"] < BASE_WEIGHTS["Six"]
    assert tired["Single"] > settled["Single"] > BASE_WEIGHTS["Single"]
    assert tired["Wicket"] == BASE_WEIGHTS["Wicket"]
    assert tired["Extras"] == BASE_WEIGHTS["Extras"]
    assert sum(tired.values()) == pytest.approx(sum(BASE_WEIGHTS.values()))
    urgent = batter_confidence.rotate_after_long_innings(BASE_WEIGHTS, 55, required_rate=10)
    assert urgent == BASE_WEIGHTS


def test_fallback_finishing_edge_preserves_all_outcomes():
    s = ready(needed=4)
    s["current_ball"] = 5
    stats = s["bat_stats"][str(s["batting_order"][0]["roster_id"])]
    stats["balls"] = 39
    assert cm._make_set_finisher_hook(s) is None
    stats.update(balls=40, out=True)
    assert cm._make_set_finisher_hook(s) is None
    stats["out"] = False
    weights = cm._make_set_finisher_hook(s)(BASE_WEIGHTS)
    assert weights["Four"] > BASE_WEIGHTS["Four"]
    assert weights["Dot"] == BASE_WEIGHTS["Dot"]
    assert weights["Wicket"] == BASE_WEIGHTS["Wicket"]
    assert weights["Extras"] == BASE_WEIGHTS["Extras"]
    s["target"] += 1
    assert cm._make_set_finisher_hook(s) is None
    s.update(innings=1, target=None)
    assert cm._make_set_finisher_hook(s) is None


@pytest.mark.parametrize("app", [False, True])
def test_bowler_prompt_only_asks_captain_to_pick(monkeypatch, app):
    state = _make_state(overs=20)
    state.update(chat_id=-901, bowl_user_tg=200, bat_user_tg=300,
                 play_mode="app" if app else "chat", current_over=5,
                 total_runs=62, partnership_runs=62, partnership_balls=24,
                 over_runs=[18, 18, 14, 12])
    assert len(cm.eligible_bowlers(state)) >= 2
    send = AsyncMock()
    context = SimpleNamespace(bot_data={}, bot=SimpleNamespace(send_message=send))
    public = AsyncMock()
    save = AsyncMock()
    monkeypatch.setattr(cp, "_new_action_message", public)
    monkeypatch.setattr(cp, "_ss", save)
    monkeypatch.setattr(cp, "_arm_timer", lambda *args: None)
    monkeypatch.setattr(cp, "_delete_prev_over", AsyncMock())

    asyncio.run(cp._prompt_bowler(context, 1, state))
    send.assert_not_awaited()
    public.assert_awaited_once()
    _, _, text, rows = public.await_args.args
    assert "pick your bowler" in text
    assert "Bowling advice" not in text
    if app:
        assert "in the Mini App" in text and rows is None
    else:
        assert [button.callback_data for row in rows for button in row] == [
            f"cipl_bowler_1_{bowler['roster_id']}" for bowler in cm.eligible_bowlers(state)
        ]
    assert state["prompt_delivered"]
    assert "bowling_advice" not in state
    save.assert_awaited_once_with(context, 1, state, next_action=A_PICK_CIPL_BOWLER)


def test_live_reveals_save_before_send_and_delete_previous_ball(monkeypatch):
    s = ready()
    calls, saved, sent, deleted = [], [], [], []
    scripted(monkeypatch, [{"type": "noball", "runs": 0}, {"type": "wide"},
                          4, 0, 2, 0, 0, 0], calls)
    async def save(context, mid, state, **kwargs):
        saved.append((len(calls), json.loads(json.dumps(state)), kwargs))
    async def send(chat_id, text, **kwargs):
        assert saved[-1][0] == len(calls)
        sent.append(text)
        return SimpleNamespace(message_id=len(sent))
    async def delete(chat_id, message_id):
        deleted.append(message_id)
    monkeypatch.setattr(cp, "_ss", save)
    monkeypatch.setattr(cp, "FINAL_OVER_BALL_DELAY", 0)
    context = SimpleNamespace(bot=SimpleNamespace(send_message=send, delete_message=delete))
    summary = asyncio.run(cp._simulate_live_final_over(context, 1, s, {}))
    assert len(calls) == len(saved) == len(sent) == 8
    assert deleted == list(range(1, 8))
    assert "FREE HIT" in sent[0] and "FREE HIT" in sent[1]
    assert "from <b>6</b> balls" in sent[0] and "from <b>6</b> balls" in sent[1]
    assert all(item[2]["next_action"] == A_PICK_BAT_APPROACH for item in saved)
    assert len(summary["over_timeline"]) == 8


def test_failed_reveal_keeps_playing_and_failed_save_never_reveals(monkeypatch):
    s = ready()
    scripted(monkeypatch, [0] * 6)
    monkeypatch.setattr(cp, "_ss", AsyncMock())
    monkeypatch.setattr(cp, "FINAL_OVER_BALL_DELAY", 0)
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("offline"))))
    summary = asyncio.run(cp._simulate_live_final_over(context, 1, s, {}))
    assert len(summary["over_events"]) == 6
    assert cm.is_innings_over(s)
    s = ready()
    scripted(monkeypatch, [0])
    monkeypatch.setattr(cp, "_ss", AsyncMock(side_effect=RuntimeError("stale save")))
    reveal = AsyncMock()
    monkeypatch.setattr(cp, "_reveal_final_ball", reveal)
    with pytest.raises(RuntimeError, match="stale save"):
        asyncio.run(cp._simulate_live_final_over(context, 1, s, {}))
    reveal.assert_not_awaited()


def test_live_wicket_saves_the_batsman_pick_and_stops(monkeypatch):
    s = ready()
    scripted(monkeypatch, ["W"])
    save = AsyncMock()
    monkeypatch.setattr(cp, "_ss", save)
    monkeypatch.setattr(cp, "_reveal_final_ball", AsyncMock())
    monkeypatch.setattr(cp, "FINAL_OVER_BALL_DELAY", 0)
    summary = asyncio.run(cp._simulate_live_final_over(None, 1, s, {"pause_on_wicket": True}))
    assert summary["paused"]
    assert save.await_args.kwargs["next_action"] == A_PICK_CIPL_NEW_BATSMAN


@pytest.mark.parametrize("needed,marks", [(1, [4]), (24, [0] * 6)])
def test_deciding_delivery_has_no_extra_pause(monkeypatch, needed, marks):
    s = ready(needed=needed)
    scripted(monkeypatch, marks)
    sleep = AsyncMock()
    monkeypatch.setattr(cp.asyncio, "sleep", sleep)
    monkeypatch.setattr(cp, "_ss", AsyncMock())
    monkeypatch.setattr(cp, "_reveal_final_ball", AsyncMock())
    asyncio.run(cp._simulate_live_final_over(None, 1, s, {}))
    assert cm.is_innings_over(s)
    # Keep the delay before each ball, without a further pause after the result.
    assert sleep.await_count == len(marks)
    assert all(call.args == (cp.FINAL_OVER_BALL_DELAY,)
               for call in sleep.await_args_list)


def test_nonterminal_wicket_keeps_its_reveal_pause(monkeypatch):
    s = ready()
    scripted(monkeypatch, ["W"])
    sleep = AsyncMock()
    monkeypatch.setattr(cp.asyncio, "sleep", sleep)
    monkeypatch.setattr(cp, "_ss", AsyncMock())
    monkeypatch.setattr(cp, "_reveal_final_ball", AsyncMock())
    summary = asyncio.run(cp._simulate_live_final_over(
        None, 1, s, {"pause_on_wicket": True}))
    assert summary["paused"]
    assert not cm.is_innings_over(s)
    assert sleep.await_count == 2


def test_only_the_live_final_over_uses_pacing():
    s = ready()
    assert cp._live_final_over(s)
    s["current_over"] = 19
    assert not cp._live_final_over(s)
    s.update(current_over=20, innings=1, target=None)
    assert not cp._live_final_over(s)


def test_run_over_drives_the_live_finale_then_finishes_once(monkeypatch):
    from services import league_records, bot_captain
    s = ready()
    scripted(monkeypatch, [0] * 6)
    simulate = AsyncMock(wraps=cp._simulate_live_final_over)
    finish = AsyncMock()
    save = AsyncMock()
    monkeypatch.setattr(cp, "_simulate_live_final_over", simulate)
    monkeypatch.setattr(cp, "_ss", save)
    monkeypatch.setattr(cp, "_edit_action_message", AsyncMock())
    monkeypatch.setattr(cp, "_reveal_final_ball", AsyncMock())
    monkeypatch.setattr(cp, "_post_tracked", AsyncMock())
    monkeypatch.setattr(cp, "_finish_innings", finish)
    monkeypatch.setattr(cp, "_cancel_timer", lambda *args: None)
    monkeypatch.setattr(cp, "_human_picks_batsman", lambda state: False)
    monkeypatch.setattr(cp, "_between_overs_row", lambda *args: None)
    monkeypatch.setattr(cp, "_fire_milestones_async", lambda *args: None)
    monkeypatch.setattr(cp, "_announce_drama_async", lambda *args: None)
    monkeypatch.setattr(cp, "FINAL_OVER_BALL_DELAY", 0)
    monkeypatch.setattr(league_records, "announce", AsyncMock())
    monkeypatch.setattr(bot_captain, "observe_over", lambda *args: None)
    asyncio.run(cp._run_over(None, 1, s))
    simulate.assert_awaited_once()
    finish.assert_awaited_once()
    assert save.await_count == 7  # each of six deliveries, then the final summary state
    assert cm.balls_bowled(s) == 120
    assert "over_milestones_before" not in s
    cp._post_tracked.assert_not_awaited()  # result has priority over cosmetic over posts


def test_winning_delivery_has_no_extra_suspense_pause(monkeypatch):
    s = ready(needed=1)
    scripted(monkeypatch, [1])
    sleep = AsyncMock()
    monkeypatch.setattr(cp.asyncio, 'sleep', sleep)
    monkeypatch.setattr(cp, '_ss', AsyncMock())
    monkeypatch.setattr(cp, '_reveal_final_ball', AsyncMock())
    asyncio.run(cp._simulate_live_final_over(None, 1, s, {}))
    assert cm.is_innings_over(s)
    # Pace the delivery itself, then return immediately once the winner is known.
    assert sleep.await_count == 1

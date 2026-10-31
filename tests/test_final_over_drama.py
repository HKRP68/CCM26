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
from services.match_state_store import A_PICK_BAT_APPROACH, A_PICK_CIPL_NEW_BATSMAN
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


@pytest.mark.parametrize("active_idx", [0, 1])
@pytest.mark.parametrize("needed", [1, 2, 3, 4])
def test_surviving_40_ball_batter_cannot_lose_a_tiny_final_ask(active_idx, needed):
    original = ready(needed=needed)
    original["current_ball"] = 5
    player = original["batting_order"][active_idx]
    original["bat_stats"][str(player["roster_id"])].update(balls=40, runs=55)
    assert chase_model.target_probabilities(**cm._chase_inputs(original))["lose"] == 0
    for seed in range(8):
        s = copy.deepcopy(original)
        random.seed(seed)
        cm.simulate_over(s)
        assert s["total_runs"] >= s["target"] - 1
        assert cm.is_innings_over(s)


def test_finishing_guarantee_excludes_fresh_dismissed_and_large_asks():
    s = ready(needed=4)
    s["current_ball"] = 5
    stats = s["bat_stats"][str(s["batting_order"][0]["roster_id"])]
    stats["balls"] = 39
    assert cm._make_set_finisher_hook(s) is None
    stats.update(balls=40, out=True)
    assert cm._make_set_finisher_hook(s) is None
    stats["out"] = False
    s["target"] += 1
    assert cm._make_set_finisher_hook(s) is None
    s.update(innings=1, target=None)
    assert cm._make_set_finisher_hook(s) is None


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

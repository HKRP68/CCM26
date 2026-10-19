"""Recent batter confidence in the actual over-by-over simulation loop."""
import copy
import json
import math
import random

import pytest

from engine import batter_confidence as confidence
from services import cipl_match as cm, bot_tactics
from tools.pitch_calibration import make_xi


BASE = {"Dot": 35., "Single": 30., "Double": 8., "Three": 1.,
        "Four": 15., "Six": 5., "Wicket": 4., "Extras": 2.}


def played(marks):
    stats = {"balls": 0, "confidence": 50.}
    for runs in marks:
        stats["balls"] += 1
        confidence.record(stats, runs)
    return stats


def state():
    s = cm.build_cipl_state(1, 20, 10, 20, 111, 222,
                           make_xi("A", 1), make_xi("B", 101),
                           "A", "B", -1, "Even")
    s["current_bowler"] = cm.eligible_bowlers(s)[0]
    s["batting_approach"] = "balanced"
    s["bowling_approach"] = "balanced"
    return s


def script(monkeypatch, marks, captured=None):
    marks = iter(marks)
    def outcome(**kw):
        if captured is not None:
            captured.append(kw["weight_hook"](dict(BASE)))
        mark = next(marks)
        if isinstance(mark, dict):
            return mark
        if mark == "W":
            return {"type": "wicket", "runs": 0, "how": "Bowled"}
        return {"type": "runs", "runs": mark}
    monkeypatch.setattr(cm, "calculate_outcome", outcome)


def test_sequence_changes_confidence_without_changing_runs_or_experience():
    # Same total, balls faced and boundaries; different recent execution.
    hot = played([0, 0, 0, 0, 4, 6])
    cold = played([4, 6, 0, 0, 0, 0])
    assert confidence.value(hot) > confidence.value(cold)
    high = confidence.apply(BASE, hot)
    low = confidence.apply(BASE, cold)
    assert high["Four"] + high["Six"] > low["Four"] + low["Six"]
    assert high["Wicket"] < low["Wicket"]


def test_neutral_and_old_saved_matches_are_noops():
    assert confidence.apply(BASE, {}) is BASE
    assert confidence.apply(BASE, {"balls": 20, "confidence": 50}) is BASE
    assert confidence.value({"runs": 90, "balls": 40}) == 50
    assert confidence.label({}) == "Steady"


def test_effect_is_bounded_preserves_extras_and_random_stream():
    before_rng = random.getstate()
    for number in range(101):
        stats = {"balls": 50, "confidence": number}
        out = confidence.apply(BASE, stats)
        assert sum(out.values()) == pytest.approx(sum(BASE.values()))
        assert out["Extras"] == BASE["Extras"]
        assert all(math.isfinite(x) and x > 0 for x in out.values())
        assert all(0.95 <= out[k] / v <= 1.05 for k, v in BASE.items())
    assert random.getstate() == before_rng
    assert BASE["Dot"] == 35


def test_a_long_hot_streak_cannot_snowball_but_can_lose_confidence():
    stats = played([6] * 200)
    assert 50 < confidence.value(stats) <= 100
    peak = confidence.apply(BASE, stats)
    for _ in range(100):
        confidence.record(stats, 0)
    assert confidence.value(stats) < 40
    low = confidence.apply(BASE, stats)
    assert peak["Four"] > low["Four"]
    assert confidence.label(stats) == "Tied down"


def test_first_balls_do_not_erase_existing_settling():
    fresh = confidence.apply(BASE, {"balls": 1, "confidence": 80})
    later = confidence.apply(BASE, {"balls": 10, "confidence": 80})
    assert fresh["Four"] < later["Four"]


def test_each_batter_keeps_their_own_rhythm_after_strike_changes(monkeypatch):
    s = state()
    a, b = (str(p["roster_id"]) for p in s["batting_order"][:2])
    script(monkeypatch, [1, 4, 0, 0, 0, 0])
    cm.simulate_over(s)
    assert s["bat_stats"][a]["confidence"] == played([1])["confidence"]
    assert s["bat_stats"][b]["confidence"] == played([4, 0, 0, 0, 0])["confidence"]
    assert s["total_runs"] == 5
    assert s["bat_stats"][a]["balls"] == 1
    assert s["bat_stats"][b]["balls"] == 5


def test_extras_dont_move_confidence_and_hooks_react_inside_the_over(monkeypatch):
    s = state()
    rid = str(s["batting_order"][0]["roster_id"])
    captured = []
    script(monkeypatch, [4, 0, {"type": "wide"}, {"type": "noball", "runs": 0},
                        {"type": "legbye", "runs": 2}, 4, 0, 0], captured)
    cm.simulate_over(s)
    assert s["bat_stats"][rid]["confidence"] == played([4, 0, 4, 0, 0])["confidence"]
    assert s["bat_stats"][rid]["balls"] == 6
    assert len(captured) == 8
    assert s["total_runs"] == 12  # 8 off bat + WD + NB + 2 leg-byes
    assert not s["free_hit"]


def test_hook_reads_the_updated_confidence_on_every_ball(monkeypatch):
    s = state()
    seen = []
    original = confidence.apply
    def capture(weights, stats):
        seen.append(confidence.value(stats))
        return original(weights, stats)
    monkeypatch.setattr(confidence, "apply", capture)
    script(monkeypatch, [4, 0, 0, 0, 0, 0], [])
    cm.simulate_over(s)
    assert seen[:3] == [50., 58., 50.4]
    assert len(seen) == 6


def test_mid_over_pause_and_json_restore_match_uninterrupted_play(monkeypatch):
    whole = state()
    interrupted = copy.deepcopy(whole)
    marks = [4, "W", 0, 0, 4, 0]
    script(monkeypatch, marks)
    random.seed(944)
    cm.simulate_over(whole)
    script(monkeypatch, marks)
    random.seed(944)
    result = cm.simulate_over(interrupted, pause_on_wicket=True)
    assert result["paused"]
    interrupted = json.loads(json.dumps(interrupted))
    cm.bring_in_batsman(interrupted)
    cm.simulate_over(interrupted, pause_on_wicket=True)
    assert interrupted["bat_stats"] == whole["bat_stats"]
    assert interrupted["total_runs"] == whole["total_runs"]
    old = str(whole["batting_order"][0]["roster_id"])
    incoming = str(whole["batting_order"][2]["roster_id"])
    assert whole["bat_stats"][old]["confidence"] == 58
    assert whole["bat_stats"][incoming]["confidence"] != 58


def test_confidence_is_archived_then_reset_for_the_chase(monkeypatch):
    s = state()
    rid = str(s["batting_order"][0]["roster_id"])
    script(monkeypatch, [4, 4, 4, 4, 4, 4])
    cm.simulate_over(s)
    before = s["bat_stats"][rid]["confidence"]
    assert before > 60
    cm.end_first_innings(s)
    assert s["inn1_bat_stats"][rid]["confidence"] == before
    assert all(bs["confidence"] == 50 for bs in s["bat_stats"].values())


def test_hundred_records_five_legal_balls_not_six(monkeypatch):
    s = state()
    s["ball_format"] = "The100"
    rid = str(s["batting_order"][0]["roster_id"])
    script(monkeypatch, [0, 0, 0, 0, 0])
    cm.simulate_over(s)
    assert s["bat_stats"][rid]["confidence"] == played([0] * 5)["confidence"]
    assert s["bat_stats"][rid]["balls"] == 5


def test_ai_projection_uses_the_same_batter_confidence():
    s = state()
    rid = str(s["batting_order"][0]["roster_id"])
    s["bat_stats"][rid].update(balls=20, runs=25, confidence=80)
    hot = bot_tactics.ball_probabilities(s)
    s["bat_stats"][rid]["confidence"] = 20
    cold = bot_tactics.ball_probabilities(s)
    assert hot["Four"] + hot["Six"] > cold["Four"] + cold["Six"]
    assert hot["Wicket"] < cold["Wicket"]
    assert hot["Extras"] == pytest.approx(cold["Extras"])


def test_ai_cache_is_invalidated_when_only_confidence_changes():
    s = state()
    rid = str(s["batting_order"][0]["roster_id"])
    s["bat_stats"][rid].update(balls=20, runs=25, confidence=80)
    key = bot_tactics._fingerprint(s, None)
    s["bat_stats"][rid]["confidence"] = 20
    assert bot_tactics._fingerprint(s, None) != key


def test_rhythm_is_exposed_on_the_approach_batter_card():
    from services.crickidex_arena import _card
    s = state()
    p = s["batting_order"][0]
    s["bat_stats"][str(p["roster_id"])]["confidence"] = 72
    card = _card(s, p, "bat")
    assert card["stats"]["confidence"] == 72
    assert card["stats"]["confidenceLabel"] == "In rhythm"


def test_cluster_guard_keeps_a_fourth_wicket_possible():
    guarded = cm._make_wicket_cluster_hook(3)(BASE)
    assert 0 < guarded["Wicket"] < BASE["Wicket"]

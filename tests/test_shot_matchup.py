"""Technique controls must affect cricket without changing the legacy flow."""
import copy
import itertools
import math
import random

import pytest

from engine import shot_matchup
from services import probability_engine as pe
from services.bowling_service import AVAILABLE_SHOTS, WEBAPP_SHOTS


def shaped(**choices):
    probs = dict(pe.BASE)
    feedback = shot_matchup.apply(probs, shot=choices.pop("shot", "Drive"), **choices)
    return probs, feedback


def test_auto_is_exact_noop_including_rng(monkeypatch):
    monkeypatch.setattr(pe, "_get_shot_mods", lambda s: pe.SHOT_MODS.get(s, {}))
    from services import config_service
    monkeypatch.setattr(config_service, "get_config", lambda: {})

    def sequence():
        random.seed(512)
        balls = [pe.calculate_outcome("Fast", "Right", "Inswing", "Good", "Even",
                                     4, 20, "Drive", 80, 80) for _ in range(300)]
        return balls, random.getstate()

    current = sequence()
    monkeypatch.setattr(shot_matchup, "apply", lambda *a, **kw: None)
    assert sequence() == current


@pytest.mark.parametrize("length,shot,good,bad", [
    ("Full Length", "Drive", "front", "back"),
    ("Bouncer", "Pull", "back", "advance"),
    ("Back of Length", "Cut", "back", "front"),
    ("Yorker", "Defend", "front", "advance"),
])
def test_matching_footwork_improves_scoring_and_safety(length, shot, good, bad):
    a, _ = shaped(length=length, shot=shot, footwork=good)
    b, _ = shaped(length=length, shot=shot, footwork=bad)
    assert a["4"] + a["6"] > b["4"] + b["6"]
    assert a["W"] < b["W"]


def test_ground_vs_lofted_has_real_tradeoff():
    ground, _ = shaped(stroke="ground")
    lofted, _ = shaped(stroke="lofted")
    assert lofted["6"] > ground["6"]
    assert lofted["W"] > ground["W"]
    assert ground["1"] > lofted["1"]


def test_intent_trades_safety_for_boundaries():
    defensive, _ = shaped(intent="defensive")
    attacking, _ = shaped(intent="attacking")
    assert attacking["4"] + attacking["6"] > defensive["4"] + defensive["6"]
    assert attacking["W"] > defensive["W"]


def test_line_matches_shot_and_wider_line_risks_wides():
    off, _ = shaped(shot="Cut", line="off")
    leg, _ = shaped(shot="Cut", line="leg")
    middle, _ = shaped(shot="Cut", line="middle")
    assert off["4"] > leg["4"]
    assert off["wide"] > middle["wide"]
    off_flick, _ = shaped(shot="Flick", line="off")
    leg_flick, _ = shaped(shot="Flick", line="leg")
    assert leg_flick["4"] > off_flick["4"]


def test_spin_sweep_matchup():
    spin, _ = shaped(shot="Sweep", footwork="front", is_spin=True)
    pace, _ = shaped(shot="Sweep", footwork="front", is_spin=False)
    assert spin["4"] > pace["4"]
    assert spin["W"] < pace["W"]


def test_choices_stay_finite_preserve_mass_and_do_not_invent_batter_extras():
    for shot, length, footwork, stroke, intent in itertools.product(
            WEBAPP_SHOTS, (None, "Full", "Yorker", "Bouncer", "Back of Length"),
            shot_matchup.FOOTWORK, shot_matchup.STROKES, shot_matchup.INTENTS):
        probs, _ = shaped(shot=shot, length=length, footwork=footwork,
                          stroke=stroke, intent=intent)
        assert sum(probs.values()) == pytest.approx(sum(pe.BASE.values()))
        assert all(math.isfinite(v) and v > 0 for v in probs.values())
        for extra in ("wide", "noball", "legbye"):
            assert probs[extra] == pe.BASE[extra]


@pytest.mark.parametrize("bad", [[], {}, True, 2, "unknown", "Front Foot"])
def test_invalid_choices_rejected(bad):
    with pytest.raises(ValueError):
        shot_matchup.validate_choice(bad, shot_matchup.FOOTWORK, "footwork")


def test_advanced_shots_have_probability_profiles_and_keep_core_indices():
    assert WEBAPP_SHOTS[:len(AVAILABLE_SHOTS)] == AVAILABLE_SHOTS
    assert len(WEBAPP_SHOTS) == len(set(WEBAPP_SHOTS))
    assert set(WEBAPP_SHOTS) <= pe.SHOT_MODS.keys()


@pytest.fixture
def live(monkeypatch):
    from services import match_webapp_service as mws
    import handlers.match as bm

    bat = {"roster_id": -1, "name": "Batter", "bat_rating": 80, "bat_hand": "Right"}
    partner = {"roster_id": -2, "name": "Partner", "bat_rating": 80}
    bowler = {"roster_id": -3, "name": "Bowler", "bowl_style": "Fast",
              "bowl_hand": "Right", "bowl_rating": 80}
    state = {
        "bat_team_id": 1, "bowl_team_id": 2,
        "batting_order": [bat, partner], "bat_xi": [bat, partner], "bowl_xi": [bowler],
        "striker_idx": 0, "non_striker_idx": 1, "next_batsman_idx": 2,
        "current_bowler": bowler, "bat_stats": {}, "bowl_stats": {},
        "total_runs": 0, "total_wickets": 0, "current_over": 1, "current_ball": 0,
        "innings": 1, "overs": 5, "wicket_limit": 10, "pitch_type": "Even",
        "timeline": [], "over_runs": [], "partnership_runs": 0, "partnership_balls": 0,
        "current_delivery": "Inswing Full", "manual_batsman": True,
    }
    store = {"action": mws.A_PICK_DELIVERY, "saves": 0}
    monkeypatch.setattr(mws.mwa, "get_state", lambda mid: state)
    monkeypatch.setattr(mws.mwa, "get_next_action", lambda mid: store["action"])
    def save(mid, st, next_action=None, **kw):
        store["action"] = next_action
        store["saves"] += 1
    monkeypatch.setattr(mws.mwa, "save_state", save)
    monkeypatch.setattr(bm, "_form_mod_for", lambda *a: 0)
    monkeypatch.setattr(bm, "_chem_mod_for", lambda *a: 0)
    monkeypatch.setattr(bm, "_ball_traits", lambda *a: [])
    monkeypatch.setattr(bm, "_fielding_quality_for", lambda *a: None)
    monkeypatch.setattr(bm, "_chem_bond_for", lambda *a: 0)
    monkeypatch.setattr(bm, "_maybe_pick_commentary", lambda *a: None)
    monkeypatch.setattr(mws, "_append_commentary_log", lambda *a: None)
    monkeypatch.setattr(pe, "_get_shot_mods", lambda s: pe.SHOT_MODS.get(s, {}))
    from services import config_service
    monkeypatch.setattr(config_service, "get_config", lambda: {})
    return mws, bm, state, store


def test_api_to_shared_engine_scores_and_clears_choices(live, monkeypatch):
    mws, bm, state, store = live
    ok, _, info = mws.set_delivery_action(995123, 2, "Inswing Full", line="off")
    assert ok and info["line"] == "off"
    assert store["action"] == mws.A_PICK_SHOT
    # Fix the dice after the execution duel: the precision model still runs.
    monkeypatch.setattr(random, "random", lambda: 0.70)
    ok, res, info = mws.set_shot_action(995123, 1, "Off Drive", footwork="front",
                                      stroke="ground", intent="balanced")
    assert ok
    assert info["shot"] == "Off Drive"
    assert state["last_ball"]["shot_matchup"]["quality"] == "good"
    assert state["current_ball"] == 1
    assert state["total_runs"] == res["runs"]
    assert state["bat_stats"]["-1"]["balls"] == 1
    assert not ({"delivery_plan", "batting_plan"} & state.keys())
    assert store["saves"] == 2
    assert store["action"] == mws.A_PICK_DELIVERY


def test_spin_length_reaches_engine_once(live, monkeypatch):
    mws, bm, state, store = live
    state["current_bowler"]["bowl_style"] = "Off Spinner"
    captured = []
    def capture(*args, **kw):
        captured.append((args, kw))
        return {"type": "runs", "runs": 0}
    monkeypatch.setattr(pe, "calculate_outcome", capture)
    assert mws.set_delivery_action(995123, 2, "Off Break", spin_length="short")[0]
    assert mws.set_shot_action(995123, 1, "Pull", footwork="back")[0]
    assert captured[0][0][3] == "Short of Length"
    assert captured[0][1]["footwork"] == "back"
    bm._calc(state, state["batting_order"][0], state["current_bowler"], "Drive", "Off Break")
    assert captured[1][0][3] is None
    assert captured[1][1]["footwork"] is None


@pytest.mark.parametrize("actor,line,spin_length", [
    (1, "off", None), (2, "invalid", None), (2, {}, None), (2, None, "short"),
])
def test_bad_bowling_action_cannot_mutate_state(live, actor, line, spin_length):
    mws, _, state, store = live
    before = copy.deepcopy(state)
    assert not mws.set_delivery_action(995123, actor, "Inswing Full", line=line,
                                       spin_length=spin_length)[0]
    assert state == before
    assert store["saves"] == 0


@pytest.mark.parametrize("actor,footwork,stroke,intent", [
    (2, "front", None, None), (1, {}, None, None),
    (1, None, "invalid", None), (1, None, None, True),
])
def test_bad_batting_action_cannot_mutate_state(live, actor, footwork, stroke, intent):
    mws, _, state, store = live
    store["action"] = mws.A_PICK_SHOT
    before = copy.deepcopy(state)
    assert not mws.set_shot_action(995123, actor, "Drive", footwork=footwork,
                                  stroke=stroke, intent=intent)[0]
    assert state == before
    assert store["saves"] == 0


def test_precision_choices_preserve_free_hit_rules(live):
    _, _, _, _ = live
    random.seed(177)
    for _ in range(1500):
        outcome = pe.calculate_outcome("Fast", "Right", "Inswing", "Bouncer", "Even",
                                       19, 20, "Slog", 80, 80, free_hit=True,
                                       footwork="advance", stroke="lofted", intent="attacking")
        assert outcome["type"] != "wicket" or outcome["how"] == "Run Out"

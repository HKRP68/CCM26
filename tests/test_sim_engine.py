"""Conditions Engine v4 (engine/sim) — the rules, the determinism, the balance.

Unit tests pin each formula from the spec to its config value; the match tests
check that a seed reproduces a match and that the formats' flow (DLS, follow-on,
innings defeat, new ball) is sound; the live-hook tests check it is bounded and
that the CIPL/letsplay engine still runs with it on.
"""

import json
import random
import statistics
from dataclasses import replace

import pytest

from engine.sim import (ball_aging, config as sim_config, drama, factors, hook, outcome,
                        pitch, rain_dls, situation, stadium, time_of_day, weather)
from engine.sim.match import MatchSetup, Team, simulate_match
from engine.sim.models import BallState, Conditions, MatchTime, Stadium, Weather
from engine.sim.player_adapter import is_spinner, to_batter, to_bowler
from engine.sim.rng import SimRng


@pytest.fixture(scope="module")
def cfg():
    return sim_config.build(local_path="")


# ── config ──────────────────────────────────────────────────────────────

def test_config_has_every_pitch_and_key(cfg):
    for name in ("Dry", "Dusty", "Bouncy", "Flat", "Hard", "Even", "Green"):
        prof = cfg["pitches"][name]
        for k in sim_config.PITCH_KEYS:
            assert sim_config.PITCH_MIN <= prof[k] <= sim_config.PITCH_MAX


def test_config_clamps_out_of_range_values():
    c = sim_config.build(override={"pitches": {"Flat": {"battingEase": 9.0}},
                                   "drama": {"dramaSlider": 250}}, local_path="")
    assert c["pitches"]["Flat"]["battingEase"] == 3.0
    assert c["drama"]["dramaSlider"] == 100
    assert any("battingEase" in w for w in c["_warnings"])


def test_deep_merge_keeps_untouched_keys():
    merged = sim_config.deep_merge({"a": {"x": 1, "y": 2}}, {"a": {"y": 5}})
    assert merged == {"a": {"x": 1, "y": 5}}


def test_flat_is_a_road_and_even_is_balanced(cfg):
    flat, even = cfg["pitches"]["Flat"], cfg["pitches"]["Even"]
    assert flat["battingEase"] > even["battingEase"]
    for k in ("paceHelp", "seamHelp", "swingHelp", "spinTurn"):
        assert flat[k] < 1.0 <= even[k]


# ── weather ─────────────────────────────────────────────────────────────

def test_swing_formula_from_cloud(cfg):
    w = Weather(cloud_cover=80, humidity=50)
    assert weather.swing_factor(w, cfg) == pytest.approx(0.6 + 0.8 * 1.2)
    assert weather.swing_factor(Weather(cloud_cover=0, humidity=50), cfg) == pytest.approx(0.6)


def test_humidity_extends_swing_window(cfg):
    assert weather.swing_window_extra(Weather(humidity=85), cfg) == 5
    assert weather.swing_window_extra(Weather(humidity=70), cfg) == 0


def test_heat_drains_stamina_and_wind_helps_spin_drift(cfg):
    hot = Conditions(weather=Weather(temperature_c=38))
    fs = factors.FactorSet()
    weather.apply(fs, hot, cfg)
    assert fs["stamina_drain"] == pytest.approx(1.5)
    windy = Conditions(weather=Weather(wind_kph=30, wind_toward="A"), bowling_end="A")
    fs = factors.FactorSet()
    weather.apply(fs, windy, cfg)
    assert fs["spin"] == pytest.approx(1.3)
    assert fs["six"] == pytest.approx(1.10)


def test_rain_is_rare(cfg):
    w = Weather(rain_chance=50, cloud_cover=50)
    p = weather.rain_prob_per_over(w, cfg)
    assert 1 - (1 - p) ** 40 < 0.02       # a T20: both innings
    assert 1 - (1 - p) ** 100 < 0.05      # an ODI
    assert weather.rain_prob_per_over(Weather(rain_chance=0), cfg) == 0


def test_drift_is_seeded_and_clamped(cfg):
    w = Weather(cloud_cover=99, humidity=1)
    a = weather.drift(w, SimRng(5), cfg)
    b = weather.drift(w, SimRng(5), cfg)
    assert a == b
    assert 0 <= a.cloud_cover <= 100 and 0 <= a.humidity <= 100


# ── stadium ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("metres,mult", [(60, 1.5), (65, 1.2), (74, 1.0), (82, 0.75)])
def test_six_bands(cfg, metres, mult):
    assert stadium.six_band(metres, cfg) == mult


def test_altitude_six_distance(cfg):
    assert stadium.altitude_six_distance(500, cfg) == 1.0
    assert stadium.altitude_six_distance(1300, cfg) == pytest.approx(1.12)


def test_seeded_grounds_resolve_by_alias():
    assert stadium.find("MCG").name == "Melbourne Cricket Ground"
    assert stadium.find("Dharamsala").altitude_m == 1300
    assert stadium.find("Wankhede Stadium").dew_factor == 85
    assert stadium.find("Lord's").slope is True
    assert stadium.find("Eden Gardens").avg_first_innings == 165
    assert stadium.find("Newlands").typical_pitch == "Green"
    assert stadium.find("Nowhere Park") is None


def test_every_live_venue_is_in_the_database():
    from services.match_constants import STADIUMS
    missing = [s for s in STADIUMS if stadium.find(s) is None]
    assert not missing


def test_outfield_conversion_follows_speed(cfg):
    assert stadium.outfield_four_prob(3, 0, cfg) == 0
    fast = stadium.outfield_four_prob(3, 90, cfg)
    slow = stadium.outfield_four_prob(3, 40, cfg)
    assert fast > slow > 0
    assert fast <= cfg["stadium"]["outfield"]["candidateShare"]["3"]


def test_mcg_swallows_sixes_wankhede_gives_them(cfg):
    mcg, wank = stadium.find("MCG"), stadium.find("Wankhede")
    assert stadium.six_multiplier(mcg, cfg) < 1.0 < stadium.six_multiplier(wank, cfg)


# ── time of day ─────────────────────────────────────────────────────────

def _dew_cond(hour, dew_factor=85):
    return Conditions(hour=hour, stadium=Stadium(dew_factor=dew_factor))


def test_dew_needs_the_evening(cfg):
    assert time_of_day.dew_intensity(_dew_cond(15.0), cfg) == 0
    assert time_of_day.dew_intensity(_dew_cond(19.5), cfg) == 1.0
    assert 0 < time_of_day.dew_intensity(_dew_cond(19.5, dew_factor=30), cfg) < 1


def test_full_dew_takes_spin_to_0_7_and_adds_drops(cfg):
    fs = factors.FactorSet()
    time_of_day.apply(fs, _dew_cond(20.0), None, cfg)
    assert fs["spin"] == pytest.approx(0.7)
    assert fs.drop_add == pytest.approx(0.15)
    assert any("dew" in e.text for e in fs.effects)


def test_morning_swing_and_pink_ball_window(cfg):
    fs = factors.FactorSet()
    time_of_day.apply(fs, Conditions(hour=10.5, weather=Weather(cloud_cover=20)), None, cfg)
    assert fs["swing"] == pytest.approx(1.4)
    pink = BallState(overs_old=5, color="Pink")
    fs = factors.FactorSet()
    time_of_day.apply(fs, Conditions(hour=15.0), pink, cfg)
    assert fs["swing"] == pytest.approx(1.3)
    fs = factors.FactorSet()
    time_of_day.apply(fs, Conditions(hour=15.0), replace(pink, overs_old=16), cfg)
    assert fs["swing"] == pytest.approx(1.0)


# ── ball aging ──────────────────────────────────────────────────────────

def test_new_ball_then_decay(cfg):
    fs = factors.FactorSet()
    ball_aging.apply(fs, Conditions(), ball_aging.new_ball(), cfg)
    assert (fs["swing"], fs["seam"], fs["pace"]) == pytest.approx((1.6, 1.4, 1.1))
    fs = factors.FactorSet()
    ball_aging.apply(fs, Conditions(), ball_aging.ball_at(24.9, "Even", cfg), cfg)
    assert fs["swing"] == pytest.approx(0.7, abs=0.02)


def test_reverse_swing_needs_old_dry_ball_and_a_skilled_quick(cfg):
    old = ball_aging.ball_at(30, "Dry", cfg)
    assert old.shine < 40
    assert ball_aging.reverse_available(old, "Dry", cfg)
    assert not ball_aging.reverse_available(old, "Green", cfg)
    quick = to_bowler({"id": "q", "bowl_rating": 90, "bowl_style": "Right arm fast",
                       "swing": 80, "pace": 90}, cfg)
    dibbly = to_bowler({"id": "d", "bowl_rating": 50, "bowl_style": "Right arm medium",
                        "swing": 40, "pace": 60}, cfg)
    assert ball_aging.bowler_can_reverse(quick, cfg)
    assert not ball_aging.bowler_can_reverse(dibbly, cfg)


def test_soft_old_ball_helps_spin(cfg):
    ball = ball_aging.ball_at(60, "Dusty", cfg)
    fs = factors.FactorSet()
    ball_aging.apply(fs, Conditions(pitch="Dusty"), ball, cfg)
    assert fs["pace"] < 1 < fs["spin"]


def test_new_ball_due_every_80_in_tests(cfg):
    assert ball_aging.needs_new_ball(ball_aging.ball_at(80, "Even", cfg), "Test", cfg)
    assert not ball_aging.needs_new_ball(ball_aging.ball_at(79, "Even", cfg), "Test", cfg)
    assert not ball_aging.needs_new_ball(ball_aging.ball_at(80, "Even", cfg), "ODI", cfg)


# ── pitch ───────────────────────────────────────────────────────────────

def test_pitch_decays_and_days_four_five_are_a_minefield(cfg):
    base = Conditions(pitch="Dusty", fmt="Test")
    day1 = factors.compose(replace(base, test_day=1, test_session=0), None, cfg)
    day4 = factors.compose(replace(base, test_day=4, test_session=9), None, cfg)
    assert day4["spin"] > day1["spin"]
    assert day4["spin_wkt"] / day1["spin_wkt"] > 2.0     # minefield x2 on top of wear
    even4 = factors.compose(replace(base, pitch="Even", test_day=4, test_session=9), None, cfg)
    even3 = factors.compose(replace(base, pitch="Even", test_day=3, test_session=9), None, cfg)
    assert even4["spin_wkt"] == pytest.approx(even3["spin_wkt"])


def test_heat_cracks_the_pitch_faster(cfg):
    cool = pitch.crack_level(Conditions(pitch="Dry", test_session=6, hot_sessions=0), cfg)
    hot = pitch.crack_level(Conditions(pitch="Dry", test_session=6, hot_sessions=6), cfg)
    assert hot == pytest.approx(cool * 1.3)


def test_pitch_identity_decides_who_takes_wickets(cfg):
    green = factors.compose(Conditions(pitch="Green"), None, cfg)
    dusty = factors.compose(Conditions(pitch="Dusty"), None, cfg)
    assert green["pace_wkt"] > green["spin_wkt"]
    assert dusty["spin_wkt"] > dusty["pace_wkt"]


# ── situation / drama ───────────────────────────────────────────────────

def test_pressure_ramps_above_eight_an_over(cfg):
    calm = situation.evaluate(situation.Situation(runs_needed=40, balls_left=60, is_chase=True, batter_balls=10), cfg)
    hot = situation.evaluate(situation.Situation(runs_needed=120, balls_left=60, is_chase=True, batter_balls=10), cfg)
    assert calm.wicket == 1.0
    assert hot.wicket == pytest.approx(1.6) and hot.aggression_mult == pytest.approx(1.4)


def test_settling_tail_choke_and_death(cfg):
    s = situation.evaluate(situation.Situation(batter_balls=2, batting_position=10), cfg)
    assert s.shot_quality == pytest.approx(0.8 * 0.5)
    choke = situation.evaluate(situation.Situation(runs_needed=30, balls_left=60, wickets_down=3,
                                                   batter_balls=20, is_chase=True), cfg)
    assert "choke" in choke.tags and choke.accuracy == pytest.approx(1.15)
    death = situation.evaluate(situation.Situation(fmt="T20", over=17, batter_balls=20,
                                                   bowler_death_specialist=True), cfg)
    assert death.bowler_edge == pytest.approx(1.3) and death.aggression_set == 100
    assert situation.is_death_over("ODI", 44, cfg) and not situation.is_death_over("ODI", 43, cfg)


def test_drama_chance_is_slider_over_1000(cfg):
    assert drama.chance_per_over(cfg) == pytest.approx(cfg["drama"]["dramaSlider"] / 1000)
    quiet = sim_config.build(override={"drama": {"dramaSlider": 0}}, local_path="")
    assert all(drama.roll_over(SimRng(i), quiet) is None for i in range(200))


# ── outcome ─────────────────────────────────────────────────────────────

def test_better_bowler_in_helpful_conditions_takes_more_wickets(cfg):
    bat = to_batter({"id": "b", "bat_rating": 70}, cfg)
    seamer = to_bowler({"id": "s", "bowl_rating": 85, "bowl_style": "Right arm fast-medium"}, cfg)
    green = factors.compose(Conditions(pitch="Green", hour=10.5, weather=Weather(cloud_cover=85)),
                            ball_aging.new_ball(), cfg, bowler=seamer)
    flat = factors.compose(Conditions(pitch="Flat", hour=15, weather=Weather(cloud_cover=5)),
                           ball_aging.ball_at(15, "Flat", cfg), cfg, bowler=seamer)
    mg = outcome.bucket_multipliers(bat, seamer, green, cfg)
    mf = outcome.bucket_multipliers(bat, seamer, flat, cfg)
    assert mg["Wicket"] > mf["Wicket"] and mg["Four"] < mf["Four"]


def test_resolve_always_returns_a_legal_bucket(cfg):
    rng = SimRng(1)
    base = cfg["outcome"]["base"]["T20"]
    for _ in range(500):
        r = outcome.resolve(base, {k: 1.0 for k in outcome.BUCKETS}, rng, cfg,
                            fs=factors.FactorSet())
        assert r.bucket in outcome.BUCKETS
        assert r.total_runs >= 0


def test_player_derivation_is_stable():
    c = sim_config.get_config()
    p = {"roster_id": 42, "bat_rating": 80, "bowl_rating": 70, "bowl_style": "Right arm off break"}
    assert to_batter(p, c) == to_batter(dict(p), c)
    assert to_bowler(p, c).kind == "spin"
    assert is_spinner("Left arm orthodox", c) and not is_spinner("Right arm fast", c)
    assert to_bowler({**p, "swing": 99}, c).swing == 99


# ── rain / DLS ──────────────────────────────────────────────────────────

def test_dls_second_innings_target_drops_when_overs_lost():
    assert rain_dls.second_innings_target(250, 50, 20, 0, 3) == 251
    assert rain_dls.second_innings_target(250, 50, 20, 15, 3) < 251


def test_dls_first_innings_cut():
    t = rain_dls.first_innings_target(200, 50, 30, 4, 10)
    assert 150 < t < 260


def test_rain_never_cuts_below_minimum():
    assert rain_dls.cap_overs_lost(20, 3.0, 50, 5) == 15
    assert rain_dls.cap_overs_lost(20, 17.0, 5, 5) == 3


# ── whole matches ───────────────────────────────────────────────────────

def _xi(tag, bat, bowl):
    styles = ["Right arm fast", "Right arm fast-medium", "Left arm fast", "Right arm off break",
              "Right arm leg break"]
    players = []
    for i in range(11):
        bowler = i >= 6
        players.append({"roster_id": f"{tag}{i}", "name": f"{tag} {i}",
                        "category": "Bowler" if bowler else "Batsman",
                        "bat_rating": bat - (20 if bowler else 0) - i,
                        "bowl_rating": bowl if bowler else 30,
                        "bowl_style": styles[i - 6] if bowler else "Right arm medium"})
    return Team(name=tag, players=players)


def _play(fmt, seed, a=None, b=None, **kw):
    return simulate_match(MatchSetup(fmt=fmt, team1=a or _xi("A", 80, 80), team2=b or _xi("B", 80, 80),
                                     seed=seed, stadium=stadium.find(kw.pop("ground", "MCG")),
                                     commentary=kw.pop("commentary", False), **kw))


@pytest.mark.parametrize("fmt", ["T20", "ODI", "Test"])
def test_same_seed_same_match(fmt):
    a = json.dumps(_play(fmt, 99, commentary=True), sort_keys=True, default=str)
    b = json.dumps(_play(fmt, 99, commentary=True), sort_keys=True, default=str)
    assert a == b
    assert a != json.dumps(_play(fmt, 100, commentary=True), sort_keys=True, default=str)


def test_limited_overs_scorecard_is_consistent():
    res = _play("T20", 5, commentary=True)
    for inn in res["innings"]:
        assert inn["wickets"] <= 10
        assert inn["legal_balls"] <= 120
        bat = sum(b["runs"] for b in inn["batting"])
        assert bat + inn["extras"] == inn["runs"]
        assert sum(w["wkts"] for w in inn["bowling"]) <= inn["wickets"]
        assert all(w["balls"] <= 24 for w in inn["bowling"])
        assert len(inn["fow"]) == inn["wickets"]
    assert res["result"]["text"]
    assert res["impact"] and res["impact"][0]["impact"] == 10.0


def test_odi_quota_and_death_overs():
    res = _play("ODI", 8)
    for inn in res["innings"]:
        assert all(w["balls"] <= 60 for w in inn["bowling"])


def test_test_mismatch_ends_in_a_sound_result():
    strong, weak = _xi("S", 95, 95), _xi("W", 35, 35)
    for seed in range(3):
        res = _play("Test", seed, strong, weak)
        r = res["result"]
        n = len(res["innings"])
        assert r["winner"] == "S", r
        if "innings" in r["margin"]:
            assert n == 3
        # a 4th innings is only ever played to a positive target
        if n == 4:
            assert res["innings"][3]["target"] > 0


def test_test_innings_carry_the_new_ball_and_sessions():
    res = _play("Test", 4, _xi("A", 99, 40), _xi("B", 99, 40), commentary=True, pitch="Flat")
    lines = [l for inn in res["innings"] for l in inn["commentary"]]
    long_innings = [i for i in res["innings"] if i["legal_balls"] >= 80 * 6]
    if long_innings:
        assert any("New ball taken" in l for l in lines)


def test_rain_reshapes_but_never_abandons():
    wet = sim_config.build(override={"weather": {"rain": {"basePerOverProb": 0.05}}}, local_path="")
    seen = 0
    for seed in range(8):
        res = simulate_match(MatchSetup(fmt="ODI", team1=_xi("A", 80, 80), team2=_xi("B", 80, 80),
                                        seed=seed, stadium=stadium.find("Galle"),
                                        weather=Weather(rain_chance=100), commentary=False), wet)
        if res["rain"]:
            seen += 1
            assert res["result"]["text"]
            inn2 = res["innings"][1]
            assert inn2["max_overs"] >= 20
    assert seen >= 3


def test_summary_explains_the_conditions():
    res = _play("T20", 3, ground="Wankhede", time=MatchTime(session="Night", is_day_night=True,
                                                            start_hour=19.5), pitch="Dry")
    texts = " ".join(i["text"] for i in res["influences"])
    assert "dew" in texts.lower()


def test_standalone_balance_orders_the_pitches():
    """Loose guard: a road out-scores a neutral deck, which out-scores a turner."""
    def mean(p):
        return statistics.mean(_play("T20", f"bal-{p}-{i}", pitch=p)["innings"][0]["runs"]
                               for i in range(30))
    flat, even, dusty = mean("Flat"), mean("Even"), mean("Dusty")
    assert flat > even > dusty
    assert 140 < even < 240


# ── live hook ───────────────────────────────────────────────────────────

LIVE_STATE = {"overs": 20, "pitch_type": "Dry", "stadium": "Wankhede Stadium",
              "conditions": {"weather": "Humid", "humidity": "High", "dew": "Heavy",
                             "day_night": "Night", "match_time": "Night (7:30 PM)",
                             "temperature": 29, "outfield": "Fast"}}
SPINNER = {"roster_id": 1, "name": "S", "bowl_rating": 85, "bowl_style": "Right arm off break"}
BATTER = {"roster_id": 2, "name": "B", "bat_rating": 80}


def test_parse_hour():
    assert hook.parse_hour("Night (7:30 PM)") == 19.5
    assert hook.parse_hour("Morning (10:00 AM)") == 10.0
    assert hook.parse_hour("nonsense", 14.0) == 14.0


def test_hook_is_bounded_and_dew_hurts_spin():
    c = sim_config.get_config()
    mult, shift, _, _ = hook.live_multipliers(LIVE_STATE, BATTER, SPINNER, 14, 2)
    lo, hi = c["meta"]["live_bounds"]["min"], c["meta"]["live_bounds"]["max"]
    assert all(lo <= v <= hi for v in mult.values())
    assert mult["Wicket"] < 1.0 and mult["Six"] > 1.0


def test_hook_neutral_conditions_are_a_no_op():
    c = sim_config.get_config()
    state = {"overs": 20, "pitch_type": "Even", "stadium": None, "conditions": {"match_time": "3:00 PM"}}
    cond, ball = hook.conditions_for(state, 5, 1, c)
    neu = hook.neutral_conditions(cond, c)
    fa = factors.compose(neu, ball, c)
    fb = factors.compose(hook.neutral_conditions(neu, c), ball, c)
    assert fa.as_dict() == fb.as_dict()


def test_hook_off_without_conditions_or_when_disabled(monkeypatch):
    assert hook.make_conditions_hook({"overs": 20}, BATTER, SPINNER) is None
    off = sim_config.build(override={"meta": {"enabled_live": {"T20": False}}}, local_path="")
    monkeypatch.setattr(hook, "get_config", lambda: off)
    assert hook.make_conditions_hook(dict(LIVE_STATE), BATTER, SPINNER) is None


def test_hook_records_influences_for_the_report():
    state = json.loads(json.dumps(LIVE_STATE))
    h = hook.make_conditions_hook(state, BATTER, SPINNER, {"over": 15, "innings": 2})
    out = h({"Dot": 1, "Single": 1, "Double": 1, "Three": 1, "Four": 1, "Six": 1, "Wicket": 1, "Extras": 1})
    assert out["Four"] > 1
    assert any("dew" in r["text"] for r in hook.influence_summary(state))
    json.dumps(state)          # the tally must survive the state store


def test_live_engine_runs_with_the_hook():
    from services import match_analysis
    from tools import pitch_calibration as pc

    random.seed(3)
    pc.WITH_CONDITIONS = True
    try:
        with pc._muted_logging():
            a, b = pc.make_xi("A", 1), pc.make_xi("B", 101)
            from services import cipl_match as cm
            conds, venue = pc._match_conditions("Dry")
            conds = dict(conds, day_night="Night", match_time="Night (7:30 PM)", dew="Heavy")
            state = cm.build_cipl_state(1, 20, 10, 20, 111, 222, a, b, "A", "B", -1, "Dry",
                                        False, ball_format="T20", conditions=conds,
                                        stadium="Wankhede Stadium")
            pc._run_innings(state)
            cm.end_first_innings(state)
            pc._run_innings(state)
    finally:
        pc.WITH_CONDITIONS = False
    assert state.get("sim_influences")
    html = match_analysis.build_match_analysis_html(state)
    assert "What the conditions did" in html

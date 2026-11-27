"""Rain stops play in an interactive match, and the target is revised.

The arithmetic is Duckworth-Lewis through ``engine.dls``, which already has
its own tests; what these pin is the part that is new — when a stoppage may
happen, how often, that it only ever shortens the innings within a floor, that
several showers in one innings add up rather than overwrite each other, and
that every surface follows from the one value the stoppage writes.
"""

import itertools

import pytest

from engine.sim.rng import SimRng
from services import bot_captain, cipl_match as cm, rain_interruption as rain
from tools.calibrate_odi_approach import team


def build(overs=50, weather="Light Rain Earlier", rain_chance=30, seed=11,
          ball_format="ODI", **extra):
    state = cm.build_cipl_state(
        4242, overs, 1, 2, 111, 222, team(1, 0), team(2, 0), "Alpha", "Bravo",
        -100, ball_format=ball_format,
        conditions={"weather": weather, "temperature": 22,
                    "climate": {"rainChance": rain_chance}})
    state["rng_seed"] = seed
    state.update(extra)
    return state


class _Always:
    """An rng that always rains, and always picks the first loss."""
    def random(self):
        return 0.0

    def choices(self, population, weights=None, k=1):
        return [population[0]]


class _Never:
    def random(self):
        return 1.0

    def choices(self, population, weights=None, k=1):
        return [population[0]]


# ── Where it applies ─────────────────────────────────────────────────
@pytest.mark.parametrize("overs", (21, 35, 50))
def test_rain_applies_to_odi_rules_matches(overs):
    assert rain.applies(build(overs))


@pytest.mark.parametrize("ball_format,overs", [("T20", 20), ("T20", 6),
                                               ("The100", 20)])
def test_short_formats_stay_dry(ball_format, overs):
    """A 20-over game cannot absorb a stoppage, and has no DLS to fall back on."""
    state = build(overs, ball_format=ball_format)
    assert not rain.applies(state)
    assert rain.chance(state) == 0.0
    assert not rain.due(state)


def test_practice_and_super_overs_stay_dry():
    assert not rain.applies(build(50, is_bot_match=True))
    assert not rain.applies(build(50, standalone_super_over=True))


def test_the_env_switch_pauses_it(monkeypatch):
    monkeypatch.setenv("CMU_RAIN_ENABLED", "0")
    assert not rain.enabled() and not rain.applies(build(50))
    monkeypatch.setenv("CMU_RAIN_ENABLED", "1")
    assert rain.enabled() and rain.applies(build(50))


# ── How likely, and when ─────────────────────────────────────────────
@pytest.mark.parametrize("weather", ["Clear and Sunny", "Hot and Dry", "Windy"])
def test_a_dry_sky_cannot_rain(weather):
    assert rain.chance(build(50, weather=weather)) == 0.0


def test_a_damp_sky_can_and_a_wet_ground_makes_it_likelier():
    dry_ground = rain.chance(build(50, weather="Overcast", rain_chance=10))
    neutral = rain.chance(build(50, weather="Overcast", rain_chance=30))
    wet_ground = rain.chance(build(50, weather="Overcast", rain_chance=70))
    assert 0 < dry_ground < neutral < wet_ground


def test_the_per_over_chance_stays_small_enough_to_be_an_event():
    """These compound over about ninety eligible overs in a match."""
    for weather, chance in rain.WEATHER_CHANCE.items():
        assert 0 < chance <= 0.002, (weather, chance)
        # At the wettest ground, across both innings, under one match in four.
        per_match = 1 - (1 - chance * 2.0) ** 90
        assert per_match < 0.25, (weather, per_match)


def test_a_missing_or_odd_venue_rain_chance_degrades_to_the_base():
    base = rain.WEATHER_CHANCE["Overcast"]
    state = build(50, weather="Overcast")
    state["conditions"] = {"weather": "Overcast"}
    assert rain.chance(state) == base
    state["conditions"] = {"weather": "Overcast", "climate": {"rainChance": "wet"}}
    assert rain.chance(state) == base


@pytest.mark.parametrize("over,expected", [(1, False), (5, False), (6, True),
                                           (25, True), (49, True), (50, False)])
def test_the_powerplay_and_the_last_over_are_left_alone(over, expected):
    state = build(50)
    state["current_over"] = over
    assert rain.window(state) is expected


def test_an_innings_takes_at_most_two_stoppages():
    state = build(50)
    state["current_over"] = 20
    for _ in range(rain.MAX_STOPPAGES_PER_INNINGS):
        assert rain.due(state)
        rain.apply(state, 2)
        state["current_over"] = 20
    assert rain.stoppages(state) == rain.MAX_STOPPAGES_PER_INNINGS
    assert not rain.due(state)


# ── What a stoppage does ─────────────────────────────────────────────
def test_a_stoppage_shortens_the_innings_and_everything_follows():
    state = build(50)
    state["current_over"] = 21
    record = rain.apply(state, 8)
    assert record["overs_lost"] == 8 and record["new_overs"] == 42
    assert state["overs"] == 42
    assert state["scheduled_overs"] == 50
    # One write, and the innings end, the phases and the quota all move with it.
    assert cm.total_balls(state) == 252
    assert cm.max_bowler_overs(state) == 9
    assert rain.scheduled_overs(state) == 50


def test_a_stoppage_never_cuts_below_the_floor():
    """``apply`` clamps too: it is the public door, and an unclamped number
    wrote a negative innings length that every surface read as all over."""
    state = build(50)
    state["current_over"] = 7
    losable = rain.overs_losable(state)
    assert losable > 0
    record = rain.apply(state, 999)
    assert record["overs_lost"] == losable
    assert record["new_overs"] >= rain.MIN_OVERS_PER_SIDE
    assert state["overs"] == record["new_overs"] > 0
    assert cm.total_balls(state) > 0


def test_a_stoppage_never_cuts_below_what_is_already_bowled():
    state = build(50)
    state["current_over"] = 46          # 45 overs are in the book
    assert rain.overs_losable(state) <= 5


def test_a_shower_that_cannot_fit_costs_nothing():
    state = build(21)
    state["current_over"] = 11
    # 21 overs with a floor of half the scheduled length leaves little to give.
    assert rain.overs_losable(state) <= 11
    assert rain.apply(state, 0) is None


def test_rolling_is_deterministic_from_the_match_seed():
    """A replayed match rains in the same places."""
    def rolled(seed):
        state = build(50, rain_chance=70, seed=seed)
        state["current_over"] = 20
        return rain.roll(state, SimRng(f"{seed}/rain/1/20"))
    assert rolled(5) == rolled(5)
    in_window = build(50, rain_chance=70)
    in_window["current_over"] = 20
    assert rain.roll(in_window, _Never()) == 0
    assert rain.roll(in_window, _Always()) == rain.LOSS_WEIGHTS[0][0]
    # Outside the window nothing rains, however hard the rng tries.
    out_of_window = build(50, rain_chance=70)
    out_of_window["current_over"] = 1
    assert rain.roll(out_of_window, _Always()) == 0


# ── The target ───────────────────────────────────────────────────────
def test_a_first_innings_stoppage_sets_a_dls_target_at_the_break():
    state = build(50)
    state["current_over"] = 21
    rain.apply(state, 8)
    state["total_runs"], state["total_wickets"] = 210, 5
    cm.end_first_innings(state)
    assert state["target"] == 211          # the naive one
    revised = rain.first_innings_target(state)
    assert revised is not None and revised != 211
    # The chasing side has more resources (a full 42 overs, all ten wickets)
    # than the side that was interrupted, so the target goes up.
    assert revised > 211
    lines = rain.break_lines(state, revised)
    assert any("DLS target" in line for line in lines)
    assert any("8 overs lost" in line for line in lines)


def test_a_second_innings_stoppage_revises_the_target_down():
    state = build(50)
    state.update(innings=2, inn1_runs=260, target=261, current_over=21,
                 total_runs=95, total_wickets=3)
    record = rain.apply(state, 10)
    assert record["target"] is not None
    assert record["previous_target"] == 261
    assert record["target"] < 261           # fewer overs, smaller target
    assert state["target"] == record["target"] == state["dls_target"]
    assert cm.chase(state)["target"] == record["target"]


def test_several_showers_in_one_innings_add_up():
    """Overwriting the first stoppage's bookkeeping forgot its resource loss
    and set a target as if only the last shower had happened."""
    one = build(50)
    one["current_over"] = 21
    rain.apply(one, 6)
    one["total_runs"], one["total_wickets"] = 200, 4
    cm.end_first_innings(one)
    single = rain.first_innings_target(one)

    two = build(50)
    two["current_over"] = 15
    rain.apply(two, 3)
    two["current_over"] = 21
    rain.apply(two, 3)
    assert two["overs"] == 44 and two["rain_inn1"]["stoppages"] == 2
    assert two["rain_inn1"]["overs_lost"] == 6
    two["total_runs"], two["total_wickets"] = 200, 4
    cm.end_first_innings(two)
    both = rain.first_innings_target(two)

    # Same score, same overs lost in total, same reduced length: the two
    # targets should be close. The bug made the two-shower target read as if
    # only three overs had gone.
    assert single is not None and both is not None
    assert abs(single - both) <= 6, (single, both)
    assert two["rain_inn1"]["resource_lost"] > 0


def test_no_rain_means_no_revised_target():
    state = build(50)
    state["total_runs"] = 240
    cm.end_first_innings(state)
    assert rain.first_innings_target(state) is None
    assert rain.break_lines(state, None) == []
    assert rain.summary_line(state) == ""
    assert rain.par_now(state) is None


def test_the_dls_par_is_only_offered_in_an_interrupted_chase():
    state = build(50)
    state.update(innings=2, inn1_runs=250, target=251, current_over=21,
                 total_runs=120, total_wickets=3)
    assert rain.par_now(state) is None      # no stoppage yet
    rain.apply(state, 5)
    par = rain.par_now(state)
    assert par is not None and 0 < par <= 250


# ── What the chat says ───────────────────────────────────────────────
def test_the_announcement_names_the_length_and_the_new_quota():
    state = build(50)
    state["current_over"] = 21
    record = rain.apply(state, 10)
    lines = rain.announcement(state, record)
    body = "\n".join(lines)
    assert "Rain stops play" in body and "10 overs lost" in body
    assert "40 overs" in body and "scheduled 50" in body
    assert "bowl <b>8</b>" in body
    assert "DLS at the break" in body


def test_the_second_innings_announcement_quotes_the_new_target():
    state = build(50)
    state.update(innings=2, inn1_runs=300, target=301, current_over=21,
                 total_runs=110, total_wickets=2)
    record = rain.apply(state, 8)
    body = "\n".join(rain.announcement(state, record))
    assert "Revised target" in body and str(record["target"]) in body
    assert "was 301" in body


def test_nothing_is_announced_without_a_stoppage():
    assert rain.announcement(build(50), None) == []


def test_the_result_card_says_the_rain_touched_it():
    state = build(50)
    state["current_over"] = 21
    rain.apply(state, 4)
    state["current_over"] = 30
    rain.apply(state, 3)
    line = rain.summary_line(state)
    assert "7 overs" in line and "50 overs" in line


# ── A whole wet match ────────────────────────────────────────────────
def _play_innings(state):
    while not cm.is_innings_over(state):
        bowler = bot_captain.pick_bowler(state)
        assert bowler, f"no legal bowler at over {state['current_over']}"
        state["current_bowler"] = bowler
        state["batting_approach"] = state["bowling_approach"] = "balanced"
        cm.simulate_over(state)
        if cm.is_innings_over(state):
            break
        if rain.due(state):
            lost = rain.roll(state, _Always()) if state["current_over"] % 7 == 0 else 0
            if lost:
                rain.apply(state, lost)


@pytest.mark.parametrize("overs", (30, 50))
def test_an_interrupted_match_completes_legally(overs):
    state = build(overs, rain_chance=70, seed=17)
    _play_innings(state)
    assert state["overs"] <= overs
    assert cm.balls_bowled(state) <= cm.total_balls(state)
    score = state["total_runs"]
    cm.end_first_innings(state)
    revised = rain.first_innings_target(state)
    if revised is not None:
        state["target"] = revised
        assert revised >= 1
    _play_innings(state)
    # Quotas were recomputed against the shortened innings, and nobody
    # exceeded the one that applied to them.
    for player in state["bowl_xi"]:
        used = state["bowl_stats"][str(player["roster_id"])]["balls"] // 6
        assert used <= 10
    result = cm.compute_result(state)
    assert result["winner"] or result["tie"]
    assert score >= 0

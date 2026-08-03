"""Tests for services/match_dynamics.py — pressure, scenario, super over."""
import logging
import random

logging.disable(logging.CRITICAL)

from services.match_dynamics import (
    chase_pressure, scenario_boost, total_pressure,
    super_over_batsmen, super_over_bowler,
    simulate_super_over_innings, resolve_super_over,
)
from services.probability_engine import calculate_outcome
from services.sim_match import simulate_match
from services.match_formats import custom_format
from tests.test_sim_match import _make_xi


# ── pressure ───────────────────────────────────────────────────────────

def test_chase_pressure_zero_first_innings_and_easy_chase():
    assert chase_pressure(1, None, 80, 30, 20) == 0.0      # 1st innings
    assert chase_pressure(2, 100, 60, 60, 20) == 0.0       # easy RRR
    assert chase_pressure(2, 200, 0, 0, 20) == 0.0 or chase_pressure(2, 200, 0, 0, 20) >= 0


def test_chase_pressure_rises_with_required_rate():
    low = chase_pressure(2, 120, 60, 60, 20)   # need 60 off 60 = 6 RPO
    high = chase_pressure(2, 200, 60, 90, 20)   # need 140 off 30 = 28 RPO
    assert high > low
    assert 0.0 <= high <= 1.0


def test_scenario_boost_only_in_close_finish():
    assert scenario_boost(1, None, 50, 30, 20, True) == 0.0
    assert scenario_boost(2, 120, 50, 30, 20, True) == 0.0   # too far out (60 balls left)
    close = scenario_boost(2, 120, 115, 116, 20, True)        # 5 needed, 4 balls left
    assert close > 0.0
    assert scenario_boost(2, 120, 115, 116, 20, False) == 0.0  # disabled


def test_total_pressure_from_state():
    state = {"innings": 2, "target": 200, "total_runs": 60,
             "current_over": 16, "current_ball": 0, "overs": 20, "total_wickets": 4}
    p = total_pressure(state, scenario=False)
    assert p > 0.0


def test_pressure_increases_boundaries_and_wickets():
    def dist(pressure, n=4000):
        random.seed(11)
        c = {"6": 0, "W": 0}
        for _ in range(n):
            oc = calculate_outcome("Fast", "Right", "Inswing", "Good", "Flat",
                                   18, 20, "Slog", 80, 80, pressure=pressure)
            if oc["type"] == "wicket":
                c["W"] += 1
            elif oc["type"] == "runs" and oc["runs"] == 6:
                c["6"] += 1
        return c
    lo, hi = dist(0.0), dist(1.0)
    assert hi["6"] > lo["6"]
    assert hi["W"] > lo["W"]


# ── super over ─────────────────────────────────────────────────────────

def test_super_over_selection():
    xi = _make_xi("A", 80)
    bats = super_over_batsmen(xi)
    assert len(bats) == 3
    brs = [b["bat_rating"] for b in bats]
    assert brs == sorted(brs, reverse=True)
    bowler = super_over_bowler(xi)
    assert bowler["category"] in ("Bowler", "All-rounder")


def test_super_over_innings_limits():
    random.seed(2)
    inn = simulate_super_over_innings(_make_xi("A", 80), _make_xi("B", 80), "Flat")
    assert inn["balls"] <= 6
    assert inn["wickets"] <= 2
    assert inn["runs"] >= 0


def test_resolve_super_over_decides_or_shares():
    random.seed(4)
    so = resolve_super_over(_make_xi("A", 85), _make_xi("B", 80), "A", "B", "Flat")
    assert "text" in so and so["innings"]
    if not so["shared"]:
        assert so["winner"] in ("A", "B")


def test_tie_triggers_super_over_in_sim():
    # Sweep seeds until a tie occurs; assert it is resolved by a super over.
    found = False
    for s in range(400):
        random.seed(s)
        m = simulate_match(_make_xi("H", 80), _make_xi("A", 80), 5, "Hard",
                           "H", "A", fmt=custom_format(5))
        if m.get("super_over"):
            found = True
            assert m["result"]["margin_type"] in ("super_over", "tie")
            assert m["result"]["text"]
    assert found, "expected at least one tie across 400 seeds"


# ── super over: the rules actually hold ────────────────────────────────
#
# The auto super over used to track only the striker and rotate strike by
# flipping between "slot 0" and "slot 1". With three nominated batsmen that is
# wrong the moment one is out: the reserve comes in at slot 2, and the next
# single put slot 0 — the batsman who had just been dismissed — back on strike.
# A side could bat on with a player who was already out. These tests script the
# ball outcomes so the crease can be read directly rather than hoped for.

import services.match_dynamics as md


def _so_xi(prefix, ratings):
    """An XI whose batting ratings are distinct, so the striker facing each
    ball can be identified from the rating passed to calculate_outcome."""
    return [
        {"name": f"{prefix}{i}", "rating": r, "bat_rating": r,
         "bowl_rating": 40 + i, "category": "All-rounder",
         "bowl_style": "Fast", "bowl_hand": "Right", "bat_hand": "Right"}
        for i, r in enumerate(ratings)
    ]


def _script(monkey_outcomes, seen_strikers):
    """Fake calculate_outcome: replay a fixed list of outcomes and record the
    batting rating (i.e. the identity) of whoever faced each delivery."""
    it = iter(monkey_outcomes)
    def fake(*args, **kwargs):
        seen_strikers.append(args[8])          # striker's batting rating
        try:
            return dict(next(it))
        except StopIteration:
            return {"type": "runs", "runs": 0}
    return fake


def _run_scripted_innings(outcomes, ratings=(90, 80, 70), target=None):
    seen = []
    orig = md.calculate_outcome
    md.calculate_outcome = _script(outcomes, seen)
    try:
        inn = simulate_super_over_innings(
            _so_xi("A", list(ratings)), _so_xi("B", [75, 74, 73]), "Flat",
            target=target)
    finally:
        md.calculate_outcome = orig
    return inn, seen


DOT = {"type": "runs", "runs": 0}
ONE = {"type": "runs", "runs": 1}
WKT = {"type": "wicket", "runs": 0, "how": "Bowled"}
WIDE = {"type": "wide"}


def test_a_dismissed_batsman_never_comes_back_to_the_crease():
    # 90 is out first ball; 70 walks in. Singles then rotate the strike between
    # the survivor (80) and the reserve (70) — 90 must never face again.
    inn, seen = _run_scripted_innings([WKT, ONE, ONE, ONE, ONE, ONE])
    assert inn["wickets"] == 1
    assert seen[0] == 90                      # opener faced the wicket ball
    assert 90 not in seen[1:], f"a dismissed batsman batted again: {seen}"
    assert set(seen[1:]) <= {80, 70}


def test_the_innings_stops_dead_on_the_second_wicket():
    inn, seen = _run_scripted_innings([WKT, WKT, ONE, ONE, ONE, ONE])
    assert inn["wickets"] == 2
    assert inn["balls"] == 2                  # nobody batted on afterwards
    assert len(seen) == 2
    assert inn["dismissed"] == ["A0", "A2"]


def test_a_wide_costs_a_run_and_is_re_bowled():
    inn, _ = _run_scripted_innings([WIDE, WIDE] + [DOT] * 6)
    assert inn["runs"] == 2                   # one penalty run each
    assert inn["balls"] == 6                  # ...and neither was a legal ball
    assert inn["deliveries"] == 8


def test_a_chase_can_be_won_off_a_wide():
    inn, _ = _run_scripted_innings([WIDE] + [DOT] * 6, target=1)
    assert inn["runs"] == 1
    assert inn["balls"] == 0                  # won before a legal ball was bowled


def test_an_unbroken_run_of_wides_still_terminates():
    inn, _ = _run_scripted_innings([WIDE] * 500)
    assert inn["balls"] == 0
    assert inn["deliveries"] == md.SO_MAX_DELIVERIES


def test_completed_runs_before_a_run_out_are_counted():
    inn, _ = _run_scripted_innings(
        [{"type": "wicket", "runs": 1, "how": "Run Out"}] + [DOT] * 6)
    assert inn["runs"] == 1
    assert inn["wickets"] == 1


def test_sixes_and_fours_are_tracked_apart_for_the_countback():
    inn, _ = _run_scripted_innings(
        [{"type": "runs", "runs": 6}, {"type": "runs", "runs": 4}] + [DOT] * 4)
    assert (inn["sixes"], inn["fours"]) == (1, 1)
    assert inn["boundaries"] == 2


def test_restrictions_only_relax_when_a_side_would_have_nobody_left():
    xi = _so_xi("A", [90, 80, 70, 60, 50])
    fresh = super_over_batsmen(xi, exclude=["A0", "A1"])
    assert [p["name"] for p in fresh] == ["A2", "A3", "A4"]
    # A bowler cannot bowl two super overs in a row.
    assert super_over_bowler(xi)["name"] == "A4"          # best bowl_rating
    assert super_over_bowler(xi, exclude="A4")["name"] == "A3"


def test_level_scores_go_to_sixes_before_fours():
    calls = []

    def fake_innings(bat_xi, bowl_xi, pitch, run_factor=1.0, target=None,
                     commentary=None, feed=None, label="", exclude_batsmen=None,
                     exclude_bowler=None):
        calls.append(label)
        # Both sides 10; the side batting first hit more sixes but fewer fours.
        first = len(calls) % 2 == 1
        return {"runs": 10, "wickets": 0, "balls": 6, "deliveries": 6,
                "boundaries": 3, "sixes": 2 if first else 1,
                "fours": 1 if first else 2, "dismissed": [], "bowler": "x"}

    orig = md.simulate_super_over_innings
    md.simulate_super_over_innings = fake_innings
    try:
        so = resolve_super_over(_so_xi("A", [90]), _so_xi("B", [90]),
                                "A", "B", "Flat")
    finally:
        md.simulate_super_over_innings = orig
    assert so["winner"] == "A"
    assert "more sixes" in so["text"]


def test_a_replay_swaps_who_bats_first_and_carries_the_restrictions():
    calls = []

    def fake_innings(bat_xi, bowl_xi, pitch, run_factor=1.0, target=None,
                     commentary=None, feed=None, label="", exclude_batsmen=None,
                     exclude_bowler=None):
        calls.append({"label": label,
                      "exclude_batsmen": list(exclude_batsmen or []),
                      "exclude_bowler": exclude_bowler})
        n = len(calls)
        if n <= 2:                       # super over 1: dead level, even boundaries
            side = "A" if n == 1 else "B"
            return {"runs": 10, "wickets": 1, "balls": 6, "deliveries": 6,
                    "boundaries": 1, "sixes": 1, "fours": 0,
                    "dismissed": [f"{side}out"], "bowler": f"{side}opp_bowler"}
        # super over 2: whoever bats second wins it
        return {"runs": 10 if n == 3 else 11, "wickets": 0, "balls": 6,
                "deliveries": 6, "boundaries": 0, "sixes": 0, "fours": 0,
                "dismissed": [], "bowler": "b2"}

    orig = md.simulate_super_over_innings
    md.simulate_super_over_innings = fake_innings
    try:
        so = resolve_super_over(_so_xi("A", [90]), _so_xi("B", [90]),
                                "A", "B", "Flat", first_bat="a")
    finally:
        md.simulate_super_over_innings = orig

    assert len(calls) == 4
    # B chased in super over 1, so B bats first in the replay.
    assert calls[0]["label"] == "Super Over A"
    assert calls[2]["label"] == "Super Over B"
    # Batsmen dismissed in super over 1 cannot bat in super over 2...
    assert calls[2]["exclude_batsmen"] == ["Bout"]
    assert calls[3]["exclude_batsmen"] == ["Aout"]
    # ...and neither bowler may bowl two super overs in a row.
    assert calls[2]["exclude_bowler"] == "Bopp_bowler"   # A's bowler in SO1
    assert calls[3]["exclude_bowler"] == "Aopp_bowler"   # B's bowler in SO1
    assert so["winner"] == "A"                           # A batted 2nd in SO2


def test_the_side_that_batted_second_bats_first_in_the_super_over():
    # A tied match is a fresh start: the chasing side takes the super over
    # first. /sim used to hand it back to whoever batted first in the match.
    seen = {}

    def fake_resolve(team_a_xi, team_b_xi, a_name, b_name, pitch, **kwargs):
        seen["a"] = a_name
        seen["b"] = b_name
        seen["first_bat"] = kwargs.get("first_bat", "a")
        return {"winner": a_name, "loser": b_name, "shared": False,
                "text": f"{a_name} won the Super Over", "innings": []}

    orig = md.resolve_super_over
    md.resolve_super_over = fake_resolve
    try:
        for s in range(400):
            random.seed(s)
            m = simulate_match(_make_xi("H", 80), _make_xi("A", 80), 5, "Hard",
                               "H", "A", fmt=custom_format(5))
            if m.get("super_over"):
                break
        else:                                    # pragma: no cover - seed sweep
            raise AssertionError("expected at least one tie across 400 seeds")
    finally:
        md.resolve_super_over = orig

    # team_a is the side that batted first in the match, so the super over must
    # be told the OTHER side opens it.
    assert seen["a"] == m["innings1"]["batting_team"]
    assert seen["first_bat"] == "b"

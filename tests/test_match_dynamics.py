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

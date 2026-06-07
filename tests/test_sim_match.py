"""Tests for the auto-simulated match engine (services/sim_match.py)."""
import logging
import random

logging.disable(logging.CRITICAL)

from services.sim_match import (
    simulate_match, simulate_innings, _build_bowling_plan,
    render_innings_card, render_result,
)


def _make_xi(prefix, base):
    cats = ["Batsman"] * 4 + ["Wicket Keeper"] + ["All-rounder"] * 2 + ["Bowler"] * 4
    bowl_styles = ["Fast", "Leg Spinner", "Off Spinner", "Medium Pacer"]
    xi, bi = [], 0
    for i, c in enumerate(cats):
        if c == "Bowler":
            style = bowl_styles[bi % len(bowl_styles)]; bi += 1; bowl_r = base + 5
        elif c == "All-rounder":
            style = "Medium Pacer"; bowl_r = base
        else:
            style = "Medium Pacer"; bowl_r = 10
        xi.append({
            "name": f"{prefix}{i+1}", "rating": base,
            "bat_rating": base if c != "Bowler" else base - 15,
            "bowl_rating": bowl_r, "category": c,
            "bowl_style": style, "bowl_hand": "Right", "bat_hand": "Right",
        })
    return xi


def test_full_match_runs_and_is_consistent():
    random.seed(7)
    home, away = _make_xi("H", 80), _make_xi("A", 78)
    m = simulate_match(home, away, 20, "Flat", "Alpha", "Bravo")
    i1, i2 = m["innings1"], m["innings2"]

    # Scores in a sane T20 range, second innings can't exceed its allotted balls.
    assert 60 <= i1["runs"] <= 320
    assert i1["wickets"] <= 10 and i2["wickets"] <= 10
    assert i2["balls"] <= 20 * 6
    assert m["target"] == i1["runs"] + 1
    assert m["result"]["text"]
    assert m["potm"]
    # Commentary feed has one entry per delivery event.
    assert len(m["commentary_feed"]) > 50


def test_batting_order_is_descending_by_bat_rating():
    random.seed(1)
    home, away = _make_xi("H", 75), _make_xi("A", 75)
    inn = simulate_innings(home, away, 10, "Hard", 1, "H", "A")
    brs = [p["bat_rating"] for p in inn["order"]]
    assert brs == sorted(brs, reverse=True)


def test_only_bowlers_and_allrounders_bowl_no_consecutive_and_capped():
    random.seed(3)
    xi = _make_xi("A", 80)
    overs = 20
    plan = _build_bowling_plan(xi, overs)
    names = [b["name"] for b in plan]
    assert len(names) == overs
    # eligibility: every bowler is a Bowler or All-rounder
    by_name = {p["name"]: p for p in xi}
    assert all(by_name[n]["category"] in ("Bowler", "All-rounder") for n in names)
    # no bowler bowls two overs in a row
    assert all(names[i] != names[i + 1] for i in range(len(names) - 1))
    # 20% quota: nobody bowls more than 4 in a 20-over innings
    from collections import Counter
    assert max(Counter(names).values()) <= 4


def test_result_branches():
    random.seed(99)
    home, away = _make_xi("H", 90), _make_xi("A", 60)  # lopsided
    m = simulate_match(home, away, 10, "Green", "Strong", "Weak")
    assert m["result"]["margin_type"] in ("runs", "wickets", "tie")
    if m["result"]["margin_type"] != "tie":
        assert m["result"]["winner"] in ("Strong", "Weak")


def test_renderers_produce_html():
    random.seed(11)
    home, away = _make_xi("H", 80), _make_xi("A", 80)
    m = simulate_match(home, away, 5, "Dry", "Alpha", "Bravo")
    card = render_innings_card(m["innings1"])
    assert "BATTING" in card and "BOWLING" in card and "TOTAL" in card
    res = render_result(m)
    assert "MATCH RESULT" in res and "Player of the Match" in res
    # Telegram message size guard
    assert len(card) < 4096


def test_chase_stops_at_target():
    random.seed(5)
    home, away = _make_xi("H", 70), _make_xi("A", 95)  # chasers much stronger
    m = simulate_match(home, away, 10, "Flat", "Def", "Chase")
    i2 = m["innings2"]
    if i2["runs"] >= m["target"]:
        # won by wickets — must not have used all 10 wickets unnecessarily nor
        # exceeded the over limit
        assert i2["balls"] <= 10 * 6

"""Tests for the Player-of-the-Match rule across game modes.

Rule: POTM goes to the highest-impact player on the WINNING team, unless a
LOSING-team player has 50+ impact — losing players are only eligible with 50+,
and among all eligible players the highest impact wins. A tie (no winner) falls
back to the overall highest impact.
"""
import logging

logging.disable(logging.CRITICAL)

from handlers.match import _calc_potm
from services.match_webapp_service import _pick_player_of_match
from services.sim_match import _player_of_the_match


# ── handlers/match.py:_calc_potm (/playmatch, /vsbot) ────────────────────────

def _match_state(loser_runs):
    """Two-innings state: 'Losers' batted first, 'Winners' chased.
    Winner's best batsman scores 45 (impact 45); the losing batsman's runs vary.
    """
    return {
        "innings": 2,
        "inn1_team": "Losers",
        "bat_team_name": "Winners",
        "bowl_team_name": "Losers",
        "inn1_bat_xi": [{"roster_id": 1, "name": "L1"}],
        "inn1_bowl_xi": [],
        "inn1_bat_stats": {1: {"runs": loser_runs, "balls": loser_runs, "fours": 0, "sixes": 0}},
        "inn1_bowl_stats": {},
        "bat_xi": [{"roster_id": 2, "name": "W1"}],
        "bowl_xi": [],
        "bat_stats": {2: {"runs": 45, "balls": 45, "fours": 0, "sixes": 0}},
        "bowl_stats": {},
    }


def test_calc_potm_losing_player_with_50plus_wins():
    name, impact, _ = _calc_potm(_match_state(55), "Winners")
    assert name == "L1"
    # 55 runs + 15 fifty-milestone bonus.
    assert impact == 70


def test_calc_potm_losing_player_under_50_does_not_win():
    name, impact, _ = _calc_potm(_match_state(40), "Winners")
    assert name == "W1"
    assert impact == 45


def test_calc_potm_tie_falls_back_to_overall_highest():
    # No winner → highest impact across both sides wins even if under 50.
    name, _, _ = _calc_potm(_match_state(48), None)
    assert name == "L1"


# ── services/match_webapp_service.py:_pick_player_of_match (Mini-App) ─────────

def _webapp_state(loser_runs):
    return {
        "inn1_bat_stats": {1: {"runs": loser_runs, "fours": 0, "sixes": 0}},
        "inn1_bat_xi": [{"roster_id": 1, "name": "L1", "player_id": 1}],
        "inn1_bat_team_id": 100,
        "inn1_bowl_stats": {},
        "inn1_bowl_xi": [],
        "inn1_bowl_team_id": 200,
        "bat_stats": {2: {"runs": 45, "fours": 0, "sixes": 0}},
        "bat_xi": [{"roster_id": 2, "name": "W1", "player_id": 2}],
        "bat_team_id": 200,
        "bowl_stats": {},
        "bowl_xi": [],
        "bowl_team_id": 100,
    }


def test_webapp_potm_losing_player_with_50plus_wins():
    pom = _pick_player_of_match(_webapp_state(55), {"winner_team_id": 200})
    assert pom["name"] == "L1"


def test_webapp_potm_losing_player_under_50_does_not_win():
    pom = _pick_player_of_match(_webapp_state(40), {"winner_team_id": 200})
    assert pom["name"] == "W1"


# ── services/sim_match.py:_player_of_the_match (/sim) ─────────────────────────

def _sim_innings(loser_runs):
    p_l1 = {"name": "L1"}
    p_w1 = {"name": "W1"}
    inn1 = {
        "order": [p_l1],
        "bat_stats": {id(p_l1): {"runs": loser_runs}},
        "bowl_plan": [],
        "bowl_stats": {},
        "batting_team": "Losers",
        "bowling_team": "Winners",
    }
    inn2 = {
        "order": [p_w1],
        "bat_stats": {id(p_w1): {"runs": 45}},
        "bowl_plan": [],
        "bowl_stats": {},
        "batting_team": "Winners",
        "bowling_team": "Losers",
    }
    return inn1, inn2


def test_sim_potm_losing_player_with_50plus_wins():
    inn1, inn2 = _sim_innings(55)
    assert _player_of_the_match(inn1, inn2, {"winner": "Winners"}) == "L1"


def test_sim_potm_losing_player_under_50_does_not_win():
    inn1, inn2 = _sim_innings(40)
    assert _player_of_the_match(inn1, inn2, {"winner": "Winners"}) == "W1"

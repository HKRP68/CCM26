"""Tests for Phase 3 — pitch conditions + formats wired into the auto-sim."""
import logging
import random
from collections import Counter

logging.disable(logging.CRITICAL)

from services.ground_conditions import list_pitches, get_pitch_meta
from services.match_formats import (
    resolve_format, get_format, custom_format, phase_for,
)
from services.probability_engine import PITCH_MODS
from services.sim_match import simulate_match
from tests.test_sim_match import _make_xi


# ── ground_conditions ──────────────────────────────────────────────────

def test_list_pitches_subset_of_engine():
    pitches = list_pitches()
    assert pitches, "expected at least one pitch"
    assert all(p in PITCH_MODS for p in pitches)
    assert "Dead" in PITCH_MODS  # added by Phase 3


def test_pitch_meta_shape_and_ideal_toss():
    for name in ("Green", "Dry", "Hard", "Flat", "Dead"):
        m = get_pitch_meta(name)
        assert set(m) >= {"description", "run_factor", "ideal_toss", "favours"}
        assert m["ideal_toss"] in ("bat", "bowl")
    # Seam-friendly green → bowl first; spin-worsening dry → bat first.
    assert get_pitch_meta("Green")["ideal_toss"] == "bowl"
    assert get_pitch_meta("Dry")["ideal_toss"] == "bat"


# ── match_formats ──────────────────────────────────────────────────────

def test_format_resolution_and_quota():
    assert resolve_format("t20") == "T20"
    assert resolve_format("10") == "T10"
    assert resolve_format("nope") is None
    assert get_format("T10")["max_bowler_overs"] == 2
    assert get_format("T20")["max_bowler_overs"] == 4
    cf = custom_format(15)
    assert cf["overs"] == 15 and cf["max_bowler_overs"] == 3  # ceil(15/5)


def test_phase_for():
    fmt = get_format("T20")
    assert phase_for(1, fmt) == "Powerplay"
    assert phase_for(10, fmt) == "Middle"
    assert phase_for(18, fmt) == "Death"


# ── integration ────────────────────────────────────────────────────────

def test_format_quota_enforced_in_sim():
    random.seed(9)
    m = simulate_match(_make_xi("H", 80), _make_xi("A", 80), 10, "Flat",
                       "H", "A", fmt=get_format("T10"))
    counts = Counter(b["name"] for b in m["innings1"]["bowl_plan"])
    assert max(counts.values()) <= 2  # T10 quota
    assert len(m["innings1"]["bowl_plan"]) == 10


def test_toss_decision_follows_pitch():
    # Green → bowl first: the toss winner bowls, so the OTHER side bats first.
    random.seed(3)
    m = simulate_match(_make_xi("H", 80), _make_xi("A", 80), 20, "Green",
                       "Home", "Away", toss_winner="Home", fmt=get_format("T20"))
    assert m["innings1"]["batting_team"] == "Away"
    # Dry → bat first: the toss winner bats.
    random.seed(3)
    m2 = simulate_match(_make_xi("H", 80), _make_xi("A", 80), 20, "Dry",
                        "Home", "Away", toss_winner="Home", fmt=get_format("T20"))
    assert m2["innings1"]["batting_team"] == "Home"


def test_batting_pitch_outscores_bowling_pitch():
    def avg_first_innings(pitch, n=12):
        total = 0
        for s in range(n):
            random.seed(2000 + s)
            m = simulate_match(_make_xi("H", 80), _make_xi("A", 80), 20, pitch,
                               "H", "A", fmt=get_format("T20"))
            total += m["innings1"]["runs"]
        return total / n
    assert avg_first_innings("Dead") > avg_first_innings("Green")


def test_match_carries_pitch_and_format_metadata():
    random.seed(1)
    m = simulate_match(_make_xi("H", 80), _make_xi("A", 80), 20, "Flat",
                       "H", "A", fmt=get_format("T20"))
    assert m["format"] == "T20"
    assert "description" in m["pitch_meta"]

"""Tests for the UnderCover /cric mechanics added to the match engine:
free hit, mystery ball, momentum, bowler-spam penalty, plus the state
bookkeeping in match_webapp_service._apply_outcome that feeds them.
"""
import logging
import random

logging.disable(logging.CRITICAL)  # silence DB-fallback warnings in this env

from services.probability_engine import calculate_outcome
from services.match_webapp_service import _apply_outcome


# ── Engine: calculate_outcome new kwargs ──────────────────────────────────

def _roll(n=3000, **kw):
    random.seed(123)
    counts = {"W": 0, "runout": 0, "four": 0, "six": 0, "other_wicket": 0}
    for _ in range(n):
        oc = calculate_outcome("Fast Pacer", "Right", "Inswing", "Good", "Flat",
                               10, 20, "Drive", 80, 80, **kw)
        if oc["type"] == "wicket":
            counts["W"] += 1
            if (oc.get("how") or "") == "Run Out":
                counts["runout"] += 1
            else:
                counts["other_wicket"] += 1
        elif oc["type"] == "runs":
            if oc["runs"] == 4:
                counts["four"] += 1
            elif oc["runs"] == 6:
                counts["six"] += 1
    return counts


def test_free_hit_allows_only_run_out():
    c = _roll(free_hit=True)
    # On a free hit the only legal dismissal is a run-out.
    assert c["other_wicket"] == 0
    assert c["W"] == c["runout"]


def test_free_hit_lifts_boundaries():
    base = _roll()
    fh = _roll(free_hit=True)
    assert (fh["four"] + fh["six"]) > (base["four"] + base["six"])


def test_mystery_ball_raises_wickets():
    base = _roll()
    myst = _roll(mystery=True)
    assert myst["W"] > base["W"]


def test_spam_penalty_raises_wickets():
    base = _roll()
    spam = _roll(delivery_repeat=5)
    assert spam["W"] > base["W"]


def test_consec_wickets_momentum_raises_wickets():
    base = _roll()
    mom = _roll(consec_wickets=3)
    assert mom["W"] > base["W"]


def test_defaults_are_noop_equivalent():
    # Calling with explicit defaults must equal the no-kwarg distribution.
    a = _roll()
    b = _roll(free_hit=False, mystery=False, recent_runs=0,
              consec_wickets=0, delivery_repeat=0)
    assert a == b


# ── State bookkeeping: _apply_outcome ─────────────────────────────────────

def _state():
    striker = {"roster_id": 100, "name": "Striker"}
    non_striker = {"roster_id": 101, "name": "NonStriker"}
    bowler = {"roster_id": 200, "name": "Bowler"}
    s = {
        "batting_order": [striker, non_striker],
        "striker_idx": 0, "non_striker_idx": 1, "next_batsman_idx": 2,
        "current_bowler": bowler,
        "bat_stats": {}, "bowl_stats": {},
        "total_runs": 0, "total_wickets": 0,
        "extras_total": 0, "wides": 0, "noballs": 0, "legbyes": 0, "byes": 0,
        "current_over": 1, "current_ball": 0,
        "timeline": [], "over_runs": [],
        "partnership_runs": 0, "partnership_balls": 0, "partnership_history": [],
        "innings": 1, "overs": 5, "wicket_limit": 10,
        # mechanics state
        "free_hit": False, "mystery_active": False,
        "delivery_history": [], "recent_runs_window": [], "consec_wickets": 0,
    }
    return s, striker, bowler


def _apply(s, oc, delivery="Inswing Good"):
    striker = s["batting_order"][s["striker_idx"]]
    return _apply_outcome(s, oc, "Drive", delivery, striker, s["current_bowler"])


def test_noball_arms_free_hit_and_legal_ball_clears_it():
    s, _, _ = _state()
    _apply(s, {"type": "noball", "runs": 0})
    assert s["free_hit"] is True
    # Next legal ball consumes the free hit.
    _apply(s, {"type": "runs", "runs": 1})
    assert s["free_hit"] is False


def test_wide_preserves_free_hit():
    s, _, _ = _state()
    s["free_hit"] = True
    _apply(s, {"type": "wide"})
    assert s["free_hit"] is True  # free hit survives an illegal wide


def test_delivery_history_accumulates_then_resets_at_over_end():
    s, _, _ = _state()
    for _ in range(5):
        _apply(s, {"type": "runs", "runs": 1}, delivery="Inswing Good")
    assert s["delivery_history"] == ["Inswing Good"] * 5
    # 6th legal ball ends the over and clears the history.
    _apply(s, {"type": "runs", "runs": 1}, delivery="Inswing Good")
    assert s["delivery_history"] == []


def test_consec_wickets_tracks_streak():
    s, _, _ = _state()
    _apply(s, {"type": "wicket", "runs": 0, "how": "Bowled"})
    # new batsman would be installed by the caller; simulate next striker exists
    s["bat_stats"].setdefault("101", {"runs": 0, "balls": 0, "fours": 0,
                                      "sixes": 0, "out": False})
    assert s["consec_wickets"] == 1
    _apply(s, {"type": "wicket", "runs": 0, "how": "LBW"})
    assert s["consec_wickets"] == 2
    _apply(s, {"type": "runs", "runs": 2})
    assert s["consec_wickets"] == 0  # reset by a legal scoring ball


def test_recent_runs_window_caps_at_12():
    s, _, _ = _state()
    for _ in range(15):
        _apply(s, {"type": "runs", "runs": 1})
    assert len(s["recent_runs_window"]) == 12

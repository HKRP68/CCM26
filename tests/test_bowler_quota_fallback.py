"""Batsmen bowl once the front-line attack is exhausted (bot matches).

In /wpm and /wpmbot the bowling side is normally restricted to all-rounders
and specialist bowlers. But a team can run out of front-line overs — every
all-rounder/bowler is either the bowler who just bowled or has used their full
per-bowler quota. Real cricket answers this by letting a recognised batsman
fill in, and these tests lock in that fallback across the bowler pool helper,
the picker options, and the human/bot selection paths.
"""

import services.match_webapp_service as svc
from services.match_state_store import A_PICK_NEW_BOWLER


def _bowler(rid, name):
    return {"roster_id": rid, "player_id": rid + 1000, "name": name,
            "rating": 80, "bat_rating": 40, "bowl_rating": 82,
            "category": "Bowler", "bowl_style": "Fast Pacer"}


def _allrounder(rid, name):
    return {"roster_id": rid, "player_id": rid + 1000, "name": name,
            "rating": 80, "bat_rating": 70, "bowl_rating": 75,
            "category": "All-rounder", "bowl_style": "Off Spinner"}


def _batsman(rid, name):
    return {"roster_id": rid, "player_id": rid + 1000, "name": name,
            "rating": 78, "bat_rating": 85, "bowl_rating": 40,
            "category": "Batsman", "bowl_style": "Off Spinner"}


def _state(bowl_stats=None, prev=None, overs=5, is_vsbot=True):
    # overs=5 → per-bowler quota = ceil(5/5) = 1.
    return {
        "match_id": 42,
        "setup": svc.SETUP_DONE,
        "innings": 1,
        "overs": overs,
        "is_vsbot": is_vsbot,
        "bat_team_id": 1,
        "bowl_team_id": 2,
        "bowl_xi": [
            _bowler(200, "Pacer"),
            _allrounder(201, "AllRounder"),
            _batsman(202, "Opener"),
            _batsman(203, "TopOrder"),
        ],
        "bowl_stats": bowl_stats or {},
        "prev_bowler_rid": prev,
    }


# ── _bot_bowler_pool ──────────────────────────────────────────────────

def test_pool_front_line_only_while_available():
    """Fresh innings: only the all-rounder + specialist bowler are offered."""
    state = _state()
    pool = svc._bot_bowler_pool(state, state["bowl_xi"], prev_rid=None)
    names = {p["name"] for p in pool}
    assert names == {"Pacer", "AllRounder"}


def test_pool_unlocks_batsmen_when_front_line_spent():
    """Both front-liners at quota → batsmen join the pool."""
    state = _state(bowl_stats={200: {"overs_done": 1}, 201: {"overs_done": 1}},
                   prev=201)
    pool = svc._bot_bowler_pool(state, state["bowl_xi"], prev_rid=201)
    names = {p["name"] for p in pool}
    assert names == {"Pacer", "AllRounder", "Opener", "TopOrder"}


def test_pool_stays_front_line_when_one_front_liner_free():
    """One front-liner still under quota keeps batsmen locked out."""
    state = _state(bowl_stats={200: {"overs_done": 1}}, prev=200)
    pool = svc._bot_bowler_pool(state, state["bowl_xi"], prev_rid=200)
    names = {p["name"] for p in pool}
    assert names == {"Pacer", "AllRounder"}


# ── get_new_bowler_options ────────────────────────────────────────────

def _patch(monkeypatch, state, na=A_PICK_NEW_BOWLER):
    monkeypatch.setattr(svc.mwa, "get_state", lambda match_id: state)
    monkeypatch.setattr(svc.mwa, "get_next_action", lambda match_id: na)
    monkeypatch.setattr(svc.mwa, "save_state", lambda *a, **k: None)


def test_options_offer_only_front_line_when_available(monkeypatch):
    state = _state()
    _patch(monkeypatch, state)
    res = svc.get_new_bowler_options(42, 2)
    offered = {o["name"] for o in res["options"]}
    assert offered == {"Pacer", "AllRounder"}


def test_options_offer_enabled_batsmen_when_front_line_spent(monkeypatch):
    state = _state(bowl_stats={200: {"overs_done": 1}, 201: {"overs_done": 1}},
                   prev=201)
    _patch(monkeypatch, state)
    res = svc.get_new_bowler_options(42, 2)
    by_name = {o["name"]: o for o in res["options"]}
    # Batsmen are offered and selectable now that the attack is exhausted.
    assert by_name["Opener"]["disabled"] is False
    assert by_name["TopOrder"]["disabled"] is False
    # The spent specialists are disabled (capped), prev stays disabled.
    assert by_name["Pacer"]["disabled"] is True
    assert by_name["AllRounder"]["disabled"] is True


# ── set_over_bowler ───────────────────────────────────────────────────

def test_set_bowler_rejects_batsman_while_front_line_free(monkeypatch):
    state = _state()
    _patch(monkeypatch, state)
    ok, msg = svc.select_new_bowler(42, 2, 202)  # Opener (batsman)
    assert ok is False
    assert "front-line" in msg.lower()


def test_set_bowler_accepts_batsman_when_front_line_spent(monkeypatch):
    state = _state(bowl_stats={200: {"overs_done": 1}, 201: {"overs_done": 1}},
                   prev=201)
    _patch(monkeypatch, state)
    ok, msg = svc.select_new_bowler(42, 2, 202)  # Opener (batsman) now allowed
    assert ok is True
    assert state["current_bowler"]["roster_id"] == 202


def test_set_bowler_rejects_capped_specialist_when_batsmen_unlocked(monkeypatch):
    """Once batsmen are the only quota-safe option, a capped specialist can't
    steal an illegal extra over."""
    state = _state(bowl_stats={200: {"overs_done": 1}, 201: {"overs_done": 1}},
                   prev=201)
    _patch(monkeypatch, state)
    ok, msg = svc.select_new_bowler(42, 2, 200)  # Pacer, already at quota
    assert ok is False
    assert "quota" in msg.lower()

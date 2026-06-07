from types import SimpleNamespace

import services.match_webapp_service as svc
from services.match_state_store import A_PICK_NEW_BATSMAN


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def join(self, *args, **kwargs):
        return self

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *args, **kwargs):
        return _FakeQuery(self._rows)


def _incoming_row(roster_id=300, name="Impact Sub"):
    entry = SimpleNamespace(id=roster_id)
    player = SimpleNamespace(
        id=roster_id + 1000,
        name=name,
        rating=75,
        bat_rating=50,
        bowl_rating=80,
        category="Bowler",
        bowl_style="Fast Pacer",
        bowl_hand="Right",
        bat_hand="Right",
    )
    return entry, player


def _player(roster_id, name):
    return {
        "roster_id": roster_id,
        "player_id": roster_id + 1000,
        "name": name,
        "rating": 70,
        "bat_rating": 40,
        "bowl_rating": 80,
    }


def _after_wicket_state():
    current_bowler = _player(200, "Current Bowler")
    previous_bowler = _player(201, "Previous Bowler")
    other_bowler = _player(202, "Other Bowler")
    return {
        "match_id": 99,
        "setup": svc.SETUP_DONE,
        "innings": 1,
        "bat_team_id": 1,
        "bowl_team_id": 2,
        "bat_team_name": "Batters",
        "bowl_team_name": "Bowlers",
        "bat_xi": [_player(100, "Striker"), _player(101, "Non Striker")],
        "bowl_xi": [current_bowler, previous_bowler, other_bowler],
        "batting_order": [_player(100, "Striker"), _player(101, "Non Striker")],
        "striker_idx": 0,
        "non_striker_idx": 1,
        "current_bowler": current_bowler,
        "prev_bowler_rid": previous_bowler["roster_id"],
        "current_over": 2,
        "current_ball": 3,
        "impact_players": {},
        "commentary_log": [],
    }


def _patch_after_wicket(monkeypatch, state):
    monkeypatch.setattr(svc.mwa, "get_state", lambda match_id: state)
    monkeypatch.setattr(svc.mwa, "get_next_action", lambda match_id: A_PICK_NEW_BATSMAN)
    monkeypatch.setattr(svc.mwa, "save_state", lambda *args, **kwargs: None)


def test_current_bowler_is_not_replaceable_after_wicket(monkeypatch):
    state = _after_wicket_state()
    _patch_after_wicket(monkeypatch, state)

    opts = svc.get_impact_player_options(_FakeSession([_incoming_row()]), 99, 2)

    by_rid = {p["roster_id"]: p for p in opts["replaceable_players"]}
    assert by_rid[200]["disabled"] is True
    assert by_rid[200]["disabled_reason"] == "Bowling current over"
    assert by_rid[201]["disabled"] is True
    assert by_rid[201]["disabled_reason"] == "Bowled previous over"
    assert by_rid[202]["disabled"] is False


def test_use_impact_player_rejects_current_bowler_after_wicket(monkeypatch):
    state = _after_wicket_state()
    _patch_after_wicket(monkeypatch, state)

    ok, msg, rec = svc.use_impact_player(_FakeSession([_incoming_row()]), 99, 2, 300, 200)

    assert ok is False
    assert msg == "Bowling current over"
    assert rec is None
    assert state["current_bowler"]["roster_id"] == 200
    assert state["bowl_xi"][0]["roster_id"] == 200

from types import SimpleNamespace

import services.match_webapp_service as svc
from services.match_state_store import (
    A_COMPLETED, A_PICK_NEW_BATSMAN, A_PICK_SHOT,
)


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


def _fake_update_state_cas(state):
    """Drive svc.select_openers/select_bowler's CAS mutator against a plain
    in-memory `state` dict, mirroring update_state_cas's contract without a DB."""
    def _run(match_id, mutator, max_retries=5):
        try:
            result, _next_action = mutator(state)
        except svc.CasAbort as abort:
            return abort.result
        return result
    return _run


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


def test_impact_player_preserves_outgoing_identity_when_replaced(monkeypatch):
    state = _after_wicket_state()
    state["bowl_stats"] = {
        "200": {"balls": 3, "runs": 10, "wickets": 1},
        "201": {"balls": 6, "runs": 8, "wickets": 0},
        "202": {"balls": 6, "runs": 5, "wickets": 1},
    }
    _patch_after_wicket(monkeypatch, state)

    ok, msg, rec = svc.use_impact_player(_FakeSession([_incoming_row()]), 99, 2, 300, 202)

    assert ok is True
    assert "Impact Player confirmed" in msg
    assert rec["in_roster_id"] == 300
    by_rid = {p["roster_id"]: p for p in state["bowl_xi"]}
    assert by_rid[202]["active"] is False
    assert by_rid[202]["impact_replaced"] is True
    assert by_rid[202]["replaced_by_roster_id"] == 300
    assert by_rid[300]["active"] is True
    assert by_rid[300]["impact_replacement"] is True
    assert state["bowl_stats"]["202"]["wickets"] == 1
    assert state["bowl_stats"]["300"] == {
        "balls": 0, "runs": 0, "wickets": 0, "overs_done": 0,
        "this_over_balls": 0, "maidens": 0, "this_over_runs": 0,
    }


def test_persist_stats_lookup_includes_inactive_impact_player(monkeypatch):
    import services.player_stats_service as stats_svc

    calls = []
    monkeypatch.setattr(
        stats_svc,
        "_update_bowling",
        lambda session, user_id, player_id, bowling: (
            calls.append((user_id, player_id, bowling)) or True
        ),
    )

    state = {
        "innings": 2,
        "bat_team_id": 1,
        "bowl_team_id": 2,
        "bat_xi": [],
        "bowl_xi": [
            {**_player(202, "Other Bowler"), "active": False, "impact_replaced": True},
            {**_player(300, "Impact Sub"), "active": True, "impact_replacement": True},
        ],
        "bat_stats": {},
        "bowl_stats": {
            "202": {"balls": 6, "runs": 5, "wickets": 1},
            "300": {"balls": 0, "runs": 0, "wickets": 0},
        },
        "inn1_stats_saved": True,
    }

    counts = stats_svc.persist_player_game_stats(object(), state)

    assert counts["bowling"] == 1
    assert calls == [(2, 1202, {"balls": 6, "runs": 5, "wickets": 1})]


def _patch_next_action(monkeypatch, state, next_action):
    monkeypatch.setattr(svc.mwa, "get_state", lambda match_id: state)
    monkeypatch.setattr(svc.mwa, "get_next_action", lambda match_id: next_action)
    monkeypatch.setattr(svc.mwa, "save_state", lambda *args, **kwargs: None)


def _innings_two_state_without_setup():
    state = _after_wicket_state()
    state.pop("setup", None)
    state["innings"] = 2
    return state


def test_missing_setup_in_innings_two_is_not_treated_as_innings_break(monkeypatch):
    state = _innings_two_state_without_setup()
    _patch_next_action(monkeypatch, state, A_PICK_SHOT)

    opts = svc.get_impact_player_options(_FakeSession([_incoming_row()]), 99, 2)
    ok, msg, rec = svc.use_impact_player(
        _FakeSession([_incoming_row()]), 99, 2, 300, 202,
    )

    assert opts["can_use"] is False
    assert opts["legal_break"] is None
    assert ok is False
    assert msg == (
        "Impact Player can be used only between overs, after a wicket, or "
        "at innings break."
    )
    assert rec is None


def test_completed_innings_two_without_setup_is_not_treated_as_innings_break(monkeypatch):
    state = _innings_two_state_without_setup()
    _patch_next_action(monkeypatch, state, A_COMPLETED)

    opts = svc.get_impact_player_options(_FakeSession([_incoming_row()]), 99, 2)
    ok, msg, rec = svc.use_impact_player(
        _FakeSession([_incoming_row()]), 99, 2, 300, 202,
    )

    assert opts["can_use"] is False
    assert opts["legal_break"] is None
    assert ok is False
    assert msg == (
        "Impact Player can be used only between overs, after a wicket, or "
        "at innings break."
    )
    assert rec is None


def test_pvp_innings_break_setup_phase_allows_impact_player(monkeypatch):
    state = _innings_two_state_without_setup()
    state["setup"] = svc.SETUP_PICKING
    _patch_next_action(monkeypatch, state, "SETUP")

    opts = svc.get_impact_player_options(_FakeSession([_incoming_row()]), 99, 2)

    assert opts["can_use"] is True
    assert opts["legal_break"] == "innings break"


def test_select_players_uses_serialized_batting_xi_indices_after_impact(monkeypatch):
    state = {
        "setup": svc.SETUP_PICKING,
        "bat_team_id": 1,
        "bowl_team_id": 2,
        "bat_xi": [
            _player(100, "Opener"),
            {**_player(101, "Replaced Player"), "active": False, "impact_replaced": True},
            _player(102, "Impact Opener"),
        ],
        "bowl_xi": [_player(200, "Bowler")],
        "openers_done": False,
        "bowler_done": False,
    }
    monkeypatch.setattr(svc.mwa, "get_state", lambda match_id: state)
    monkeypatch.setattr(svc.mwa, "update_state_cas", _fake_update_state_cas(state))

    ok, started, msg = svc.select_players(99, 1, striker_idx=0, non_striker_idx=2)

    assert ok is True
    assert started is False
    assert msg == "Openers locked in. Waiting for the bowler…"
    assert [p["roster_id"] for p in state["batting_order"]] == [100, 102]


def test_select_players_uses_serialized_bowling_xi_indices_after_impact(monkeypatch):
    state = {
        "setup": svc.SETUP_PICKING,
        "bat_team_id": 1,
        "bowl_team_id": 2,
        "bat_xi": [_player(100, "Batter")],
        "bowl_xi": [
            _player(200, "New Ball Bowler"),
            {**_player(201, "Replaced Bowler"), "active": False, "impact_replaced": True},
            _player(202, "Impact Bowler"),
        ],
        "openers_done": False,
        "bowler_done": False,
    }
    monkeypatch.setattr(svc.mwa, "get_state", lambda match_id: state)
    monkeypatch.setattr(svc.mwa, "update_state_cas", _fake_update_state_cas(state))

    ok, started, msg = svc.select_players(99, 2, bowler_idx=2)

    assert ok is True
    assert started is False
    assert msg == "Bowler selected. Waiting for the openers…"
    assert state["current_bowler"]["roster_id"] == 202

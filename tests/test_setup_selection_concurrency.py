"""Regression coverage for the "stuck on Waiting for opponent" bug.

select_openers/select_bowler used to read the whole match-state JSON blob,
flip only their own side's flag, and overwrite the blob — so two near-
simultaneous submissions (the normal case: both players tap "Confirm" once
ready) could race and silently drop one side's flag, leaving the match
forever stuck in setup. These tests exercise the optimistic-concurrency CAS
helper that replaced that read-modify-write, and the innings-break phase
mapping that now sits between the 1st and 2nd innings.
"""

import json

import services.match_webapp_service as svc
from services.match_state_store import (
    CasAbort, update_state_cas, A_PICK_DELIVERY,
)
from models import MatchState


class _FakeRow:
    def __init__(self, match_id, state, version=1, next_action=None):
        self.match_id = match_id
        self.state_json = json.dumps(state)
        self.version = version
        self.next_action = next_action
        self.ball_seq = 0
        self.last_modified = None


class _FakeQuery:
    def __init__(self, table, conditions=None):
        self.table = table
        self.conditions = conditions or {}

    def filter(self, *exprs):
        conditions = dict(self.conditions)
        for e in exprs:
            conditions[e.left.key] = e.right.value
        return _FakeQuery(self.table, conditions)

    def _matches(self, row):
        return all(getattr(row, k) == v for k, v in self.conditions.items())

    def first(self):
        for row in self.table:
            if self._matches(row):
                return row
        return None

    def update(self, values, synchronize_session=False):
        matched = [row for row in self.table if self._matches(row)]
        for row in matched:
            for col, val in values.items():
                setattr(row, col.key, val)
        return len(matched)


class _FakeSession:
    def __init__(self, table):
        self.table = table

    def query(self, model):
        assert model is MatchState
        return _FakeQuery(self.table)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class _DummyCtx:
    def __init__(self):
        self.bot_data = {}


def _patch_store(monkeypatch, table):
    import services.match_state_store as store
    store.clear_cache()  # don't leak shared-cache entries across tests
    monkeypatch.setattr(store, "get_session", lambda: _FakeSession(table))


def test_update_state_cas_merges_concurrent_writers_instead_of_clobbering(monkeypatch):
    """Two independent mutators ("batting side picks openers", "bowling side
    picks a bowler") racing for the same row must BOTH land — the second
    writer's CAS retry has to see the first writer's already-committed change,
    not a stale pre-race snapshot."""
    table = [_FakeRow(1, {"openers_done": False, "bowler_done": False})]
    _patch_store(monkeypatch, table)

    ran_inner = []

    def mutator_openers(state):
        # Simulate the *other* side's request completing its full
        # read → mutate → CAS-write cycle while ours is "in flight" — this
        # bumps the row's version under us, so our own conditional UPDATE
        # below will miss and have to retry against the merged state.
        if not ran_inner:
            ran_inner.append(True)
            update_state_cas(_DummyCtx(), 1, mutator_bowler)
        state["openers_done"] = True
        both = state.get("openers_done") and state.get("bowler_done")
        return both, (A_PICK_DELIVERY if both else None)

    def mutator_bowler(state):
        state["bowler_done"] = True
        both = state.get("openers_done") and state.get("bowler_done")
        return both, (A_PICK_DELIVERY if both else None)

    started = update_state_cas(_DummyCtx(), 1, mutator_openers)

    final_state = json.loads(table[0].state_json)
    assert final_state["openers_done"] is True
    assert final_state["bowler_done"] is True
    assert started is True
    assert table[0].next_action == A_PICK_DELIVERY


def test_update_state_cas_aborts_without_writing_on_validation_failure(monkeypatch):
    table = [_FakeRow(1, {"openers_done": True}, version=5)]
    _patch_store(monkeypatch, table)

    def mutator(state):
        raise CasAbort(("rejected", state.get("openers_done")))

    result = update_state_cas(_DummyCtx(), 1, mutator)

    assert result == ("rejected", True)
    # Nothing persisted — version untouched.
    assert table[0].version == 5


def test_select_openers_then_select_bowler_both_land_and_start_match(monkeypatch):
    """End-to-end through the real select_openers/select_bowler against a
    shared CAS-backed row: both submissions must be reflected and the match
    must auto-start — neither side should be left staring at "Waiting for
    opponent..."."""
    state = {
        "setup": svc.SETUP_PICKING,
        "bat_team_id": 1,
        "bowl_team_id": 2,
        "bat_xi": [
            {"roster_id": 100, "name": "Opener A"},
            {"roster_id": 101, "name": "Opener B"},
            {"roster_id": 102, "name": "Number 3"},
        ],
        "bowl_xi": [
            {"roster_id": 200, "name": "Bowler A"},
            {"roster_id": 201, "name": "Bowler B"},
        ],
        "openers_done": False,
        "bowler_done": False,
    }
    table = [_FakeRow(1, state)]
    _patch_store(monkeypatch, table)
    monkeypatch.setattr(svc.mwa, "get_state", lambda match_id: json.loads(table[0].state_json))

    ok1, started1, msg1 = svc.select_openers(1, 1, 100, 101)
    assert ok1 is True
    assert started1 is False
    assert "Waiting for the bowler" in msg1

    ok2, started2, msg2 = svc.select_bowler(1, 2, 200)
    assert ok2 is True
    assert started2 is True
    assert "match starting" in msg2.lower()

    final_state = json.loads(table[0].state_json)
    assert final_state["openers_done"] is True
    assert final_state["bowler_done"] is True
    assert final_state["setup"] == svc.SETUP_DONE
    assert table[0].next_action == A_PICK_DELIVERY


def test_phase_status_maps_innings_break_setup_to_dedicated_phase():
    state = {"setup": svc.SETUP_INNINGS_BREAK, "innings": 2}
    assert svc.phase_status(state, "playing") == "innings_break"

    # Once resumed, normal phases resolve again.
    state["setup"] = svc.SETUP_PICKING
    assert svc.phase_status(state, "playing") == "xi_selection"


def test_resume_after_innings_break_pvp_resets_setup_for_both_sides():
    state = {"is_vsbot": False, "openers_done": True, "bowler_done": True,
             "current_bowler": {"roster_id": 1}, "innings_break_started_at": 123.0}
    next_action = svc._resume_after_innings_break(state)
    assert state["setup"] == svc.SETUP_PICKING
    assert state["openers_done"] is False
    assert state["bowler_done"] is False
    assert state["current_bowler"] is None
    assert "innings_break_started_at" not in state
    assert next_action == "SETUP"


def test_advance_innings_break_if_due_waits_then_resumes(monkeypatch):
    import time as _time

    now = [1000.0]
    monkeypatch.setattr(_time, "time", lambda: now[0])
    monkeypatch.setattr(svc, "_time", _time)

    state = {"setup": svc.SETUP_INNINGS_BREAK, "is_vsbot": False,
             "openers_done": False, "bowler_done": False,
             "innings_break_started_at": 1000.0}
    table = [_FakeRow(1, state)]
    _patch_store(monkeypatch, table)

    # Too early — no transition yet.
    assert svc.advance_innings_break_if_due(1) is False
    assert json.loads(table[0].state_json)["setup"] == svc.SETUP_INNINGS_BREAK

    # Past the display window — now resumes into PvP setup.
    now[0] = 1000.0 + svc.INNINGS_BREAK_SECONDS + 1
    assert svc.advance_innings_break_if_due(1) is True
    final_state = json.loads(table[0].state_json)
    assert final_state["setup"] == svc.SETUP_PICKING
    assert table[0].next_action == "SETUP"

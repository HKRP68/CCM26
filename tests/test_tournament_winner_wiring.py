"""The website's selected simulation winner survives kickoff and Super Overs."""

import asyncio
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import handlers.cipl_play as cp
import handlers.super_over as so_mod
from services.match_webapp_service import strip_hidden_plans
from test_super_over import (
    CHAT, GUEST_TG, GUEST_UID, HOST_TG, HOST_UID, FakeContext,
    _get, _play_match, _tied_state, _xi,
)


def test_challenge_kickoff_preserves_the_selected_winner_in_saved_state(monkeypatch):
    import handlers.challenge as challenge

    saved = []

    async def save(context, mid, state, **kwargs):
        saved.append(dict(state))

    monkeypatch.setattr(cp, "_ss", save)
    monkeypatch.setattr(cp, "_cleanup_setup_and_announce", AsyncMock())
    monkeypatch.setattr(cp, "_prompt_bowler", AsyncMock())
    monkeypatch.setattr(challenge, "_challenge_xi_limits", lambda draft: (0, 11, []))
    monkeypatch.setattr(challenge, "fresh_overseas_limits", lambda draft: None)
    match = SimpleNamespace(id=9001, overs=20, stadium="Eden Gardens")
    host = SimpleNamespace(id=HOST_UID, telegram_id=HOST_TG, username="host", first_name="Host")
    guest = SimpleNamespace(id=GUEST_UID, telegram_id=GUEST_TG, username="guest", first_name="Guest")
    draft = {
        "tournament_id": 77, "reserved_fixture_id": 12,
        "tournament_team_by_user": {HOST_UID: 10, GUEST_UID: 20},
        "_planned_winner_user_id": GUEST_UID, "_planned_super_overs": 2,
        "conditions": {"day_night": "Night"},
    }
    asyncio.run(cp.begin_cipl_match(
        FakeContext(), CHAT, match, host, guest, _xi("H", 100), _xi("G", 200),
        "Host XI", "Guest XI", "Hard", draft=draft))

    assert len(saved) == 1
    assert saved[0]["_planned_winner_user_id"] == GUEST_UID
    assert saved[0]["_planned_super_overs"] == 2
    public = strip_hidden_plans(saved[0])
    assert "_planned_winner_user_id" not in public
    assert "_planned_super_overs" not in public
    assert public["tournament_id"] == 77
    # Sanitizing a client response never deletes the server's match settings.
    assert saved[0]["_planned_winner_user_id"] == GUEST_UID


@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize("viewer", [HOST_UID, GUEST_UID, 999])
def test_raw_match_api_keeps_planned_settings_server_side(monkeypatch, completed, viewer):
    import admin
    from services import match_webapp_access as access
    from services import match_webapp_service as webapp

    state = dict(_tied_state(), mode="cipl_approach", _planned_super_overs=2,
                 _planned_winner_user_id=GUEST_UID)
    monkeypatch.setattr(access, "get_state", lambda mid: None if completed else state)
    monkeypatch.setattr(access, "get_next_action", lambda mid: "PICK_BAT_APPROACH")
    monkeypatch.setattr(access, "get_ball_seq", lambda mid: 7)
    monkeypatch.setattr(webapp, "load_final_scorecard", lambda *a: {
        "arena_state": state, "result_text": "Guest XI won"})
    monkeypatch.setattr(webapp, "build_snapshot", lambda *a, **k: {"status": "active"})
    monkeypatch.setattr(admin, "_user_has_autoplay", lambda *a: False)

    payload = admin._match_rest_full_state(None, state["match_id"], viewer)

    assert payload["ok"]
    assert "_planned_winner_user_id" not in payload["state"]
    assert "_planned_super_overs" not in payload["state"]
    assert payload["state"]["total_runs"] == state["total_runs"]
    assert state["_planned_winner_user_id"] == GUEST_UID
    assert state["_planned_super_overs"] == 2
    if completed:
        assert payload["completed"]
        assert payload["snapshot"]["status"] == "completed"


def test_practice_match_clears_official_fixture_plans(monkeypatch):
    from services import bot_captain

    monkeypatch.setattr(bot_captain, "assign_persona", lambda *a: None)
    monkeypatch.setattr(bot_captain, "assign_difficulty", lambda *a: None)
    state = dict(_tied_state(), tournament_id=77, reserved_fixture_id=12,
                 _planned_winner_user_id=GUEST_UID, _planned_super_overs=2)
    cp.mark_bot_match(state, bot_user_id=GUEST_UID)
    assert state["is_bot_match"]
    assert state["stats_disabled"]
    assert state["tournament_id"] is None
    assert state["reserved_fixture_id"] is None
    assert "_planned_winner_user_id" not in state
    assert "_planned_super_overs" not in state


def test_completed_scorecard_api_sanitizes_its_embedded_arena(monkeypatch):
    from services import match_webapp_service as webapp

    arena = dict(_tied_state(), mode="cipl_approach", _planned_super_overs=2,
                 _planned_winner_user_id=GUEST_UID)
    archived = {"arena_state": arena, "innings": [{"runs": 150}],
                "result_text": "Guest XI won", "completed": True}
    monkeypatch.setattr(webapp.mwa, "get_state", lambda mid: None)
    monkeypatch.setattr(webapp, "load_final_scorecard", lambda *a: archived)

    payload = webapp.get_scorecard_any(None, arena["match_id"], 999)

    assert payload["ok"]
    assert payload["completed"]
    assert payload["innings"] == archived["innings"]
    assert "_planned_winner_user_id" not in payload["arena_state"]
    assert "_planned_super_overs" not in payload["arena_state"]
    assert archived["arena_state"]["_planned_winner_user_id"] == GUEST_UID


@pytest.mark.parametrize("winner", [HOST_UID, GUEST_UID])
@pytest.mark.parametrize("planned", [0, 1, 2])
@pytest.mark.parametrize("natural", ["dot", "six", "wicket", "wide", "noball"])
def test_super_over_records_the_selected_winner_from_real_deliveries(
        monkeypatch, winner, planned, natural):
    random.seed(59 + winner + planned)
    captured = {}

    async def finalize(context, mid, winner_uid, loser_uid, **kwargs):
        so = _get(context, mid)
        captured.update(winner=winner_uid, loser=loser_uid, so=so)
        context.bot_data.pop(so_mod._so_key(mid), None)

    def ball(*args, **kwargs):
        if natural == "wicket":
            return dict(type="wicket", runs=0, batter_out=True, wicket_type="Bowled")
        if natural in ("wide", "noball"):
            return dict(type="extra", runs=0, is_extra=True,
                        extra_type="Wide" if natural == "wide" else "No Ball")
        return dict(type="run", runs=6 if natural == "six" else 0)

    monkeypatch.setattr(so_mod, "_super_over_ball", ball)
    monkeypatch.setattr(so_mod, "_persist_main_stats", lambda *a, **k: None)
    monkeypatch.setattr(so_mod, "_send_main_scorecard", AsyncMock())
    monkeypatch.setattr(so_mod, "_build_super_over_card", lambda *a, **k: None)
    monkeypatch.setattr(so_mod, "_finalize", finalize)
    monkeypatch.setattr(so_mod, "_BALL_PAUSE", 0)
    ctx = FakeContext()
    state = _tied_state()
    state.update(tournament_id=77, _planned_winner_user_id=winner,
                 _planned_super_overs=planned)
    mid = state["match_id"]

    async def play():
        assert await so_mod.start_super_over(ctx, mid, state)
        await _play_match(ctx, mid)

    asyncio.run(play())
    assert _get(ctx, mid) is None
    assert captured["winner"] == winner
    assert captured["loser"] != winner
    so = captured["so"]
    assert so["so_number"] == (2 if planned == 2 else 1)
    if planned == 2:
        first = so["results"][0]
        assert first[HOST_UID][0] == first[GUEST_UID][0]
    first_uid, second_uid = so["first_bat_uid"], so["bat_uid"]
    first_runs = so["score"][first_uid][0]
    second_runs = so["score"][second_uid][0]
    assert (second_runs > first_runs) == (winner == second_uid)
    assert abs(first_runs - second_runs) <= 6
    # Winner selection must not bypass the two scored innings of the deciding SO.
    assert len(so["so_innings"]) == 2

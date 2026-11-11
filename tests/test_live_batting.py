"""Temporary retirement, saved figures, live ordering and both control surfaces."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from handlers import live_batting as h
from services import cipl_match as cm, live_batting as batting
from services import match_webapp_service as web
from services.match_state_store import (
    A_PICK_BAT_APPROACH, A_PICK_CIPL_BOWLER, A_PICK_DELIVERY, A_PICK_SHOT,
    A_PICK_CIPL_NEW_BATSMAN, A_PICK_NEW_BATSMAN, A_COMPLETED,
)
from tests.test_approach_miniapp_play import _state, _ready_to_bowl, _Script

NA = A_PICK_BAT_APPROACH


def retire(state, out=101, incoming=103, na=NA):
    return batting.retire(state, na, out, incoming, batting.token(state, na))


@pytest.mark.parametrize("outgoing,slot,partner", [(101, "striker_idx", 102), (102, "non_striker_idx", 101)])
def test_retirement_preserves_score_wickets_balls_and_partner(outgoing, slot, partner):
    s = _state()
    s["bat_stats"][str(outgoing)].update(runs=47, balls=23, fours=4, sixes=2,
                                         confidence=82, balls_to_50=26)
    s.update(total_runs=65, current_ball=0, current_over=6,
             partnership_runs=28, partnership_balls=14, free_hit=True)
    before = copy.deepcopy(s["bat_stats"][str(outgoing)])
    retire(s, outgoing, 108)
    st = s["bat_stats"][str(outgoing)]
    assert {k: st[k] for k in before} == before
    assert st["retired"] and st["batted"] and not st["out"]
    assert s["batting_order"][s[slot]]["roster_id"] == 108
    assert partner in [s["batting_order"][s[key]]["roster_id"]
                       for key in ("striker_idx", "non_striker_idx")]
    assert (s["total_runs"], s["total_wickets"], s["current_over"], s["current_ball"]) == (65, 0, 6, 0)
    assert s["free_hit"] and s["next_batsman_idx"] == 2
    assert s["partnership_history"][0]["ended_by"] == "retirement"
    assert (s["partnership_runs"], s["partnership_balls"]) == (0, 0)
    assert outgoing in [p["roster_id"] for p in cm.available_batsmen(s)]


@pytest.mark.parametrize("int_keys", [False, True])
def test_retired_batter_returns_from_saved_figures_and_adds_runs(int_keys):
    s = _state()
    s["bat_stats"]["101"].update(runs=37, balls=20, fours=4)
    s["total_runs"] = 37
    if int_keys:
        s["bat_stats"] = {int(k): v for k, v in s["bat_stats"].items()}
    retire(s, "101", "107")
    s = json.loads(json.dumps(s))  # persisted, or paused/resumed
    assert "resumes on 37 (20)" in retire(s, 107, 101)
    assert not batting.stat(s, 101).get("retired")
    assert batting.stat(s, 107)["retired"]
    with _Script(["4", "0", "0", "0", "0", "0"]):
        cm.simulate_over(_ready_to_bowl(s))
    assert batting.stat(s, 101)["runs"] == 41
    assert batting.stat(s, 101)["balls"] == 26
    assert batting.stat(s, 101)["fours"] == 5
    assert s["total_runs"] == 41


def test_reordering_does_not_move_crease_or_saved_xi_and_controls_auto_promotion():
    s = _state()
    original_xi = copy.deepcopy(s["bat_xi"])
    ids = [p["roster_id"] for p in cm.available_batsmen(s)]
    ids = [ids[-1]] + ids[:-1]
    batting.reorder(s, NA, ids, batting.token(s, NA))
    assert s["bat_xi"] == original_xi
    assert [s["batting_order"][s[k]]["roster_id"] for k in ("striker_idx", "non_striker_idx")] == [101, 102]
    with _Script(["W", "0", "0", "0", "0", "0"]):
        cm.simulate_over(_ready_to_bowl(s))
    assert batting.stat(s, ids[0])["balls"] == 5
    assert s["total_wickets"] == 1


def test_retired_batter_is_available_at_a_wicket_and_not_marked_out():
    s = _state()
    s["bat_stats"]["101"].update(runs=18, balls=12)
    retire(s)
    with _Script(["W", "4", "0", "0", "0", "0"]):
        summary = cm.simulate_over(_ready_to_bowl(s), pause_on_wicket=True)
        assert summary["paused"] and cm.needs_new_batsman(s)
        assert 101 in [p["roster_id"] for p in cm.available_batsmen(s)]
        assert cm.bring_in_batsman(s, 101)["roster_id"] == 101
        assert not batting.stat(s, 101).get("retired")
        cm.simulate_over(s, pause_on_wicket=True)
    assert batting.stat(s, 101)["runs"] == 22
    assert batting.stat(s, 101)["balls"] == 17
    assert s["total_wickets"] == 1


@pytest.mark.parametrize("bad_id,changes", [
    (999, {}), (102, {}), (103, {"out": True}), (103, {"retired_hurt": True}),
])
def test_unavailable_batters_cannot_be_sent_in(bad_id, changes):
    s = _state()
    s["bat_stats"]["103"].update(changes)
    before = copy.deepcopy(s)
    with pytest.raises(ValueError):
        retire(s, incoming=bad_id)
    assert s == before


def test_replaced_and_injured_players_are_excluded_and_no_replacement_is_safe():
    s = _state()
    for p in s["batting_order"][2:]:
        p["active"] = False
    assert not batting.options(s, NA, can_control=True)["waiting"]
    with pytest.raises(ValueError):
        retire(s)
    assert not batting.stat(s, 101).get("retired")


@pytest.mark.parametrize("ids", [[103, 103], [101] + list(range(104, 112)), [999] + list(range(104, 112)), []])
def test_reorder_requires_exactly_the_available_players(ids):
    s = _state()
    before = copy.deepcopy(s)
    with pytest.raises(ValueError):
        batting.reorder(s, NA, ids, batting.token(s, NA))
    assert s == before


@pytest.mark.parametrize("na,changes", [
    (A_PICK_SHOT, {}), (A_PICK_CIPL_NEW_BATSMAN, {}), (A_COMPLETED, {}),
    (NA, {"over_in_progress": {"balls_this_over": 2}}),
    (NA, {"concussion_pending": {"user_id": 7}}),
    (NA, {"setup": "PICKING"}), (NA, {"total_wickets": 10}),
    (NA, {"innings": 2, "target": 1, "total_runs": 1}),
    (NA, {"ball_format": "The100", "current_over": 20, "current_ball": 5}),
])
def test_changes_are_refused_during_play_setup_medical_or_terminal_states(na, changes):
    s = _state()
    s.update(changes)
    assert not batting.options(s, na, can_control=True)["canManage"]
    with pytest.raises(ValueError):
        retire(s, na=na)


def test_old_selection_cannot_be_replayed_even_if_the_order_is_restored():
    s = _state()
    tok = batting.token(s, NA)
    retire(s)
    retire(s, 103, 101)
    before = copy.deepcopy(s)
    with pytest.raises(ValueError, match="moved on"):
        batting.retire(s, NA, 101, 103, tok)
    assert s == before


def test_last_three_can_rotate_the_only_retired_batter_without_ending_innings():
    s = _state()
    for p in s["batting_order"][3:]:
        batting.stat(s, p["roster_id"])["out"] = True
    s["total_wickets"] = 8
    retire(s)
    assert [p["roster_id"] for p in cm.available_batsmen(s)] == [101]
    retire(s, 103, 101)
    assert s["total_wickets"] == 8 and not cm.is_innings_over(s)


def test_zero_ball_retirement_is_visible_in_scorecard_and_archived_at_interval(monkeypatch):
    s = _state()
    retire(s)
    from services.crickidex_arena import _merge_stats, _full_dismissal
    assert _full_dismissal(batting.stat(s, 101)) == batting.RETIRED_LABEL
    assert _merge_stats(s)["101"]["hasBatted"]
    monkeypatch.setattr(web.mwa, "get_state", lambda mid: s)
    rows = web.build_scorecard(42, 7)["innings"][0]["batting"]
    assert any(r["how_out"] == batting.RETIRED_LABEL and r["balls"] == 0 for r in rows)
    cm.end_first_innings(s)
    assert s["inn1_bat_stats"]["101"]["retired"]
    assert not any(st.get("retired") for st in s["bat_stats"].values())


def test_classic_return_and_order_follow_eligible_players_even_below_old_cursor():
    s = _state()
    s.pop("mode")
    s["next_batsman_idx"] = 9  # a captain previously selected out of order
    retire(s, na=A_PICK_DELIVERY)
    batting.stat(s, 103)["out"] = True
    idx = web._next_available_batsman(s)
    assert s["batting_order"][idx]["roster_id"] == 101
    web._install_new_batsman(s, idx)
    assert not batting.stat(s, 101).get("retired")


def test_chat_return_after_last_ball_wicket_keeps_the_surviving_partner(monkeypatch):
    from handlers import match as match_handler
    s = _state()
    retire(s)
    s.update(striker_idx=1, non_striker_idx=0, current_over=2, current_ball=0,
             total_wickets=1)
    batting.stat(s, 103)["out"] = True
    monkeypatch.setattr(match_handler, "_gs", lambda *a: s)
    monkeypatch.setattr(match_handler, "_ss", lambda *a, **kw: None)
    monkeypatch.setattr(match_handler, "get_next_action", lambda *a: A_PICK_NEW_BATSMAN)
    monkeypatch.setattr(match_handler, "_cancel_action_timer", lambda *a: None)
    monkeypatch.setattr(match_handler, "_send_batsman_card", AsyncMock())
    monkeypatch.setattr(match_handler, "render_screen", AsyncMock())
    q = SimpleNamespace(data="newbat_42_2", from_user=SimpleNamespace(id=s["bat_user_tg"]),
                        answer=AsyncMock(), edit_message_text=AsyncMock())
    ctx = SimpleNamespace(bot_data={})
    asyncio.run(match_handler.new_batsman_callback(SimpleNamespace(callback_query=q), ctx))
    assert s["batting_order"][s["striker_idx"]]["roster_id"] == 102
    assert s["batting_order"][s["non_striker_idx"]]["roster_id"] == 101
    assert not batting.stat(s, 101).get("retired")


def _wire(monkeypatch, s, na=NA):
    from handlers import cipl_play as cp
    box = {"state": s}
    saved = AsyncMock()
    async def save(ctx, mid, state, **kwargs):
        await saved(ctx, mid, state, **kwargs)
        box["state"] = state
    monkeypatch.setattr(cp, "_gs", AsyncMock(side_effect=lambda *a: box["state"]))
    monkeypatch.setattr(cp, "_get_next_action", AsyncMock(return_value=na))
    monkeypatch.setattr(cp, "_ss", save)
    monkeypatch.setattr(cp, "_super_over_active", lambda *a: False)
    monkeypatch.setattr(h, "get_match_lock", lambda mid: asyncio.Lock())
    context = SimpleNamespace(bot_data={}, bot=SimpleNamespace(send_message=AsyncMock()))
    return box, saved, context


def test_shared_submission_is_captain_only_and_does_not_change_turn_or_timer(monkeypatch):
    s = _state(play_mode="app")
    box, saved, ctx = _wire(monkeypatch, s)
    action = {"outRosterId": 101, "inRosterId": 106, "token": batting.token(s, NA)}
    assert not asyncio.run(h.submit_change(ctx, 42, 12345, "retire", action, source="app"))[0]
    saved.assert_not_awaited()
    accept = AsyncMock()
    assert asyncio.run(h.submit_change(ctx, 42, s["bat_user_tg"], "retire", action, source="app", on_accept=accept))[0]
    assert saved.call_args.kwargs["next_action"] == NA
    assert box["state"]["current_over"] == 1
    accept.assert_awaited_once()
    assert not asyncio.run(h.submit_change(ctx, 42, s["bat_user_tg"], "retire", action, source="app"))[0]
    assert saved.await_count == 1


def test_chat_picker_returns_retired_options_and_applies_choice(monkeypatch):
    s = _state()
    retire(s)
    box, saved, ctx = _wire(monkeypatch, s)
    q = SimpleNamespace(data=f"lb_r_42_{batting.token(s, NA)}_103_101",
                        from_user=SimpleNamespace(id=s["bat_user_tg"]),
                        answer=AsyncMock(), edit_message_text=AsyncMock())
    asyncio.run(h.batting_callback(SimpleNamespace(callback_query=q), ctx))
    assert not batting.stat(box["state"], 101).get("retired")
    saved.assert_awaited_once()
    assert "Manage batting" in q.edit_message_text.call_args.args[0]


def test_opening_management_from_plan_picker_preserves_plan_buttons(monkeypatch):
    s = _state()
    box, saved, ctx = _wire(monkeypatch, s)
    q = SimpleNamespace(data="lb_n_42", from_user=SimpleNamespace(id=s["bat_user_tg"]),
                        answer=AsyncMock(), edit_message_text=AsyncMock())
    asyncio.run(h.batting_callback(SimpleNamespace(callback_query=q), ctx))
    q.edit_message_text.assert_not_awaited()
    ctx.bot.send_message.assert_awaited_once()
    saved.assert_not_awaited()


def test_batting_write_excludes_delivery_bowler_and_ai_mutations(monkeypatch):
    mid = 876541
    assert web._claim_action(mid)
    monkeypatch.setattr(web.mwa, "get_state", lambda mid: pytest.fail("Busy actions must not read stale state"))
    try:
        assert not web.set_delivery(mid, 7, "Seam", "Good")[0]
        assert not web.select_new_bowler(mid, 7, 501)[0]
        assert web.auto_play_bot_turns(None, mid) == []
    finally:
        web._release_action(mid)


def test_miniapp_dispatch_uses_shared_submission_and_does_not_play_a_ball(monkeypatch):
    import admin
    from services import bot_bridge, crickidex_arena
    s = _state(play_mode="app")
    box, saved, ctx = _wire(monkeypatch, s)
    db = SimpleNamespace(close=lambda: None)
    user = SimpleNamespace(id=7, telegram_id=s["bat_user_tg"])
    match = SimpleNamespace(id=42)
    monkeypatch.setattr(admin, "get_session", lambda: db)
    monkeypatch.setattr(admin, "_match_rest_user_and_match", lambda *a: (user, match, None))
    monkeypatch.setattr(web.mwa, "get_state", lambda mid: box["state"])
    monkeypatch.setattr(crickidex_arena, "serialize_match_state", lambda *a: {"updated": True})
    monkeypatch.setattr(bot_bridge, "submit_and_wait_for_accept",
                        lambda factory: asyncio.run(factory(ctx, AsyncMock())))
    action = {"outRosterId": 101, "inRosterId": 105, "token": batting.token(s, NA)}
    with admin.app.test_request_context("/api/match/action", method="POST", json={
            "userId": 7, "matchId": 42, "type": "retire_batsman", "action": action}):
        result = admin.match_rest_action()
    assert result["ok"] and result["matchState"]["updated"]
    assert batting.stat(box["state"], 101)["retired"]
    assert box["state"]["current_ball"] == 0
    saved.assert_awaited_once()

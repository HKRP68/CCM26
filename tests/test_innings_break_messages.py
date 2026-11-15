"""The chase equation must use the full allocation and the new batting side."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Bot, InlineKeyboardButton

from handlers import cipl_play as cp
from services import innings_break_messages as ui, rich_message as R
from tests.test_approach_miniapp_play import _state
from tests.test_rich_message_defaults import TOKEN, delivery  # noqa: F401
from tests.test_rich_text_surfaces import flatten


@pytest.mark.parametrize("balls,hundred,expected_rate", [(120, False, "9.05 runs/over"),
                                                       (30, False, "36.20 runs/over"),
                                                       (100, True, "1.81 runs/ball")])
def test_chase_target_allocation_and_rate_agree_in_both_renderings(balls, hundred, expected_rate):
    blocks, text = ui.build(first_team="A <XI>", chasing_team="B & Co", runs=180, wickets=10,
                            progress="7.2 overs", balls=balls, hundred=hundred,
                            next_action="Follow the next prompt.", scorecard="<b>Top batter</b>: 80 (40)")
    assert json.loads(json.dumps(blocks)) == blocks
    for fact in ("180/10", "181 to win", f"{balls} balls", expected_rate, "7.2 overs", "80 (40)"):
        assert fact in text and fact in flatten(blocks)
    assert "A &lt;XI&gt;" in text and "B &amp; Co" in text
    details = next(block for block in blocks if block["type"] == "details")
    assert "80 (40)" in flatten(details)


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("hundred", [True, False])
def test_break_transitions_once_preserves_controls_and_prompts_chase(monkeypatch, delivery, native, hundred):
    monkeypatch.setattr(R.config, "RICH_TEXT_ENABLED", native)
    state = _state(bot_match=False)
    state.update(total_runs=180, total_wickets=6, current_over=21, current_ball=0,
                 bat_team_name="A <XI>", bowl_team_name="B & Co")
    if hundred:
        state["ball_format"] = "The100"
        state["play_mode"] = "app"
    for name in ("_delete_prev_over", "_ss", "_prompt_bowler"):
        monkeypatch.setattr(cp, name, AsyncMock())
    monkeypatch.setattr(cp, "_between_overs_row", lambda *a: [[
        InlineKeyboardButton("Impact Player", callback_data="impact_42")]])
    context = SimpleNamespace(bot=Bot(TOKEN))
    asyncio.run(cp._innings_break(context, 42, state))
    endpoint, payload, _ = delivery.calls[0]
    assert endpoint == ("sendRichMessage" if native else "sendMessage")
    rendered = flatten(payload["rich_message"]["blocks"]) if native else payload["text"]
    assert "181 to win" in rendered and "180/6" in rendered
    assert ("100 balls" if hundred else "120 balls") in rendered
    if hundred:
        assert "20 overs" not in rendered and "Mini App" in rendered
    else:
        assert "9.05 runs/over" in rendered
    assert payload["reply_markup"].inline_keyboard[0][0].callback_data == "impact_42"
    assert state["innings"] == 2 and state["target"] == 181
    assert state["bat_team_name"] == "B & Co" and state["total_runs"] == 0
    assert state["over_msg_ids"] == [77] and state["action_msg_id"] is None
    cp._ss.assert_awaited_once_with(context, 42, state, next_action=cp.A_PICK_CIPL_BOWLER)
    cp._prompt_bowler.assert_awaited_once_with(context, 42, state, first=True)


def test_failed_break_card_still_starts_the_chase(monkeypatch):
    state = _state(bot_match=False)
    state.update(total_runs=90, total_wickets=10, current_over=8, current_ball=2)
    for name in ("_delete_prev_over", "_ss", "_prompt_bowler"):
        monkeypatch.setattr(cp, name, AsyncMock())
    monkeypatch.setattr(cp, "_between_overs_row", lambda *a: None)
    monkeypatch.setattr(R, "send_rich_message", AsyncMock(side_effect=RuntimeError("offline")))
    context = SimpleNamespace(bot=SimpleNamespace())
    asyncio.run(cp._innings_break(context, 42, state))
    assert state["target"] == 91 and state["innings"] == 2
    assert state["over_msg_ids"] == []
    cp._prompt_bowler.assert_awaited_once_with(context, 42, state, first=True)


def test_autoplay_break_names_the_archived_first_team_and_new_chasing_side():
    from services.wsp_autoplay import _innings_break_text
    text = _innings_break_text(dict(inn1_runs=175, inn1_wickets=9, inn1_team="First XI",
                                   inn1_overs="20.0", bat_team_name="Chasers", bowl_team_name="First XI", overs=20))
    assert "First XI 175/9" in text and "Chasers need 176 to win" in text
    assert "8.80 runs/over" in text and "automatically" in text

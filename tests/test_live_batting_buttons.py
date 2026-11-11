"""Embedded Telegram callbacks and the keyboard used when rich sending fails."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from handlers import cipl_play as cp, letsplay, live_batting as handler
from services import live_batting_help as ui, rich_message as rich
from tests.test_approach_miniapp_play import _state, _ready_to_bowl
from tests.test_live_batting import _wire


def embedded_buttons(node):
    if isinstance(node, dict):
        if node.get("type") == "button":
            return [node["button"]]
        return [b for value in node.values() for b in embedded_buttons(value)]
    if isinstance(node, (list, tuple)):
        return [b for value in node for b in embedded_buttons(value)]
    return []


def context():
    return SimpleNamespace(bot_data={}, job_queue=None, bot=SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=10)),
        edit_message_text=AsyncMock(), delete_message=AsyncMock(),
        pin_chat_message=AsyncMock()))


def test_rich_entry_sits_in_partnership_paragraph_and_matches_keyboard():
    state = _state(bot_match=False)
    blocks = cp._approach_card_blocks(state)
    paragraph = next(b for b in blocks if b["type"] == "paragraph" and embedded_buttons(b))
    assert "Partnership" in str(paragraph)
    embedded = embedded_buttons(paragraph)
    assert len(embedded) == 1
    keyboard = ui.with_manage_batting(state, None)
    assert embedded[0]["callback_data"] == keyboard[-1][0].callback_data == "lb_n_42"
    assert embedded[0]["text"] == keyboard[-1][0].text == "🏏 Manage batting"


@pytest.mark.parametrize("prompt", ["_prompt_bowler", "_prompt_bowl_approach", "_prompt_bat_approach"])
def test_all_over_pickers_keep_one_manage_entry_without_a_miniapp_link(monkeypatch, prompt):
    state = _ready_to_bowl(_state(bot_match=False))
    ctx = context()
    monkeypatch.setattr(cp, "_miniapp_row", lambda state: None)
    monkeypatch.setattr(cp, "_mark_prompt_delivered", AsyncMock())
    monkeypatch.setattr(rich, "rich_text_enabled", lambda: False)
    asyncio.run(getattr(cp, prompt)(ctx, 42, state))
    calls = ctx.bot.send_message.call_args_list + ctx.bot.edit_message_text.call_args_list
    buttons = [b for call in calls for row in call.kwargs["reply_markup"].inline_keyboard for b in row]
    assert len([b for b in buttons if b.callback_data == "lb_n_42"]) == 1
    assert any((b.callback_data or "").startswith("cipl_") for b in buttons)


@pytest.mark.parametrize("announce", ["cipl", "letsplay"])
def test_pinned_start_card_has_manage_entry_without_a_miniapp_link(monkeypatch, announce):
    state = _state(bot_match=False)
    ctx = context()
    monkeypatch.setattr(cp, "_miniapp_row", lambda state: None)
    if announce == "cipl":
        asyncio.run(cp._cleanup_setup_and_announce(ctx, state, {}))
    else:
        asyncio.run(letsplay._announce(ctx, state, "Hard"))
    markup = ctx.bot.send_message.call_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[-1][0].callback_data == "lb_n_42"
    assert state["pinned_msg_id"] == 10


@pytest.mark.parametrize("changes", [
    {"bat_user_tg": -1}, {"play_mode": "app"},
    {"concussion_pending": {"user_id": 7}},
    {"over_in_progress": {"balls_this_over": 2}},
    {"current_over": 21}, {"total_wickets": 10},
    {"innings": 2, "target": 1, "total_runs": 1},
])
def test_unavailable_or_app_played_batting_does_not_offer_a_chat_callback(changes):
    state = _state()
    state.update(changes)
    assert ui.manage_batting_button(state) is None
    assert not embedded_buttons(cp._approach_card_blocks(state))
    assert ui.with_manage_batting(state, None) is None


def test_entry_does_not_modify_or_duplicate_existing_keyboard():
    state = _state()
    original = InlineKeyboardMarkup([[InlineKeyboardButton("View Match", url="https://example.com")]])
    once = ui.with_manage_batting(state, original)
    twice = ui.with_manage_batting(state, once)
    assert len(original.inline_keyboard) == 1
    assert len(once) == len(twice) == 2
    assert twice[0][0].url == "https://example.com"
    assert twice[1][0].callback_data == "lb_n_42"


@pytest.mark.parametrize("refuse_rich", [False, True])
def test_native_callback_or_html_fallback_keeps_an_actionable_button(monkeypatch, refuse_rich):
    state = _state()
    ctx = context()
    monkeypatch.setattr(cp, "_miniapp_row", lambda state: None)
    monkeypatch.setattr(rich.config, "RICH_TEXT_ENABLED", True)
    monkeypatch.setattr(rich, "_unsupported", False)
    payload = {"message_id": 44, "date": 0,
               "chat": {"id": state["chat_id"], "type": "supergroup"}}
    ctx.bot._post = (AsyncMock(side_effect=BadRequest("Unknown RichText type button"))
                     if refuse_rich else AsyncMock(return_value=payload))
    asyncio.run(cp._new_action_message(
        ctx, state, "Batting scorecard", [], blocks=cp._approach_card_blocks(state)))
    endpoint, sent = ctx.bot._post.call_args.args
    assert endpoint == "sendRichMessage"
    assert embedded_buttons(sent["rich_message"])[0]["callback_data"] == "lb_n_42"
    if refuse_rich:
        ctx.bot.send_message.assert_awaited_once()
        markup = ctx.bot.send_message.call_args.kwargs["reply_markup"]
        assert markup.inline_keyboard[-1][0].callback_data == "lb_n_42"
    else:
        ctx.bot.send_message.assert_not_awaited()
        assert state["action_msg_id"] == 44


def test_opposing_captain_cannot_open_the_embedded_batting_controls(monkeypatch):
    state = _state(bot_match=False)
    _box, saved, ctx = _wire(monkeypatch, state)
    query = SimpleNamespace(data="lb_n_42", from_user=SimpleNamespace(id=8008),
                            answer=AsyncMock(), edit_message_text=AsyncMock())
    asyncio.run(handler.batting_callback(SimpleNamespace(callback_query=query), ctx))
    assert "Only the current batting captain" in query.answer.call_args.args[0]
    ctx.bot.send_message.assert_not_awaited()
    query.edit_message_text.assert_not_awaited()
    saved.assert_not_awaited()

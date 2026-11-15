"""Match tools collapse into the message without moving the current choices."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from handlers import cipl_play as cp
from services import match_controls as ui, rich_message as R
from services.rich_message_html import render
from tests.test_live_batting_buttons import embedded_buttons
from tests.test_rich_message_defaults import TOKEN, delivery  # noqa: F401


def tools():
    return [[InlineKeyboardButton("View Match", url="https://t.me/Bot/Game?startapp=cricket_7_42"),
             InlineKeyboardButton("Impact Player", callback_data="cipl_imp_7")],
            [InlineKeyboardButton("Manage batting", callback_data="lb_n_7")],
            [InlineKeyboardButton("Live scores & stats", url="https://example.com/stats")]]


def menu(blocks):
    return next(block for block in blocks if block.get("summary") == ui.TITLE)


def test_all_tools_are_collapsed_and_have_the_original_callbacks_and_links():
    body = [R.heading("Choose approach")]
    original = deepcopy(body)
    blocks, text = ui.attach({}, "Choose approach", body, tools())
    assert body == original
    assert len(blocks) == 2 and not menu(blocks).get("is_open")
    assert [button["callback_data"] for button in embedded_buttons(menu(blocks))] == ["cipl_imp_7", "lb_n_7"]
    native_html = render(blocks)
    assert '<details><summary>⚙️ Manage match</summary>' in native_html
    assert 'data="cipl_imp_7"' in native_html and 'data="lb_n_7"' in native_html
    assert "startapp=cricket_7_42" in native_html and "https://example.com/stats" in native_html
    assert "<blockquote expandable>" in text
    assert "/impact" in text and "/managebatting" in text


def test_repeated_render_replaces_tools_and_removes_unavailable_controls():
    blocks, text = ui.attach({}, "Score", [R.heading("Score")], tools())
    blocks, text = ui.attach({}, text, blocks, [tools()[2]])
    assert len([block for block in blocks if block.get("summary") == ui.TITLE]) == 1
    assert text.count(ui.TITLE) == 1 and "/impact" not in text
    assert not embedded_buttons(menu(blocks))
    blocks, text = ui.attach({}, text, blocks, [])
    assert not any(block.get("summary") == ui.TITLE for block in blocks)
    assert text == "Score"


def test_private_webapp_button_becomes_a_telegram_miniapp_launch_link(monkeypatch):
    monkeypatch.setenv("BOT_USERNAME", "CricketBot")
    monkeypatch.setenv("MINIAPP_NAME", "Cricket")
    original = InlineKeyboardButton("Play in Mini App", web_app=WebAppInfo(url="https://example.com/cricket?match_id=7&chat_id=42"))
    blocks, text = ui.attach(dict(match_id=7, chat_id=42), "Score", None, [[original]])
    expected = "https://t.me/CricketBot/Cricket?startapp=cricket_7_42"
    assert expected in text and expected in render(blocks)


@pytest.mark.parametrize("delivery_mode", ["native", "disabled", "missing"])
@pytest.mark.parametrize("edit", [False, True])
def test_choices_stay_outside_and_utilities_stay_inside_on_send_and_edit(monkeypatch, delivery, delivery_mode, edit):
    monkeypatch.setattr(R.config, "RICH_TEXT_ENABLED", delivery_mode != "disabled")
    if delivery_mode == "missing":
        delivery.wire.missing = True
    monkeypatch.setattr(cp, "_view_and_impact_rows", lambda state: tools())
    state = dict(match_id=7, chat_id=42, over_msg_ids=[77] if edit else [])
    if edit:
        state["action_msg_id"] = 77
    keyboard = [[InlineKeyboardButton("Attack", callback_data="cipl_batapp_7_1")]]
    context = SimpleNamespace(bot=Bot(TOKEN))
    method = cp._edit_action_message if edit else cp._new_action_message
    asyncio.run(method(context, state, "Choose approach", keyboard, blocks=[R.heading("Choose approach")]))
    endpoint, payload, _ = delivery.calls[-1]
    rows = payload["reply_markup"].inline_keyboard
    assert len(rows) == 1 and len(rows[0]) == 1
    assert rows[0][0].callback_data == "cipl_batapp_7_1"
    if delivery_mode == "native":
        section = menu(payload["rich_message"]["blocks"])
        assert len(embedded_buttons(section)) == 2
        assert not section.get("is_open")
    else:
        assert endpoint == ("editMessageText" if edit else "sendMessage")
        assert "rich_message" not in payload
        assert "⚙️ Manage match" in payload["text"] and "/impact" in payload["text"]
        assert "/managebatting" in payload["text"]
    assert state["action_msg_id"] == 77


def test_broadcast_keeps_the_match_launch_inside_the_section(monkeypatch, delivery):
    from services import match_broadcast
    from tests.test_rich_text_surfaces import _match_state
    state = _match_state()
    state.update(chat_id=42, match_id=7)
    monkeypatch.setattr(match_broadcast, "play_match_keyboard", lambda *a, **k:
                        InlineKeyboardMarkup([[tools()[0][0]]]))
    asyncio.run(match_broadcast.broadcast_scorecard(SimpleNamespace(bot=Bot(TOKEN)), 7, state))
    endpoint, payload, _ = delivery.calls[0]
    assert endpoint == "sendRichMessage" and payload.get("reply_markup") is None
    assert "startapp=cricket_7_42" in render([menu(payload["rich_message"]["blocks"])])

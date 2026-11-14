"""Wire contract from https://core.telegram.org/bots/api (Bot API 10.4).

These checks deliberately reject old invented caption/checklist/quotation
fields: JSON serialization alone cannot establish Telegram compatibility.
"""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, InvalidToken, TelegramError

from services import rich_message as R


@pytest.fixture(autouse=True)
def native(monkeypatch):
    monkeypatch.setattr(R.config, "RICH_TEXT_ENABLED", True)
    R.reset_support_latch()
    yield
    R.reset_support_latch()


def text(value):
    if isinstance(value, str):
        return
    if isinstance(value, list):
        for child in value:
            text(child)
        return
    kind = value["type"]
    if kind in ("bold", "italic", "underline", "strikethrough", "spoiler", "code"):
        assert set(value) == {"type", "text"}
        text(value["text"])
    elif kind == "url":
        assert set(value) == {"type", "text", "url"}
        text(value["text"])
    elif kind == "date_time":
        assert set(value) == {"type", "text", "unix_time", "date_time_format"}
        assert isinstance(value["unix_time"], int)
        text(value["text"])
    elif kind == "button":
        button(value["button"])
    elif kind in ("reference", "reference_link"):
        assert set(value) == {
            "type",
            "text",
            "name" if kind == "reference" else "reference_name",
        }
        text(value["text"])
    else:
        raise AssertionError("Undocumented inline type: " + kind)


def button(value):
    assert set(value) <= {"text", "style", "callback_data", "url"}
    assert ("callback_data" in value) != ("url" in value)
    assert value.get("style", "primary") in ("primary", "success", "danger", "link")
    if "callback_data" in value:
        assert 1 <= len(value["callback_data"].encode()) <= 64
    text(value["text"])


def blocks(value):
    allowed = {
        "paragraph": {"type", "text"},
        "heading": {"type", "text", "size"},
        "footer": {"type", "text"},
        "pre": {"type", "text", "language"},
        "divider": {"type"},
        "pullquote": {"type", "text", "credit"},
        "blockquote": {"type", "blocks", "credit"},
        "expandable_blockquote": {"type", "text", "credit"},
        "details": {"type", "summary", "blocks", "is_open"},
        "list": {"type", "items"},
        "table": {
            "type",
            "cells",
            "is_bordered",
            "is_striped",
            "is_compact",
            "caption",
        },
        "buttons": {"type", "buttons", "align"},
    }
    for row in value:
        kind = row["type"]
        assert set(row) <= allowed[kind]
        if "text" in row:
            text(row["text"])
        if "credit" in row:
            text(row["credit"])
        if "summary" in row:
            text(row["summary"])
        if "caption" in row:
            text(row["caption"])
        if kind == "heading":
            assert 1 <= row["size"] <= 6
        if "blocks" in row:
            blocks(row["blocks"])
        if kind == "table":
            for line in row["cells"]:
                for cell in line:
                    assert set(cell) <= {
                        "text",
                        "is_header",
                        "colspan",
                        "rowspan",
                        "align",
                        "valign",
                    }
                    assert cell["align"] in ("left", "center", "right") and cell[
                        "valign"
                    ] in ("top", "middle", "bottom")
                    if "text" in cell:
                        text(cell["text"])
        if kind == "list":
            for item in row["items"]:
                assert set(item) <= {
                    "blocks",
                    "has_checkbox",
                    "is_checked",
                    "value",
                    "type",
                }
                blocks(item["blocks"])
                if "has_checkbox" in item:
                    assert item["has_checkbox"] is True
                if "is_checked" in item:
                    assert item["is_checked"] is True
        if kind == "buttons":
            assert 1 <= len(row["buttons"]) <= 8
            for item in row["buttons"]:
                button(item)


def bot():
    async def post(endpoint, payload):
        content = payload["rich_message"]
        assert len(set(content) & {"blocks", "html", "markdown"}) == 1
        if "blocks" in content:
            blocks(content["blocks"])
        return {"message_id": 7, "date": 0, "chat": {"id": 42, "type": "private"}}

    return SimpleNamespace(
        _post=AsyncMock(side_effect=post), send_message=AsyncMock(return_value="legacy")
    )


def sample():
    return [
        R.heading("🏏 CMU Draft", 2),
        R.pullquote(R.bold("Root & Co"), caption="Signed by North"),
        R.table(
            [
                [R.cell("Player", header=True), R.cell("Purse", header=True)],
                [R.cell("Joe <Root>"), R.cell(R.bold("₹74.5 Cr"), align="right")],
            ],
            compact=True,
        ),
        R.details("Squad", [R.paragraph("Joe Root")]),
        R.checklist([(True, "Bowler"), (False, "Keeper")]),
        R.list_block(["First", "Second"], ordered=True),
        R.blockquote([R.paragraph("Rules")], expandable=True),
        R.paragraph(R.date_time("Deadline", 1800000000)),
        R.paragraph(R.footnote("Rule", "Minimum one keeper")),
    ]


def test_current_cards_use_documented_block_shapes_and_exact_keyboard_callbacks():
    client = bot()
    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Confirm", callback_data="fd_y_quote"),
                InlineKeyboardButton("❌ Cancel", callback_data="fd_n_quote"),
            ]
        ]
    )
    result = asyncio.run(
        R.send_rich_message(client, 42, sample(), "<b>Draft</b>", reply_markup=markup)
    )
    assert result.message_id == 7
    payload = client._post.call_args.args[1]
    assert "reply_markup" not in payload
    buttons = payload["rich_message"]["blocks"][-1]["buttons"]
    assert [b["callback_data"] for b in buttons] == ["fd_y_quote", "fd_n_quote"]
    assert [b["style"] for b in buttons] == ["success", "danger"]
    client.send_message.assert_not_awaited()


def test_old_saved_event_snapshots_are_upgraded_without_mutating_the_archive():
    old = [
        {"type": "pullquote", "text": "Root", "caption": "North"},
        {
            "type": "blockquote",
            "is_expandable": True,
            "blocks": [{"type": "paragraph", "text": "Rules"}],
        },
        {
            "type": "list",
            "is_checkbox": True,
            "items": [
                {
                    "blocks": [{"type": "paragraph", "text": "Bowler"}],
                    "is_checked": False,
                }
            ],
        },
        {
            "type": "list",
            "items": [
                {"label": "1.", "blocks": [{"type": "paragraph", "text": "First"}]}
            ],
        },
    ]
    saved = deepcopy(old)
    client = bot()
    asyncio.run(R.send_rich_message(client, 42, old, "<b>Root</b>"))
    assert old == saved
    normalized = client._post.call_args.args[1]["rich_message"]["blocks"]
    assert (
        normalized[0]["credit"] == "North"
        and normalized[1]["type"] == "expandable_blockquote"
    )
    client.send_message.assert_not_awaited()


def test_block_refusal_retries_native_rich_html_with_tables_buttons_and_escaped_names():
    client = bot()
    success = client._post.side_effect

    async def refuse_blocks(endpoint, payload):
        if "blocks" in payload["rich_message"]:
            raise BadRequest("Block is not supported in this message")
        return await success(endpoint, payload)

    client._post.side_effect = refuse_blocks
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Confirm", callback_data="pick:1")]]
    )
    asyncio.run(
        R.send_rich_message(client, 42, sample(), "<b>Draft</b>", reply_markup=markup)
    )
    body = client._post.call_args.args[1]["rich_message"]["html"]
    for fragment in (
        "<table",
        "<details>",
        "<aside>",
        "<cite>Signed by North</cite>",
        "<tg-button-row",
        'data="pick:1"',
        "Joe &lt;Root&gt;",
    ):
        assert fragment in body
    client.send_message.assert_not_awaited()
    client._post.side_effect = success
    asyncio.run(R.send_rich_message(client, 42, sample(), "<b>Draft</b>"))
    assert "blocks" in client._post.call_args.args[1]["rich_message"]


def test_missing_method_is_scoped_to_one_bot_and_automatically_recovers(monkeypatch):
    instant = [100.0]
    monkeypatch.setattr(R.time, "monotonic", lambda: instant[0])
    old, new = bot(), bot()
    success = old._post.side_effect
    old._post.side_effect = InvalidToken("Unknown method sendRichMessage")
    asyncio.run(R.send_rich_message(old, 42, sample(), "<b>Fallback</b>"))
    assert not R.rich_text_enabled(old) and R.rich_text_enabled(new)
    asyncio.run(R.send_rich_message(new, 42, sample(), "<b>Fallback</b>"))
    new.send_message.assert_not_awaited()
    instant[0] += R.UNSUPPORTED_RETRY_SECONDS + 1
    old._post.side_effect = success
    asyncio.run(R.send_rich_message(old, 42, sample(), "<b>Fallback</b>"))
    assert R.rich_text_enabled(old) and old.send_message.await_count == 1


def test_failed_rich_edit_does_not_turn_off_sending_for_the_same_bot():
    client = bot()
    success = client._post.side_effect

    async def old_edit(endpoint, payload):
        if endpoint == "editMessageText":
            raise InvalidToken("Unknown method editMessageText")
        return await success(endpoint, payload)

    client._post.side_effect = old_edit
    assert not asyncio.run(R.edit_rich_message(client, 42, 7, sample()))
    assert (
        asyncio.run(
            R.send_rich_message(client, 42, sample(), "<b>Draft</b>")
        ).message_id
        == 7
    )
    client.send_message.assert_not_awaited()


def test_native_requests_preserve_topics_protection_and_reply_context():
    client = bot()
    asyncio.run(
        R.send_rich_message(
            client,
            42,
            sample(),
            "<b>Draft</b>",
            reply_to_message_id=99,
            message_thread_id=123,
            protect_content=True,
            disable_notification=True,
        )
    )
    payload = client._post.call_args.args[1]
    assert payload["message_thread_id"] == 123 and payload["reply_parameters"] == {
        "message_id": 99
    }
    assert payload["protect_content"] and payload["disable_notification"]
    assert "allow_paid_broadcast" not in payload


def test_button_feedback_uses_10_4_private_callback_parameters():
    client = bot()
    # Older SDK Message parsers expect message_id; ephemeral replies have their
    # own ID. This path must accept the successful raw API response directly.
    client._post.side_effect = None
    client._post.return_value = {"ephemeral_message_id": 8}
    query = SimpleNamespace(
        id="callback-123",
        get_bot=lambda: client,
        message=SimpleNamespace(chat_id=-100123),
    )
    assert asyncio.run(R.answer_callback_rich(query, sample(), "<b>Access denied</b>"))
    payload = client._post.call_args.args[1]
    assert payload["ephemeral_message_parameters"] == {
        "callback_query_id": "callback-123"
    }
    assert "receiver_user_id" not in payload and payload["chat_id"] == -100123
    client.send_message.assert_not_awaited()


def test_private_reply_never_falls_back_to_a_public_group_message():
    client = bot()
    client._post.side_effect = InvalidToken("Unknown method sendRichMessage")
    with pytest.raises(TelegramError, match="no public message"):
        asyncio.run(
            R.send_rich_message(
                client,
                -100123,
                sample(),
                "<b>Private</b>",
                ephemeral_message_parameters={"callback_query_id": "callback-123"},
            )
        )
    client.send_message.assert_not_awaited()


def test_html_only_surfaces_still_use_native_rich_messages():
    client = bot()
    asyncio.run(R.send_rich_message(client, 42, None, "<b>Settings</b>"))
    assert client._post.call_args.args[1]["rich_message"] == {"html": "<b>Settings</b>"}
    client.send_message.assert_not_awaited()


def test_old_server_fallback_is_styled_chunked_and_keeps_last_page_buttons():
    client = bot()
    client._post.side_effect = InvalidToken("Unknown method sendRichMessage")
    body = "\n\n".join(f"<b>Player {i}</b> " + ("x" * 150) for i in range(70))
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Refresh", callback_data="refresh")]]
    )
    asyncio.run(R.send_rich_message(client, 42, sample(), body, reply_markup=markup))
    calls = client.send_message.call_args_list
    assert len(calls) > 1
    assert sum(call.args[1].count("<b>Player") for call in calls) == 70
    assert all(call.kwargs["parse_mode"] == "HTML" for call in calls)
    assert calls[-1].kwargs["reply_markup"] == markup
    assert all(call.kwargs["reply_markup"] is None for call in calls[:-1])


def test_specialized_keyboard_actions_remain_on_the_original_keyboard():
    from telegram import CopyTextButton

    client = bot()
    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Refresh", callback_data="refresh"),
                InlineKeyboardButton("Copy ID", copy_text=CopyTextButton("123")),
            ]
        ]
    )
    asyncio.run(
        R.send_rich_message(client, 42, sample(), "<b>Draft</b>", reply_markup=markup)
    )
    payload = client._post.call_args.args[1]
    assert payload["reply_markup"] == markup
    assert not any(
        row["type"] == "buttons" for row in payload["rich_message"]["blocks"]
    )


def test_long_callback_card_is_edited_natively_before_any_new_replies():
    client = bot()
    message = SimpleNamespace(chat_id=42, message_id=7, reply_text=AsyncMock())
    query = SimpleNamespace(
        message=message, get_bot=lambda: client, edit_message_text=AsyncMock()
    )
    body = "\n\n".join(f"<b>Player {i}</b> " + ("x" * 150) for i in range(70))
    assert asyncio.run(R.edit_rich(query, sample(), body))
    assert client._post.call_args.args[0] == "editMessageText"
    message.reply_text.assert_not_awaited()
    query.edit_message_text.assert_not_awaited()


def test_long_callback_fallback_keeps_every_row_and_last_page_controls(monkeypatch):
    monkeypatch.setattr(R.config, "RICH_TEXT_ENABLED", False)
    message = SimpleNamespace(chat_id=42, message_id=7, reply_text=AsyncMock())
    query = SimpleNamespace(message=message, edit_message_text=AsyncMock())
    body = "\n\n".join(f"<b>Player {i}</b> " + ("x" * 150) for i in range(70))
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Refresh", callback_data="refresh")]]
    )
    assert asyncio.run(R.edit_rich(query, sample(), body, reply_markup=markup))
    calls = message.reply_text.call_args_list
    assert len(calls) > 1
    assert sum(call.args[0].count("<b>Player") for call in calls) == 70
    assert all(call.kwargs["parse_mode"] == "HTML" for call in calls)
    assert calls[-1].kwargs["reply_markup"] == markup
    query.edit_message_text.assert_not_awaited()

"""Aligned shared facts and focused match, batting-order and milestone cards."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from services import rich_message as R
from services.rich_layout import aligned_blocks, plain


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def test_general_fact_tables_keep_mentions_and_align_numbers():
    blocks = aligned_blocks(
        '<b>Profile</b>\nOwner: <a href="tg://user?id=7">Root &amp; Co</a>\nCoins: <b>25,000</b>\nSquad: 11/15'
    )
    table = next(block for block in blocks if block["type"] == "table")
    assert (
        table["cells"][1][1]["align"] == "right"
        and table["cells"][2][1]["align"] == "right"
    )
    assert any(node.get("url") == "tg://user?id=7" for node in walk(blocks))
    assert "Root & Co" in plain(table["cells"][0][1]["text"])


def test_complex_html_and_clock_times_are_not_reinterpreted_as_fact_tables():
    assert aligned_blocks("<blockquote expandable>Long report</blockquote>") is None
    assert aligned_blocks("12:30\nhttps://example.com") is None


def test_expandable_fact_quotes_become_expandable_tables():
    blocks = aligned_blocks(
        "<b>Profile</b>\n<blockquote expandable>Coins: <b>25,000</b>\nSquad: 11/15</blockquote>"
    )
    details = next(node for node in blocks if node["type"] == "details")
    assert details["blocks"][0]["type"] == "table"
    assert details["blocks"][0]["cells"][0][1]["align"] == "right"


def test_default_rich_action_buttons_are_below_the_message(monkeypatch):
    monkeypatch.setattr(R.config, "RICH_TEXT_ENABLED", True)
    R.reset_support_latch()
    client = SimpleNamespace(
        _post=AsyncMock(
            return_value={
                "message_id": 1,
                "date": 1,
                "chat": {"id": 42, "type": "private"},
            }
        )
    )
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Attack", callback_data="cipl_batapp_12_1")]]
    )
    asyncio.run(
        R.send_rich_message(
            client,
            42,
            [R.heading("Choose approach")],
            "<b>Choose approach</b>",
            reply_markup=markup,
        )
    )
    payload = client._post.call_args.args[1]
    assert payload["reply_markup"] == markup
    assert not any(
        node.get("type") == "buttons"
        for node in walk(payload["rich_message"]["blocks"])
    )
    R.reset_support_latch()


def test_sbo_uses_real_columns_and_the_same_numbering_for_bench():
    from handlers.batting_order import order_blocks

    pairs = [
        (
            SimpleNamespace(id=i),
            SimpleNamespace(
                name="Long player name" if i % 2 else "Root",
                bat_rating=90 + i,
                category="Batsman",
                bowl_style="Fast",
            ),
        )
        for i in range(1, 14)
    ]
    blocks = order_blocks(pairs, SimpleNamespace(captain_roster_id=2), True)
    tables = [node for node in walk(blocks) if node.get("type") == "table"]
    xi = tables[0]["cells"][1:]
    bench = tables[1]["cells"][1:]
    assert [row[0]["text"] for row in xi] == [str(i) for i in range(1, 12)]
    assert [row[0]["text"] for row in bench] == ["12", "13"]
    assert all(row[2]["align"] == "right" for row in xi + bench)
    assert "👑" in xi[1][1]["text"]


def test_milestone_card_keeps_admin_wording_and_uses_only_known_stats():
    from services.event_media_service import milestone_blocks

    blocks = milestone_blocks(
        "fifty",
        {"player": "Joe <Root>", "runs": 52, "balls": 31, "strike_rate": "167.74"},
        "<b>Special announcement</b> for Joe &lt;Root&gt;",
    )
    assert blocks[0]["type"] == "heading"
    table = next(block for block in blocks if block["type"] == "table")
    assert any(
        row[0]["text"] == "Balls" and row[1]["text"] == "31" for row in table["cells"]
    )
    assert not any(row[0]["text"] == "Economy" for row in table["cells"])
    assert any(node.get("text") == "Special announcement" for node in walk(blocks))

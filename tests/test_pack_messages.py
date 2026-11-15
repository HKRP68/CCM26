"""Pack buyers see accurate contents, currency choices and purchase receipts."""

import asyncio
import html
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services import pack_messages as ui, rich_message as R


def pack(**changes):
    values = dict(id=1, slot_number=1, name="Star <Pack>", emoji="⭐", description="A & B",
                  main_count=1, main_min_rating=85, main_max_rating=90,
                  main_filter_mode="rating", main_versions_json='["Star"]',
                  bonus_count=2, bonus_min_rating=70, bonus_max_rating=80,
                  cost_coins=1000, cost_quest_points=100, cost_gems=0, daily_limit=2)
    return SimpleNamespace(**dict(values, **changes))


def user():
    return SimpleNamespace(id=3, total_coins=12000, total_gems=8, quest_points=700)


def visible(value):
    if isinstance(value, list):
        return " ".join(visible(item) for item in value)
    if isinstance(value, dict):
        return " ".join(visible(item) for item in value.values())
    return str(value)


@pytest.mark.parametrize("mode,expected", [
    ("rating", "rated 85–90"), ("version", "Star version · any rating"),
    ("both", "Star version · rated 85–90"),
])
def test_shop_and_detail_agree_on_version_filters_price_and_contents(mode, expected):
    item, owner = pack(main_filter_mode=mode), user()
    for text, blocks in ((ui.list_text([item], owner), ui.list_blocks([item], owner)),
                         (ui.detail_text(item, owner, 1), ui.detail_blocks(item, owner, 1))):
        assert expected in html.unescape(text) and expected in visible(blocks)
        for fact in ("1,000 🪙 Coins or 100 🎯 Quest Points", "2 × bonus · rated 70–80", "/openpack"):
            assert fact in html.unescape(text) and fact in visible(blocks)
        assert "Star &lt;Pack&gt;" in text and "Star <Pack>" in visible(blocks)
    assert "1/2" in ui.detail_text(item, owner, 1)
    assert "1/2" in visible(ui.detail_blocks(item, owner, 1))


@pytest.mark.parametrize("currency,cost,expected", [
    ("coins", 1000, "1,000 🪙 coins"), ("quest_points", 100, "100 Quest Points"),
    ("gems", 2, "2 💎 gems"), ("free", 0, "FREE"),
])
def test_receipt_shows_actual_currency_and_unopened_inventory(currency, cost, expected):
    result = dict(currency=currency, cost_paid=cost)
    text = ui.receipt_text(pack(), user(), result, 7)
    blocks = ui.receipt_blocks(pack(), user(), result, 7)
    for fact in (expected, "7/50", "/openpack", "12,000"):
        assert fact in text and fact in visible(blocks)
    assert "Star &lt;Pack&gt;" in text


def test_free_pack_and_sold_out_state_are_clear():
    from handlers.packs import _build_detail_keyboard
    item = pack(cost_coins=0, cost_quest_points=0)
    assert ui.cost_text(item) == "FREE"
    markup = _build_detail_keyboard(12, item, True, bought_today=2)
    assert markup.inline_keyboard[0][0].text == "⏳ Daily limit reached"
    assert not any((button.callback_data or "").startswith("pkb_")
                   for row in markup.inline_keyboard for button in row)


def test_store_detail_and_back_handlers_use_explicit_rich_cards(monkeypatch):
    from handlers import packs
    owner, item = user(), pack()
    db = MagicMock()
    monkeypatch.setattr(packs, "get_session", lambda: db)
    monkeypatch.setattr(packs, "list_packs", lambda *a, **k: [item])
    monkeypatch.setattr(packs, "get_user_pack_purchases_today", lambda *a: 1)
    monkeypatch.setattr(packs, "schedule_button_timeout", MagicMock())
    reply = AsyncMock(return_value=SimpleNamespace(chat_id=42, message_id=10))
    edit = AsyncMock(return_value=True)
    monkeypatch.setattr(R, "reply_rich", reply)
    monkeypatch.setattr(R, "edit_rich", edit)
    message = SimpleNamespace(chat_id=42, message_id=10)
    query = SimpleNamespace(data="pkv_12_1", from_user=SimpleNamespace(id=12),
                            message=message, answer=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=12), message=message, callback_query=query)
    context = SimpleNamespace(bot=SimpleNamespace(), job_queue=None)

    db.query.return_value.filter.return_value.first.return_value = owner
    asyncio.run(packs.buypack_handler(update, context))
    assert reply.call_args.args[1][0]["type"] == "heading"
    assert reply.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "pkv_12_1"

    db.query.return_value.filter.return_value.first.side_effect = [owner, item]
    asyncio.run(packs.pack_view_callback(update, context))
    assert "1/2" in visible(edit.call_args.args[1])
    assert edit.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "pkb_12_1"

    query.data = "pkbk_12"
    db.query.return_value.filter.return_value.first.side_effect = [owner]
    asyncio.run(packs.pack_back_callback(update, context))
    assert "PACK STORE" in visible(edit.call_args.args[1])


def test_purchase_receipt_falls_back_to_a_new_rich_message_without_rebuying(monkeypatch):
    from handlers import packs
    from services import pack_service, quest_service
    owner, item = user(), pack()
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [owner, item]
    monkeypatch.setattr(packs, "get_session", lambda: db)
    monkeypatch.setattr(packs, "claim_once", lambda key: True)
    monkeypatch.setattr(packs, "log_activity", MagicMock())
    monkeypatch.setattr(quest_service, "safe_track", MagicMock())
    buy = MagicMock(return_value=dict(success=True, currency="quest_points", cost_paid=100))
    monkeypatch.setattr(pack_service, "buy_pack", buy)
    monkeypatch.setattr(pack_service, "list_user_inventory", lambda *a: [item])
    edit, send = AsyncMock(return_value=False), AsyncMock()
    monkeypatch.setattr(R, "edit_rich", edit)
    monkeypatch.setattr(R, "send_rich_message", send)
    query = SimpleNamespace(data="pkb_12_1", from_user=SimpleNamespace(id=12), answer=AsyncMock(),
                            message=SimpleNamespace(chat_id=42, message_id=10))
    asyncio.run(packs.pack_buy_callback(SimpleNamespace(callback_query=query),
                                      SimpleNamespace(bot=SimpleNamespace())))
    buy.assert_called_once_with(db, owner, item)
    assert "100 Quest Points" in visible(edit.call_args.args[1])
    assert send.call_args.args[1] == 42
    assert "1/50" in visible(send.call_args.args[2])

"""Daily receipts and trade confirmations preserve the actual reward/swap."""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import MAX_ROSTER, STREAK_MILESTONE
from services import daily_messages, trade_messages, rich_message as R
from tests.test_rich_text_surfaces import flatten


def player(pid, name, rating=85):
    return SimpleNamespace(id=pid, name=name, rating=rating, version="Gold")


def captain(uid, name):
    return SimpleNamespace(id=uid, telegram_id=uid * 11, username=name, first_name=name,
                           total_coins=12000, total_gems=8, roster_count=4)


@pytest.mark.parametrize("milestone", [False, True])
def test_daily_receipt_has_breakdown_balances_placement_and_real_ready_time(milestone):
    owner = captain(1, "Captain")
    added, pending = player(1, "Added <Ace>"), player(2, "Pending B")
    deadline = datetime.now(timezone.utc) + timedelta(hours=2)
    reward = dict(coins=1200, gems=3, bonus_coins=200, bonus_gems=1,
                  streak=0 if milestone else 3, milestone=milestone,
                  milestone_player=added if milestone else None,
                  players_added=[added], players_pending=[pending])
    blocks, text = daily_messages.receipt(owner, reward, deadline)
    assert json.loads(json.dumps(blocks)) == blocks
    for fact in ("1,200", "1,000 base + 200 streak bonus", "12,000", "Pending B", "Release or Replace", "Next daily claim"):
        assert fact in text and fact in flatten(blocks)
    assert "Added &lt;Ace&gt;" in text
    when = next(node for block in blocks if block["type"] == "paragraph"
                for node in (block["text"] if isinstance(block["text"], list) else [])
                if isinstance(node, dict) and node.get("type") == "date_time")
    assert when["unix_time"] == int(deadline.timestamp())
    assert "2h" in text
    if milestone:
        assert f"{STREAK_MILESTONE}/{STREAK_MILESTONE} streak completed" in text
        assert "Next bonus in 0 days" not in text


def test_daily_callback_grants_actual_rewards_and_keeps_pending_player_controls(monkeypatch):
    from handlers import daily
    from services import command_config_service as commands, config_service, quest_service
    owner = captain(1, "Owner")
    owner.roster_count = MAX_ROSTER - 1
    stats = SimpleNamespace(last_daily=None)
    pulls = [player(1, "Added Ace"), player(2, "Pending B")]
    db = MagicMock()
    db.get.return_value = owner
    db.query.return_value.filter.return_value.first.return_value = stats
    monkeypatch.setattr(daily, "get_session", lambda: db)
    monkeypatch.setattr(daily, "claim_once", lambda key: True)
    monkeypatch.setattr(daily, "check_cooldown", lambda *a: (True, 0))
    monkeypatch.setattr(daily, "update_streak", lambda *a: (2, False))
    monkeypatch.setattr(daily, "get_random_player_by_rarity", MagicMock(side_effect=pulls))
    monkeypatch.setattr(daily, "format_player_card", lambda p: p.name)
    monkeypatch.setattr(daily, "log_activity", MagicMock())
    monkeypatch.setattr(commands, "get_user_cooldown", lambda *a: 7200)
    monkeypatch.setattr(commands, "get_coin_amount", lambda *a: 1000)
    monkeypatch.setattr(commands, "get_player_count", lambda *a: 2)
    monkeypatch.setattr(commands, "get_reward", lambda *a: None)
    monkeypatch.setattr(config_service, "get_config", lambda *a: dict(
        daily_coins=1000, daily_gems=2, daily_streak_bonus_coins=100, daily_streak_bonus_gems=1))
    monkeypatch.setattr(quest_service, "safe_track", MagicMock())
    edit = AsyncMock(return_value=True)
    monkeypatch.setattr(R, "edit_rich", edit)
    query = SimpleNamespace(data="dailyclaim_1", from_user=SimpleNamespace(id=11), answer=AsyncMock(),
                            message=SimpleNamespace(chat_id=42, message_id=10, reply_text=AsyncMock()))
    asyncio.run(daily.daily_claim_callback(SimpleNamespace(callback_query=query), SimpleNamespace()))
    assert owner.total_coins == 13100 and owner.total_gems == 11
    assert owner.roster_count == MAX_ROSTER
    assert "1,100" in flatten(edit.call_args.args[1])
    assert "2h" in edit.call_args.args[2]
    assert "Pending B" in edit.call_args.args[2]
    markup = query.message.reply_text.call_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data.startswith("release_2_1_")
    assert markup.inline_keyboard[1][0].callback_data.startswith("replace_2_1_")
    db.rollback.assert_not_called()


def offer():
    return dict(id="abc", user1_id=1, user2_id=2, user1_tg_id=11, user2_tg_id=22,
                chat_id=42, db_trade_id=77, expires_at=time.time() + 120, confirmations=[],
                status="awaiting_confirmations", trade_fee=123, offer_players=[
                    trade_messages.player_snapshot(player(1, "First <Ace>")),
                    trade_messages.player_snapshot(player(2, "Second B"))])


def test_trade_offer_preserves_cards_fees_mentions_and_first_confirmation():
    state = offer()
    state["confirmations"] = [1]
    blocks, text = trade_messages.confirmation(state, captain(1, "A&B"), captain(2, "Other"))
    assert json.loads(json.dumps(blocks)) == blocks
    for fact in ("First", "Second B", "Gold", "85 OVR", "123 coins", "1/2 captains confirmed", "Expires in", "Awaiting confirmation"):
        assert fact in text and fact in flatten(blocks)
    assert "First &lt;Ace&gt;" in text and "A&amp;B" in text
    assert "tg://user?id=11" in text
    assert "Confirmed" in flatten(blocks)


def test_trade_confirmation_retains_offer_until_both_confirm_then_sends_receipt(monkeypatch):
    from handlers import trade
    one, two = captain(1, "Alpha"), captain(2, "Beta")
    state = offer()
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = one
    monkeypatch.setattr(trade, "get_session", lambda: db)
    monkeypatch.setattr(trade, "_load_users", lambda *a: (one, two))
    monkeypatch.setattr(trade, "match_lock_message", lambda *a, **k: None)
    result = dict(success=True, initiator=one, receiver=two, init_player=player(1, "First Ace"),
                  recv_player=player(2, "Second B"), fee=123, traits_returned=2)
    complete = MagicMock(return_value=result)
    monkeypatch.setattr(trade, "complete_trade", complete)
    edit, send = AsyncMock(return_value=True), AsyncMock()
    monkeypatch.setattr(R, "edit_rich", edit)
    monkeypatch.setattr(R, "send_rich_message", send)
    context = SimpleNamespace(bot=SimpleNamespace(), bot_data={trade.TRADE_STORE_KEY: {"abc": state},
                                                              trade.USER_TRADE_KEY: {1: "abc", 2: "abc"}})
    query = SimpleNamespace(data="tcfrm_abc_1", from_user=SimpleNamespace(id=11), answer=AsyncMock(),
                            edit_message_text=AsyncMock())
    asyncio.run(trade.trade_confirm_callback(SimpleNamespace(callback_query=query), context))
    complete.assert_not_called()
    assert "First" in edit.call_args.args[2] and "Second B" in edit.call_args.args[2]
    assert "1/2 captains confirmed" in edit.call_args.args[2]
    assert edit.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "tcfrm_abc_2"
    query.data, query.from_user.id = "tcfrm_abc_2", 22
    db.query.return_value.filter.return_value.first.return_value = two
    asyncio.run(trade.trade_confirm_callback(SimpleNamespace(callback_query=query), context))
    complete.assert_called_once_with(db, 77)
    text = send.call_args.args[3]
    assert "TRADE COMPLETED" in text and "Fee paid: 123 coins" in text
    assert "2 trait(s)" in text
    assert text.index("Second B") < text.index("First Ace")
    assert not context.bot_data[trade.TRADE_STORE_KEY]
    db.rollback.assert_not_called()

"""Profile pages preserve ownership, receipt privacy and accurate result text."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Bot

from handlers import profile as P
from services import rich_message as R, tournament_payouts
from tests.test_rich_message_defaults import TOKEN, delivery  # noqa: F401
from tests.test_rich_text_surfaces import flatten


def owner(**changes):
    values = dict(id=1, telegram_id=11, username="manager", first_name="Manager",
                  team_name="A <XI> & B", captain_roster_id=None, roster_count=12,
                  total_coins=12345, total_gems=8, quest_points=350,
                  matches_played=10, matches_won=6, matches_lost=3, win_streak=0,
                  best_streak=4, active_days=5, last_match_date=datetime(2026, 11, 14))
    return SimpleNamespace(**dict(values, **changes))


def session_for(user):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = user
    db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
    db.query.return_value.join.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [(85,)]
    return db


def test_overview_has_safe_names_roster_balances_and_explained_xi_values(monkeypatch):
    from services import achievement_service
    monkeypatch.setattr(achievement_service, "get_top_unlocked", lambda *a, **k:
                        [dict(emoji="🏆", name="A <Badge>")])
    user = owner(username=None, first_name="A & B")
    text = P._format_info(session_for(user), user)
    assert "A &lt;XI&gt; &amp; B" in text and "A &lt;Badge&gt;" in text
    assert "Manager: A &amp; B" in text and "@A &amp; B" not in text
    for fact in ("Roster: 12/", "12,345", "350", "/setcaptain", "first 11 roster positions", "85,000"):
        assert fact in text


def test_broken_streak_does_not_say_the_manager_has_never_won():
    text = P._format_stats(None, owner())
    assert "60.0%" in text and "Best win streak: 4" in text
    assert "start a new streak" in text and "no wins yet" not in text
    empty = P._format_stats(None, owner(matches_played=0, matches_won=0, matches_lost=0, last_match_date=None))
    assert "Win rate: —" in empty and "first win" in empty


def test_activity_is_escaped_bounded_and_preserves_the_older_events():
    db = session_for(owner())
    events = [SimpleNamespace(created_at=datetime(2026, 11, 15, 10, i), action="buy_pack",
                              detail=f"Event {i} " + "<&>" * 100, coins_change=-20000, gems_change=2)
              for i in range(15)]
    db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = events
    text = P._format_news(db, owner())
    assert len(text) < 3800
    assert "<blockquote expandable><b>Earlier activity</b>" in text
    assert "&lt;&amp;&gt;" in text and "UTC" in text
    for i in range(15):
        assert f"Event {i} " in text
    assert "10:01 UTC" in text


def test_results_distinguish_win_loss_tie_and_unrecorded_winner_and_keep_links():
    db = session_for(owner())
    rows = []
    for index, (winner, margin, value) in enumerate(((1, "runs", 1), (2, "wickets", 6),
                                                   (None, "tie", None), (None, None, None)), 1):
        rows.append(SimpleNamespace(id=index, user1_id=1, user2_id=2, winner_id=winner,
                                    margin_type=margin, margin_value=value, overs=20,
                                    completed_at=datetime(2026, 11, 14), result_message_id=30 + index,
                                    chat_id=-100123))
    db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = rows
    db.get.return_value = owner(id=2, username="Other<Owner>")
    text, buttons = P._format_results(db, owner())
    for fact in ("#1", "won by 1 run", "#2", "lost by 6 wickets", "Tie", "No Result", "Other&lt;Owner&gt;"):
        assert fact in text
    assert buttons == []
    assert 'href="https://t.me/c/123/31"' in text
    assert len(text) < 700


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("tab,title", [("info", "YOUR PROFILE"), ("stats", "YOUR CAREER STATS"),
                                       ("news", "YOUR RECENT ACTIVITY"), ("results", "YOUR RECENT RESULTS"),
                                       ("prizes", "YOUR PRIZE RECEIPTS")])
def test_every_tab_keeps_navigation_and_uses_rich_or_html_delivery(monkeypatch, delivery, native, tab, title):
    from services import achievement_service
    monkeypatch.setattr(R.config, "RICH_TEXT_ENABLED", native)
    monkeypatch.setattr(achievement_service, "get_top_unlocked", lambda *a, **k: [])
    monkeypatch.setattr(P, "get_session", lambda: session_for(owner()))
    monkeypatch.setattr(tournament_payouts, "retry_for_recipient", MagicMock())
    monkeypatch.setattr(tournament_payouts, "recipient_receipt_page", lambda *a, **k:
                        ("💰 <b>TOURNAMENT PRIZE RECEIPTS</b>\n\nNo tournament prize receipts yet.", None))
    query = SimpleNamespace(data=f"mp_{tab}_1", from_user=SimpleNamespace(id=11), answer=AsyncMock(),
                            message=SimpleNamespace(chat_id=42, message_id=10), get_bot=lambda: Bot(TOKEN),
                            edit_message_text=AsyncMock())
    asyncio.run(P.myprofile_callback(SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(type="private")),
                                     SimpleNamespace()))
    assert not delivery.calls
    text = query.edit_message_text.call_args.args[0]
    assert title in text
    markup = query.edit_message_text.call_args.kwargs["reply_markup"]
    selected = [b for row in markup.inline_keyboard for b in row if b.text.startswith("✓ ")]
    assert len(selected) == 1 and selected[0].callback_data == f"mp_{tab}_1"
    assert len(markup.inline_keyboard[0]) == 2


@pytest.mark.parametrize("data", ["mp_bad_1", "mp_info_bad", "mp_prizes_1_-5", "mp_stats_1_10"])
def test_invalid_profile_buttons_give_a_clear_recovery_message_without_loading_data(monkeypatch, data):
    get_db = MagicMock()
    monkeypatch.setattr(P, "get_session", get_db)
    query = SimpleNamespace(data=data, answer=AsyncMock())
    asyncio.run(P.myprofile_callback(SimpleNamespace(callback_query=query), SimpleNamespace()))
    get_db.assert_not_called()
    assert "/myprofile" in query.answer.call_args.args[0]


def test_profile_buttons_reject_other_managers_before_reading_private_receipts(monkeypatch):
    monkeypatch.setattr(P, "get_session", lambda: session_for(owner(id=2)))
    receipts = MagicMock()
    monkeypatch.setattr(tournament_payouts, "recipient_receipt_page", receipts)
    query = SimpleNamespace(data="mp_prizes_1", from_user=SimpleNamespace(id=22), answer=AsyncMock())
    asyncio.run(P.myprofile_callback(SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(type="private")),
                                     SimpleNamespace()))
    receipts.assert_not_called()
    assert "another manager" in query.answer.call_args.args[0]


def test_receipt_navigation_preserves_the_owner_and_cursor(monkeypatch):
    monkeypatch.setattr(P, "get_session", lambda: session_for(owner()))
    retry = MagicMock()
    monkeypatch.setattr(tournament_payouts, "retry_for_recipient", retry)
    page = MagicMock(return_value=("💰 <b>TOURNAMENT PRIZE RECEIPTS</b>\n\n"
                                  "<b>#4 · Cup</b>\nCredited: 500 coins\n"
                                  "Balance after credit: 1,000 coins + 5 gems\n15 Nov 2026 10:00 UTC", 3))
    monkeypatch.setattr(tournament_payouts, "recipient_receipt_page", page)
    query = SimpleNamespace(data="mp_prizes_1_5", from_user=SimpleNamespace(id=11), answer=AsyncMock(),
                            edit_message_text=AsyncMock())
    asyncio.run(P.myprofile_callback(SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(type="private")),
                                     SimpleNamespace()))
    assert page.call_args.kwargs["before_id"] == 5 and page.call_args.args[1] == 11
    retry.assert_called_once()
    nav = query.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard[-1]
    assert [button.callback_data for button in nav] == ["mp_prizes_1", "mp_prizes_1_3"]
    assert "10:00 UTC" in query.edit_message_text.call_args.args[0]
    assert "Balance after credit: 1,000 coins + 5 gems" in query.edit_message_text.call_args.args[0]

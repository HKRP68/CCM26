"""Help/stats preserve line breaks, and profile results stay short and paged."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Bot, Chat, Message

from handlers import matchhelp, pitchstats, profile
from tests.test_profile_tabs_messages import owner, session_for
from tests.test_rich_message_defaults import TOKEN, delivery  # noqa: F401


@pytest.mark.parametrize("command", ["help", "pitchstats"])
def test_help_and_pitch_stats_use_regular_text_on_send_and_edit(monkeypatch, delivery, command):
    bot = Bot(TOKEN)
    message = Message(10, datetime.now(timezone.utc), Chat(42, "private"), text="Command")
    message.set_bot(bot)
    context = SimpleNamespace(args=[], job_queue=None)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=7), message=message)
    if command == "help":
        module = matchhelp
        handler, callback = module.matchhelp_handler, module.matchhelp_go_callback
        data = "mhelp_go_7_batting"
    else:
        module = pitchstats
        handler, callback = module.pitchstats_handler, module.pitchstats_callback
        data = module._cb(module.VIEW_HOME, 7, module.MODE_ALL, module.SCOPE_ALL)
        monkeypatch.setattr(module, "get_session", MagicMock)
        monkeypatch.setattr(module.pitch_stats, "overview", lambda *a: [dict(
            pitch="Flat", par_band=(170, 190), avg_first=180, matches=3, rpo=9.5, wpo=.3,
            par_verdict="in", bat_first_win_pct=33.3, bat_second_win_pct=66.7, ties=0)])
        monkeypatch.setattr(module.pitch_stats, "totals", lambda *a: (3, 97))
        monkeypatch.setattr(module.pitch_stats, "played_pitches", lambda *a: ["Flat"])
    monkeypatch.setattr(module, "schedule_button_timeout", lambda *a, **k: None)
    asyncio.run(handler(update, context))
    endpoint, payload, _ = delivery.calls[-1]
    assert endpoint == "sendMessage" and "rich_message" not in payload
    assert "\n" in payload["text"] and payload["parse_mode"] == "HTML"
    if command == "help":
        labels = [b.text for row in payload["reply_markup"].inline_keyboard for b in row]
        assert any("Batting" in label for label in labels)
        assert any("Pitches" in label for label in labels)
    else:
        assert "97 legal balls" in payload["text"]
        assert "RPO" in payload["text"] and "WPO" in payload["text"]

    async def edit(text, **kwargs):
        return await bot.edit_message_text(text, chat_id=42, message_id=77, **kwargs)
    query = SimpleNamespace(data=data, from_user=SimpleNamespace(id=7), answer=AsyncMock(), edit_message_text=edit)
    asyncio.run(callback(SimpleNamespace(callback_query=query), context))
    endpoint, payload, _ = delivery.calls[-1]
    assert endpoint == "editMessageText" and "rich_message" not in payload


def match(mid):
    return SimpleNamespace(id=mid, user1_id=1, user2_id=2, winner_id=1, margin_type="wickets",
                           margin_value=6, completed_at=datetime(2026, 11, 15), overs=20,
                           result_message_id=mid, chat_id=-100123)


def test_results_show_five_single_line_matches_and_allow_older_and_newer_pages():
    user = owner()
    db = session_for(user)
    query = db.query.return_value.filter.return_value.order_by.return_value
    query.limit.return_value.all.return_value = [match(i) for i in range(12, 6, -1)]
    query.offset.return_value.limit.return_value.all.return_value = [match(i) for i in range(7, 1, -1)]
    db.get.return_value = owner(id=2, username="Other")
    text, navigation = profile._format_results(db, user)
    assert len(text) < 900 and text.count("won by 6 wickets") == 5
    assert ">#12</a>" in text and ">#8</a>" in text and ">#7</a>" not in text
    assert navigation[0][0].callback_data == "mp_results_1_1"
    older, navigation = profile._format_results(db, user, page=1)
    query.offset.assert_called_once_with(5)
    assert older.count("won by 6 wickets") == 5 and ">#7</a>" in older
    assert [button.callback_data for button in navigation[0]] == ["mp_results_1", "mp_results_1_2"]


def test_results_pagination_callback_uses_regular_html_even_when_rich_is_enabled(monkeypatch, delivery):
    user = owner()
    db = session_for(user)
    query = db.query.return_value.filter.return_value.order_by.return_value
    query.offset.return_value.limit.return_value.all.return_value = [match(7)]
    db.get.return_value = owner(id=2, username="Other")
    monkeypatch.setattr(profile, "get_session", lambda: db)
    query_ui = SimpleNamespace(data="mp_results_1_1", from_user=SimpleNamespace(id=11),
                               answer=AsyncMock(), edit_message_text=AsyncMock())
    asyncio.run(profile.myprofile_callback(SimpleNamespace(callback_query=query_ui), SimpleNamespace()))
    assert not delivery.calls
    assert query_ui.edit_message_text.call_args.kwargs["parse_mode"] == "HTML"
    assert "Page 2" in query_ui.edit_message_text.call_args.args[0]
    query.offset.assert_called_once_with(5)

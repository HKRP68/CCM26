"""Rich starts/highlights and an ordinary end-of-over report with rich enabled."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Bot

from handlers import cipl_play as cp, letsplay
from services import highlights, league_records, match_rich, rich_message as R
from tests.test_approach_miniapp_play import _ready_to_bowl, _Script, _state
from tests.test_rich_message_defaults import TOKEN, delivery  # noqa: F401
from tests.test_rich_text_surfaces import flatten


@pytest.mark.parametrize("hundred", [False, True])
def test_start_card_keeps_competition_conditions_and_commands(hundred):
    state = _state(bot_match=False)
    state.update(stadium="Ground <A> & B", pitch_type="Flat", league_name="BBL",
                 conditions=dict(weather="Clear", temperature=25, dew="Light"))
    if hundred:
        state["ball_format"] = "The100"
    text = cp._match_start_announcement(state)
    blocks = match_rich.match_start_blocks(state, text)
    assert json.loads(json.dumps(blocks)) == blocks
    assert blocks[0]["type"] == "heading"
    facts = next(block for block in blocks if block["type"] == "table")
    assert "Ground <A> & B" in flatten(facts)
    assert ("100 balls" if hundred else "20 overs") in flatten(facts)
    assert "Batting first" in flatten(facts)
    visible = flatten(blocks)
    for fact in ("High-Voltage BBL Battle", "Clear", "25°C", "Light dew", "/pause", "/playapp"):
        assert fact in visible
    guide = next(block for block in blocks if block["type"] == "details")
    for command in ("/managebatting", "/retire", "/liveorder"):
        assert command in flatten(guide)


@pytest.mark.parametrize("announce", ["cipl", "letsplay"])
def test_start_senders_deliver_native_card_and_keep_pin_and_controls(monkeypatch, announce):
    monkeypatch.setattr(R.config, "RICH_TEXT_ENABLED", True)
    R.reset_support_latch()
    state = _state(bot_match=False)
    client = SimpleNamespace(
        _post=AsyncMock(return_value={"message_id": 88, "date": 1,
                                      "chat": {"id": 1, "type": "supergroup"}}),
        send_message=AsyncMock(), delete_message=AsyncMock(), pin_chat_message=AsyncMock())
    context = SimpleNamespace(bot=client, bot_data={})
    monkeypatch.setattr(cp, "_miniapp_row", lambda s: None)
    if announce == "cipl":
        asyncio.run(cp._cleanup_setup_and_announce(context, state, {}))
    else:
        asyncio.run(letsplay._announce(context, state, "Hard"))
    endpoint, payload = client._post.call_args.args
    assert endpoint == "sendRichMessage"
    assert payload["rich_message"]["blocks"][0]["type"] == "heading"
    assert payload.get("reply_markup") is None
    menu = next(block for block in payload["rich_message"]["blocks"] if block.get("summary") == "⚙️ Manage match")
    assert "lb_n_42" in str(menu)
    client.pin_chat_message.assert_awaited_once_with(1, 88, disable_notification=True)
    assert state["pinned_msg_id"] == 88
    client.send_message.assert_not_awaited()


def reel_state():
    return dict(chat_id=1, match_id=42, ball_format="The100", bat_team_name="Kings",
                bowl_team_name="Stars", highlight_log=[
                    dict(inn=1, over=3, kind="wicket", w=60, text="Ace <b>falls</b>"),
                    dict(inn=1, over=5, kind="six", w=12, text="A big six"),
                    dict(inn=2, over=2, kind="maiden", w=15, text="A maiden")])


def test_highlights_feature_decisive_moment_and_keep_chronological_reel():
    state = reel_state()
    blocks = highlights.reel_blocks(state, "Stars vs Kings")
    assert json.loads(json.dumps(blocks)) == blocks
    assert next(block for block in blocks if block["type"] == "pullquote")["text"] == "Ace <b>falls</b>"
    moments = [block for block in blocks if "Innings" in flatten(block)]
    assert len(moments) == 3
    assert "Innings 1 · Set 3" in flatten(moments[0])
    assert "Innings 2 · Set 2" in flatten(moments[-1])
    assert "Ace &lt;b&gt;falls&lt;/b&gt;" in highlights.render_reel(state)
    assert highlights.reel_blocks({}) is None


def test_highlight_sender_uses_native_blocks(delivery):
    state = reel_state()
    asyncio.run(cp._send_highlights(SimpleNamespace(bot=Bot(TOKEN)), state, {}))
    assert delivery.calls[0][0] == "sendRichMessage"
    blocks = delivery.calls[0][1]["rich_message"]["blocks"]
    assert blocks[0]["text"] == "🎬 MATCH HIGHLIGHTS"
    assert "Moment of the match" in flatten(blocks)


def test_completed_over_sends_old_report_through_regular_endpoint(monkeypatch, delivery):
    state = _ready_to_bowl(_state(human_bats=False))
    for name in ("_ss", "_edit_action_message", "_prompt_bowler", "_clear_final_ball"):
        monkeypatch.setattr(cp, name, AsyncMock())
    monkeypatch.setattr(cp, "_cancel_timer", lambda *a: None)
    monkeypatch.setattr(cp, "_between_overs_row", lambda *a: None)
    monkeypatch.setattr(cp, "_fire_milestones_async", lambda *a: None)
    monkeypatch.setattr(cp, "_announce_drama_async", lambda *a: None)
    monkeypatch.setattr(cp.milestones, "detect", lambda *a: [])
    monkeypatch.setattr(league_records, "announce", AsyncMock())
    with _Script(["1"] * 6):
        asyncio.run(cp._run_over(SimpleNamespace(bot=Bot(TOKEN), bot_data={}), 42, state))
    assert len(delivery.calls) == 1
    endpoint, payload, _ = delivery.calls[0]
    assert endpoint == "sendMessage" and "rich_message" not in payload
    assert "<b>End of Over 1</b>" in payload["text"]
    assert "Timeline:" in payload["text"] and "Momentum:" in payload["text"]
    assert "Bot's plan" not in payload["text"]
    assert state["over_msg_ids"] == [77]


@pytest.mark.parametrize("ball_format", ["T20", "The100"])
@pytest.mark.parametrize("mark,headline,remaining", [
    (4, "FOUR!", 5), (0, "DOT BALL", 5),
    ({"type": "wide"}, "WIDE", 6), ({"type": "noball", "runs": 0}, "NO BALL", 6),
    ("W", "WICKET!", 5),
])
def test_final_ball_uses_regular_messages_and_correct_legal_ball_counts(monkeypatch, delivery, ball_format, mark, headline, remaining):
    from tests.test_final_over_drama import ready, scripted
    state = ready(ball_format)
    scripted(monkeypatch, [mark])
    summary = cp.cipl_match.simulate_over(state, pause_after_delivery=True)
    asyncio.run(cp._reveal_final_ball(SimpleNamespace(bot=Bot(TOKEN)), state, summary))
    endpoint, payload, _ = delivery.calls[0]
    assert endpoint == "sendMessage" and "rich_message" not in payload
    text = payload["text"]
    assert headline in text
    if ball_format == "The100":
        remaining -= 1  # The Hundred finishes with five-ball sets.
    assert f"from <b>{remaining}</b> balls" in text
    assert "Target" in text and "This" in text
    assert "Bot's plan" not in text
    assert state["final_ball_msg_id"] == 77
    if ball_format == "The100":
        assert "FINAL SET" in text and "/100 balls" in text
    else:
        assert "FINAL OVER" in text
    if isinstance(mark, dict):
        assert "no legal ball used" in text
        if mark["type"] == "noball":
            assert "+1 penalty" in text and "FREE HIT" in text


@pytest.mark.parametrize("needed,marks,expected", [
    (1, [4], "Target reached"), (1, [0] * 6, "innings tied"),
    (24, [0] * 6, "win by 23 runs"),
])
def test_final_delivery_reports_win_tie_or_defence_without_a_next_ball_prompt(monkeypatch, needed, marks, expected):
    from tests.test_final_over_drama import ready, scripted
    state = ready(needed=needed)
    scripted(monkeypatch, marks)
    summary = cp.cipl_match.simulate_over(state)
    text = cp._final_ball_text(state, summary)
    assert expected in text
    assert "Need <b>" not in text and "FREE HIT next" not in text

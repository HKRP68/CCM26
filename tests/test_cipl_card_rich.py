"""The /cipl approach card as a Bot API 10.1 rich message.

``_approach_card`` (HTML) and ``_approach_card_blocks`` are two live renderings
of the same board, so these tests read both and compare what a captain acts on
— the score, the chase, the batters — and check the block tree is one Telegram
will accept. The senders are tested on their two paths: blocks when rich text
is on, the HTML twin otherwise.
"""

import asyncio
import json
import os
import random
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from services import cipl_match as cm  # noqa: E402
from services import rich_message  # noqa: E402
from tests.test_cipl_approach import _make_state  # noqa: E402
from tests.test_rich_text_surfaces import BlockTreeAssertions, flatten  # noqa: E402

try:
    import handlers.cipl_play as cp  # needs python-telegram-bot
    _HAVE_CP = True
except Exception:  # pragma: no cover - environment without Telegram deps
    _HAVE_CP = False


def _play_over(s, approach="balanced"):
    s["current_bowler"] = cm.eligible_bowlers(s)[0]
    s["bowling_approach"] = "balanced"
    s["batting_approach"] = approach
    cm.simulate_over(s)


def _state(innings=1, overs_played=0, seed=11):
    random.seed(seed)
    s = _make_state()
    s.update(bat_team_code="MI", bowl_team_code="CSK",
             bat_team_emoji="🔵", bowl_team_emoji="🟡")
    if innings == 2:
        guard = 0
        while not cm.is_innings_over(s) and guard < 50:
            _play_over(s)
            guard += 1
        cm.end_first_innings(s)
    for _ in range(overs_played):
        _play_over(s, "aggressive")
    s["current_bowler"] = cm.eligible_bowlers(s)[0]
    return s


def _of_type(blocks, kind):
    return [b for b in blocks if b.get("type") == kind]


def _commentary(blocks):
    return [b for b in _of_type(blocks, "details")
            if "Commentary" in flatten(b["summary"])]


def _bowlers(blocks):
    return [b for b in _of_type(blocks, "details")
            if "Bowlers" in flatten(b["summary"])]


@unittest.skipUnless(_HAVE_CP, "handlers.cipl_play (python-telegram-bot) not importable")
class ApproachCardBlocksTests(unittest.TestCase, BlockTreeAssertions):

    def test_first_over_is_well_formed_without_commentary(self):
        s = _state()
        blocks = cp._approach_card_blocks(s)
        self.assertGreater(self.assertWellFormed(blocks), 0)
        self.assertEqual(_commentary(blocks), [])           # no prior over
        self.assertEqual(_of_type(blocks, "pullquote"), [])  # no chase yet
        self.assertIn("Yet to bat", flatten(blocks))

    def test_score_and_batters_agree_with_html(self):
        s = _state(overs_played=1)
        html_card = cp._approach_card(s)
        text = flatten(cp._approach_card_blocks(s))
        self.assertIn(cm.format_score(s), html_card)
        self.assertIn(cm.format_score(s), text)
        for key in ("striker_idx", "non_striker_idx"):
            player = s["batting_order"][s[key]]
            st = s["bat_stats"][str(player["roster_id"])]
            self.assertIn(f"{st['runs']}({st['balls']})", html_card)
            self.assertIn(f"{player['name']} {st['runs']} {st['balls']}", text)

    def test_chase_is_a_pullquote_matching_html(self):
        s = _state(innings=2, overs_played=1)
        c = cm.chase(s)
        self.assertGreater(c["runs_required"], 0)
        html_card = cp._approach_card(s)
        self.assertIn(f"Need {c['runs_required']} off {c['balls_remaining']}",
                      html_card)
        blocks = cp._approach_card_blocks(s)
        quotes = _of_type(blocks, "pullquote")
        self.assertEqual(len(quotes), 1)
        # A pullquote takes text (and credit) only — no caption field.
        self.assertEqual(set(quotes[0]), {"type", "text"})
        self.assertIn(f"Need {c['runs_required']} off {c['balls_remaining']}",
                      flatten(quotes[0]))
        # Target and both rates sit on the paragraph right under it.
        after = blocks[blocks.index(quotes[0]) + 1]
        self.assertEqual(after["type"], "paragraph")
        self.assertIn(f"Target {c['target']}", flatten(after))
        self.assertIn(f"RRR {c['rrr']:.2f}", flatten(after))

    def test_commentary_sits_collapsed_with_every_ball(self):
        s = _state(overs_played=1)
        blocks = cp._approach_card_blocks(s)
        self.assertWellFormed(blocks)
        details = _commentary(blocks)
        self.assertEqual(len(details), 1)
        self.assertNotIn("is_open", details[0])      # buttons stay on screen
        inside = flatten(details[0])
        rows = cp._commentary_rows(s)
        self.assertTrue(rows)
        for over, line, _emoji in rows:
            self.assertIn(over, inside)
            self.assertIn(line, inside)

    def test_heading_names_innings_team_pitch_and_match_id(self):
        s = _state()
        s["pitch_type"], s["match_id"] = "Hard", 4321
        blocks = cp._approach_card_blocks(s)
        self.assertEqual(blocks[0]["type"], "heading")
        head = flatten(blocks[0])
        self.assertIn("Innings 1", head)
        self.assertIn(str(s["bat_team_name"]), head)
        sub = flatten(blocks[1])
        self.assertIn("Hard pitch", sub)
        self.assertIn("#4321", sub)
        card = cp._approach_card(s)
        first = card.split("\n", 1)[0]
        for bit in ("Innings 1", "Hard pitch", "#4321"):
            self.assertIn(bit, first)

    def test_every_bowler_sits_collapsed_with_the_current_one_first(self):
        s = _state(overs_played=2)
        blocks = cp._approach_card_blocks(s)
        self.assertWellFormed(blocks)
        bowlers = _bowlers(blocks)
        self.assertEqual(len(bowlers), 1)
        self.assertNotIn("is_open", bowlers[0])       # buttons stay on screen
        rows = cp._bowler_rows(s)
        self.assertTrue(rows[0]["current"])
        self.assertEqual(sum(r["current"] for r in rows), 1)
        inside = flatten(bowlers[0])
        for r in rows:
            self.assertIn(r["name"], inside)
        # Anyone who has bowled is on the list with their figures.
        bowled = [r for r in rows if r["balls"]]
        self.assertTrue(bowled)
        html_card = cp._approach_card(s)
        self.assertIn("BOWLERS", html_card)
        tail = html_card.split("BOWLERS", 1)[1]
        self.assertIn("<blockquote expandable>", tail)
        for r in rows:
            self.assertIn(r["name"], tail)

    def test_hundred_bowler_workload_reads_in_balls(self):
        s = _state()
        with patch.object(cm, "balls_per_unit", lambda st: 5):
            s["bowl_stats"][str(s["current_bowler"]["roster_id"])] = {
                "balls": 5, "runs": 7, "wickets": 1}
            rows = cp._bowler_rows(s)
        self.assertEqual(rows[0]["overs"], "5b")

    def test_prompt_lines_follow_a_divider(self):
        s = _state()
        s["user_names"] = {"5": "Khalid Hasan"}
        blocks = cp._approach_card_blocks(s, [
            [cp._rich_mention(s, 5), ", choose your ",
             rich_message.bold("Batting Approach"), ":"]])
        self.assertEqual(blocks[-2]["type"], "divider")
        last = blocks[-1]
        self.assertEqual(last["type"], "paragraph")
        self.assertIn("Khalid Hasan, choose your Batting Approach:", flatten(last))
        self.assertIn("tg://user?id=5", json.dumps(last))

    def test_bot_is_not_a_mention(self):
        self.assertEqual(flatten(cp._rich_mention({}, cp.BOT_TG_ID_)), "🤖 Bot")

    def test_chem_and_ovr_share_one_table(self):
        s = _state(innings=2, overs_played=1)
        chem = ("🟧 61/100", "🟥 51/100")
        boost = ({"base": 85, "effective": 85.3, "bonus": 0.3},
                 {"base": 83, "effective": 83.0, "bonus": 0.0})
        with patch.object(cp, "_chem_badges", lambda st: chem), \
                patch.object(cp, "_trait_boost", lambda st: boost):
            blocks = cp._approach_card_blocks(s)
        self.assertWellFormed(blocks)
        text = flatten(blocks)
        self.assertIn("🧪 CHEM", text)
        # Innings 2: the codes swapped at the break, so CSK is batting (row 1).
        self.assertIn("CSK 🟧 61 85 → 85.3", text)
        self.assertIn("MI 🟥 51 83 → 83.0", text)

    def test_no_matchup_table_outside_lets_play(self):
        text = flatten(cp._approach_card_blocks(_state()))
        self.assertNotIn("CHEM", text)
        self.assertNotIn("OVR", text)

    def test_broken_state_returns_none(self):
        self.assertIsNone(cp._approach_card_blocks({"bat_team_name": "X"}))


class _FakeBot:
    def __init__(self):
        self.posts = []
        self.send_message = AsyncMock(
            return_value=type("M", (), {"message_id": 77})())
        self.edit_message_text = AsyncMock()
        self.delete_message = AsyncMock()

    async def _post(self, endpoint, payload):
        self.posts.append((endpoint, payload))
        return {"message_id": 88, "date": 0,
                "chat": {"id": 42, "type": "group"}}


class _Ctx:
    def __init__(self):
        self.bot = _FakeBot()
        self.bot_data = {}
        self.job_queue = None


@unittest.skipUnless(_HAVE_CP, "handlers.cipl_play (python-telegram-bot) not importable")
class ActionMessageRichTests(unittest.TestCase):

    def setUp(self):
        rich_message.reset_support_latch()
        self.addCleanup(rich_message.reset_support_latch)
        self.blocks = [rich_message.paragraph("board")]

    def _state(self, action_msg_id=None):
        return {"chat_id": 42, "match_id": 7, "action_msg_id": action_msg_id,
                "over_msg_ids": [action_msg_id] if action_msg_id else []}

    def test_new_message_goes_out_rich(self):
        ctx, state = _Ctx(), self._state()
        with patch.object(config, "RICH_TEXT_ENABLED", True):
            asyncio.run(cp._new_action_message(ctx, state, "<b>html</b>", [],
                                               blocks=self.blocks))
        self.assertEqual(ctx.bot.posts[0][0], "sendRichMessage")
        ctx.bot.send_message.assert_not_called()
        self.assertEqual(state["action_msg_id"], 88)
        self.assertIn(88, state["over_msg_ids"])

    def test_new_message_is_html_when_rich_is_off(self):
        ctx, state = _Ctx(), self._state()
        with patch.object(config, "RICH_TEXT_ENABLED", False):
            asyncio.run(cp._new_action_message(ctx, state, "<b>html</b>", [],
                                               blocks=self.blocks))
        self.assertEqual(ctx.bot.posts, [])
        ctx.bot.send_message.assert_awaited_once()
        self.assertEqual(state["action_msg_id"], 77)

    def test_edit_in_place_goes_out_rich(self):
        ctx, state = _Ctx(), self._state(action_msg_id=500)
        with patch.object(config, "RICH_TEXT_ENABLED", True):
            asyncio.run(cp._edit_action_message(ctx, state, "<b>html</b>", [],
                                                blocks=self.blocks))
        endpoint, payload = ctx.bot.posts[0]
        self.assertEqual(endpoint, "editMessageText")
        self.assertEqual(payload["message_id"], 500)
        ctx.bot.edit_message_text.assert_not_called()
        self.assertEqual(state["action_msg_id"], 500)

    def test_edit_without_blocks_stays_html(self):
        ctx, state = _Ctx(), self._state(action_msg_id=500)
        with patch.object(config, "RICH_TEXT_ENABLED", True):
            asyncio.run(cp._edit_action_message(ctx, state, "<b>html</b>", []))
        self.assertEqual(ctx.bot.posts, [])
        ctx.bot.edit_message_text.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

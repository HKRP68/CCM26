"""The Match Summary card must actually reach the chat when a match ends.

The bug this pins: ``_miniapp_row`` returned the python-telegram-bot
``InlineKeyboardMarkup.inline_keyboard`` TUPLE, and the end-of-match code did
``miniapp_row + _rematch_row(state)`` to bolt the Rematch button on. ``tuple +
list`` raises TypeError, so every finished /lpbot and /ciplbot match died right
there — no result message, no scorecard image, and the live state was never
cleaned up. A human-vs-human match skipped that line and was unaffected, which
is exactly why it read as "the bot matches don't generate a scorecard".
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import handlers.cipl_play as cp


def _state(bot=True):
    state = {
        "match_id": 42, "chat_id": 555, "is_private": False,
        "overs": 20, "stadium": "Eden Gardens",
        "bat_team_name": "Bot XI", "bowl_team_name": "You",
        "is_letsplay": True,
    }
    if bot:
        state["is_bot_match"] = True
        state["bot_user_id"] = 99
        state["bot_persona"] = "tactician"
    return state


class MiniappRowTests(unittest.TestCase):
    def setUp(self):
        self._real = cp._miniapp_row

    def tearDown(self):
        cp._miniapp_row = self._real

    def test_the_row_is_a_list_so_buttons_can_be_appended(self):
        """Whatever the keyboard builder hands back, the Rematch button has to
        be appendable to it."""
        import services.match_broadcast as mb
        real = mb.play_match_keyboard
        mb.play_match_keyboard = lambda *a, **k: InlineKeyboardMarkup(
            [[InlineKeyboardButton("📊 View Match", url="https://example.test")]])
        try:
            rows = cp._miniapp_row(_state())
        finally:
            mb.play_match_keyboard = real

        self.assertIsInstance(rows, list)
        combined = rows + cp._rematch_row(_state())
        self.assertEqual(len(combined), 2)
        # And it still builds a real keyboard.
        self.assertEqual(len(InlineKeyboardMarkup(combined).inline_keyboard), 2)

    def test_a_broken_keyboard_builder_returns_none_not_an_exception(self):
        import services.match_broadcast as mb
        real = mb.play_match_keyboard

        def boom(*a, **k):
            raise RuntimeError("no bot username configured")

        mb.play_match_keyboard = boom
        try:
            self.assertIsNone(cp._miniapp_row(_state()))
        finally:
            mb.play_match_keyboard = real

    def test_the_rematch_button_carries_the_mode(self):
        lp = cp._rematch_row(_state())
        self.assertEqual(lp[0][0].callback_data, "botmatch_again_lp")
        cipl = cp._rematch_row({"league_key": "bbl"})
        self.assertEqual(cipl[0][0].callback_data, "botmatch_again_cipl_bbl")


class SummaryCaptionTests(unittest.TestCase):
    def test_a_practice_card_names_the_venue_and_the_bot_style(self):
        caption = cp._summary_caption(_state(), "🏆 Bot XI beat You by 6 wickets!")
        self.assertIn("Match Summary", caption)
        self.assertIn("Eden Gardens", caption)
        self.assertIn("Bot captain", caption)
        self.assertIn("unranked", caption)

    def test_a_human_match_card_has_no_bot_line(self):
        caption = cp._summary_caption(_state(bot=False), "🤝 Match Tied!")
        self.assertNotIn("Bot captain", caption)

    def test_a_broken_state_still_yields_a_caption(self):
        caption = cp._summary_caption({"is_bot_match": True}, "result")
        self.assertIn("result", caption)


class EndOfMatchDeliveryTests(unittest.TestCase):
    """Drive the real end-of-match tail with fakes and assert the photo goes."""

    def setUp(self):
        self._saved = {name: getattr(cp, name) for name in
                       ("_gs", "_ss", "_miniapp_row", "_delete_prev_over",
                        "_build_cipl_summary_image", "cleanup_state",
                        "release_match_lock", "_team_user_mentions",
                        "_innings_scorecard")}
        self.state = _state()
        cp._miniapp_row = lambda s: [[InlineKeyboardButton(
            "📊 View Match", url="https://example.test")]]
        cp._build_cipl_summary_image = lambda s, r: b"PNGDATA"
        cp.cleanup_state = lambda ctx, mid: None
        cp.release_match_lock = lambda mid: None
        cp._team_user_mentions = lambda s: {}
        cp._innings_scorecard = lambda s, innings_label="": "scorecard"

        async def _noop(*args, **kwargs):
            return None

        cp._delete_prev_over = _noop
        cp._ss = _noop

        async def _gs(ctx, mid):
            return self.state

        cp._gs = _gs

        self.ctx = MagicMock()
        self.ctx.bot.send_message = AsyncMock()
        self.ctx.bot.send_photo = AsyncMock()
        self.ctx.bot.unpin_chat_message = AsyncMock()

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(cp, name, value)

    def _run(self):
        result = {"tie": False, "winner": "Bot XI", "loser": "You",
                  "margin": 6, "margin_type": "wickets"}
        real_compute = cp.cipl_match.compute_result
        real_finalize = cp.asyncio.to_thread
        cp.cipl_match.compute_result = lambda s: result

        async def _to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        cp.asyncio.to_thread = _to_thread
        try:
            asyncio.run(cp._complete_match(self.ctx, 42, self.state))
        finally:
            cp.cipl_match.compute_result = real_compute
            cp.asyncio.to_thread = real_finalize

    def test_a_bot_match_posts_both_the_result_and_the_summary_image(self):
        self._run()
        self.ctx.bot.send_message.assert_called_once()
        self.ctx.bot.send_photo.assert_called_once()
        # The image goes out with the View Match AND the Rematch button.
        markup = self.ctx.bot.send_photo.call_args.kwargs["reply_markup"]
        self.assertEqual(len(markup.inline_keyboard), 2)

    def test_the_image_still_goes_when_the_result_message_fails(self):
        self.ctx.bot.send_message = AsyncMock(side_effect=RuntimeError("flood wait"))
        self._run()
        self.ctx.bot.send_photo.assert_called_once()

    def test_a_keyboard_failure_does_not_cost_the_image(self):
        def boom(state):
            raise TypeError("can only concatenate tuple (not \"list\") to tuple")

        cp._miniapp_row = boom
        self._run()
        self.ctx.bot.send_photo.assert_called_once()


if __name__ == "__main__":
    unittest.main()

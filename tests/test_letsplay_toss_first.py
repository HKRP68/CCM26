"""Lets Play: the toss comes BEFORE the Playing XIs.

pitch → toss (call + bat/bowl) → XI card (/change) → Start Match → launch. The
captains see who bats first on the XI card, and the launch uses the decision
recorded at the toss.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from handlers import letsplay

HOST_TG, GUEST_TG = 111, 222


class _Ctx:
    def __init__(self):
        self.bot_data = {}
        self.job_queue = None
        self.bot = MagicMock()
        self.bot.send_message = AsyncMock()


def _draft(status, **extra):
    d = {"invite_id": 9, "chat_id": -100, "status": status,
         "host": {"tg_id": HOST_TG, "name": "Host", "user_id": 1},
         "guest": {"tg_id": GUEST_TG, "name": "Guest", "user_id": 2}}
    d.update(extra)
    return d


def _query(data, user_id):
    q = MagicMock()
    q.data = data
    q.from_user.id = user_id
    q.answer = AsyncMock()
    q.edit_message_reply_markup = AsyncMock()
    return q


def _run(coro):
    return asyncio.run(coro)


class TossFirstTests(unittest.TestCase):
    def setUp(self):
        self.ctx = _Ctx()

    def _put(self, draft):
        self.ctx.bot_data[letsplay._dkey(draft["invite_id"])] = draft
        return draft

    def test_picking_the_pitch_opens_the_toss_not_the_xis(self):
        draft = self._put(_draft("pitch"))
        q = _query("lp_pitch_9_Hard", HOST_TG)
        with patch.object(letsplay.R, "edit_rich", new=AsyncMock()), \
             patch.object(letsplay, "_start_toss", new=AsyncMock()) as toss, \
             patch.object(letsplay, "_prompt_show_xi", new=AsyncMock()) as xi:
            _run(letsplay.letsplay_pitch_callback(
                MagicMock(callback_query=q), self.ctx))
        self.assertEqual(draft["status"], "toss")
        toss.assert_awaited_once()
        xi.assert_not_awaited()

    def test_electing_shows_the_xis_instead_of_launching(self):
        draft = self._put(_draft("toss", toss_winner_side="host"))
        q = _query("lp_toss_bowl_9_host", HOST_TG)
        with patch.object(letsplay, "_reveal_rich", new=AsyncMock()), \
             patch.object(letsplay, "_prompt_show_xi", new=AsyncMock()) as xi, \
             patch.object(letsplay, "_launch_match", new=AsyncMock()) as launch:
            _run(letsplay.letsplay_toss_callback(
                MagicMock(callback_query=q), self.ctx))
        self.assertEqual(draft["status"], "showxi")
        self.assertEqual(draft["toss_decision"], "bowl")
        xi.assert_awaited_once()
        launch.assert_not_awaited()

    def test_a_bot_toss_win_also_goes_to_the_xis(self):
        draft = self._put(_draft("toss", vs_bot=True))
        q = _query("lp_coin_heads_9", HOST_TG)

        async def _reveal(edit):
            await edit()
            return True

        with patch("services.match_broadcast.run_coin_toss",
                   new=AsyncMock(return_value=("tails", False))), \
             patch("services.match_broadcast.reveal_toss_result", new=_reveal), \
             patch("services.bot_captain.elect_toss_decision", return_value="bat"), \
             patch.object(letsplay, "_reveal_rich", new=AsyncMock()), \
             patch.object(letsplay, "_prompt_show_xi", new=AsyncMock()) as xi, \
             patch.object(letsplay, "_launch_match", new=AsyncMock()) as launch:
            _run(letsplay.letsplay_coin_callback(
                MagicMock(callback_query=q), self.ctx))
        self.assertEqual(draft["toss_winner_side"], "guest")
        self.assertEqual(draft["toss_decision"], "bat")
        self.assertEqual(draft["status"], "showxi")
        xi.assert_awaited_once()
        launch.assert_not_awaited()

    def test_start_match_launches_with_the_stored_toss(self):
        draft = self._put(_draft("showxi", toss_winner_side="guest",
                                 toss_decision="bat"))
        q = _query("lp_startmatch_9", HOST_TG)
        with patch.object(letsplay, "_launch_match", new=AsyncMock()) as launch:
            _run(letsplay.letsplay_startmatch_callback(
                MagicMock(callback_query=q), self.ctx))
        launch.assert_awaited_once_with(self.ctx, draft, "bat", "guest")
        self.assertTrue(draft["match_launched"])

    def test_only_the_host_starts_the_match(self):
        draft = self._put(_draft("showxi", toss_winner_side="guest",
                                 toss_decision="bat"))
        q = _query("lp_startmatch_9", GUEST_TG)
        with patch.object(letsplay, "_launch_match", new=AsyncMock()) as launch:
            _run(letsplay.letsplay_startmatch_callback(
                MagicMock(callback_query=q), self.ctx))
        launch.assert_not_awaited()
        self.assertFalse(draft.get("match_launched"))

    def test_the_xi_card_says_who_bats_first(self):
        draft = _draft("showxi", toss_winner_side="host", toss_decision="bowl")
        self.assertEqual(letsplay._toss_summary(draft),
                         "🪙 Host won the toss and elected to BOWL — "
                         "Guest bats first.")
        kb = letsplay._start_match_kb(9)
        self.assertEqual(kb.inline_keyboard[0][0].callback_data,
                         "lp_startmatch_9")


if __name__ == "__main__":
    unittest.main()

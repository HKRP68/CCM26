"""The reported bug: tapping Bat/Bowl answers "Match already started" — but no
match ever started.

``match_launched`` is set the moment the Match row commits, which happens
BEFORE the toss-result edit, the state write and the first bowler prompt. Any
failure in that tail left a committed row with no board: the chat was locked by
the active-match checks, and every retry of Bat/Bowl bounced off the
"already started" guard on a match nobody could play.

These tests pin the two halves of the fix:

  • the guard now proves the match is playable before it claims it started, and
    clears the wreckage when it isn't, so the retry actually launches;
  • the hand-off after the commit can fail without taking the match with it —
    it resumes the board when the state survived, and rolls the launch back
    when it didn't.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

import handlers.cipl_play as cp
from services.match_state_store import A_COMPLETED, A_PICK_CIPL_BOWLER


def _query(data, user_id=7):
    q = MagicMock()
    q.data = data
    q.from_user.id = user_id
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    q.message.message_id = 1234
    return q


class _Patched:
    """Swap module attributes for the duration of a test, then put them back."""

    def __init__(self, test, **attrs):
        self._saved = {name: getattr(cp, name) for name in attrs}
        for name, value in attrs.items():
            setattr(cp, name, value)
        test.addCleanup(self._restore)

    def _restore(self):
        for name, value in self._saved.items():
            setattr(cp, name, value)


class LaunchGuardTests(unittest.TestCase):
    """cipl_toss_callback: "already started" must be true before it is said."""

    DRAFT_ID = 31
    WINNER_TG = 7

    def setUp(self):
        self.ctx = MagicMock()
        self.ctx.bot_data = {}
        self.ctx.bot.send_message = AsyncMock()
        self.draft = {
            "turn": "complete", "chat_id": -100, "match_launched": True,
            "match_id": 55, "host_tg_id": 1, "target_tg_id": self.WINNER_TG,
        }
        self.ctx.bot_data[cp._draft_key(self.DRAFT_ID)] = self.draft
        self.launched = []
        self.resumed = []
        self.cancelled = []

        async def _launch(context, q, draft, draft_id, decision, winner_side):
            self.launched.append((draft_id, decision, winner_side))

        async def _resume(context, mid, state=None):
            self.resumed.append(mid)
            return True

        _Patched(self, _launch_after_toss=_launch, cipl_resume=_resume,
                 cleanup_state=lambda ctx, mid: None,
                 release_match_lock=lambda mid: None,
                 _cancel_orphan_match_row=self.cancelled.append,
                 _release_launch_reservations=lambda draft: None)

    def _tap(self):
        q = _query(f"cipl_toss_bat_{self.DRAFT_ID}_target", self.WINNER_TG)
        asyncio.run(cp.cipl_toss_callback(MagicMock(callback_query=q), self.ctx))
        return q

    def _install_state(self, state, action=A_PICK_CIPL_BOWLER):
        async def _gs(ctx, mid):
            return state

        async def _next(ctx, mid):
            return action

        _Patched(self, _gs=_gs, _get_next_action=_next)

    def test_a_dead_launch_retries_instead_of_claiming_it_started(self):
        # No saved state: the Match row committed, the board never appeared.
        self._install_state(None)

        q = self._tap()

        self.assertEqual(self.launched, [(self.DRAFT_ID, "bat", "target")])
        answered = " ".join(str(c.args[0]) for c in q.answer.call_args_list
                            if c.args)
        self.assertNotIn("already started", answered)
        # The orphan row is cancelled so the retry isn't rejected by the
        # per-chat / per-player active-match gates.
        self.assertEqual(self.cancelled, [55])
        self.assertFalse(self.draft["match_launched"])

    def test_a_finished_match_is_also_treated_as_dead(self):
        self._install_state({"mode": "cipl_approach"}, action=A_COMPLETED)

        self._tap()

        self.assertEqual(self.launched, [(self.DRAFT_ID, "bat", "target")])

    def test_a_genuine_double_tap_never_relaunches(self):
        self._install_state({"mode": "cipl_approach", "action_msg_id": 900})

        q = self._tap()

        self.assertEqual(self.launched, [])
        self.assertEqual(self.resumed, [])       # the board is already on screen
        self.assertEqual(self.cancelled, [])
        answered = " ".join(str(c.args[0]) for c in q.answer.call_args_list
                            if c.args)
        self.assertIn("already started", answered)

    def test_only_the_toss_winner_rolls_a_dead_launch_back(self):
        # Rolling back writes to the database, so a bystander tapping the stale
        # button must not trigger it — they just get told it's starting.
        self._install_state(None)
        q = _query(f"cipl_toss_bat_{self.DRAFT_ID}_target", 4242)
        asyncio.run(cp.cipl_toss_callback(MagicMock(callback_query=q), self.ctx))

        self.assertEqual(self.cancelled, [])
        self.assertEqual(self.launched, [])
        self.assertTrue(self.draft["match_launched"])

    def test_a_live_match_with_no_visible_board_is_put_back_on_screen(self):
        # State survived but the prompt didn't — the player is staring at a dead
        # toss message, which is exactly why they tapped again.
        self._install_state({"mode": "cipl_approach", "action_msg_id": None})

        self._tap()

        self.assertEqual(self.resumed, [55])
        self.assertEqual(self.launched, [])
        self.assertTrue(self.draft["match_launched"])


class HandoffRecoveryTests(unittest.TestCase):
    """_recover_failed_handoff: resume what survived, roll back what didn't."""

    def setUp(self):
        self.ctx = MagicMock()
        self.ctx.bot_data = {}
        self.ctx.bot.send_message = AsyncMock()
        self.draft = {"chat_id": -100, "match_id": 77, "match_launched": True}
        self.cancelled = []
        self.released = []

    def _patch(self, state, resume_ok=True, action=A_PICK_CIPL_BOWLER):
        async def _gs(ctx, mid):
            return state

        async def _next(ctx, mid):
            return action

        async def _resume(context, mid, state=None):
            self.resumed = mid
            return resume_ok

        self.resumed = None
        _Patched(self, _gs=_gs, _get_next_action=_next, cipl_resume=_resume,
                 cleanup_state=lambda ctx, mid: None,
                 release_match_lock=lambda mid: None,
                 _cancel_orphan_match_row=self.cancelled.append,
                 _release_launch_reservations=self.released.append)

    def _recover(self):
        asyncio.run(cp._recover_failed_handoff(
            self.ctx, self.draft, 31, "target", 7, "Winner"))

    def test_a_playable_match_is_resumed_not_thrown_away(self):
        self._patch({"mode": "cipl_approach"})

        self._recover()

        self.assertEqual(self.resumed, 77)
        self.assertEqual(self.cancelled, [])
        self.assertTrue(self.draft["match_launched"])
        self.ctx.bot.send_message.assert_not_called()

    def test_an_unplayable_match_is_rolled_back_and_the_toss_re_offered(self):
        self._patch(None)

        self._recover()

        self.assertEqual(self.cancelled, [77])
        self.assertEqual(self.released, [self.draft])
        self.assertFalse(self.draft["match_launched"])
        self.assertIsNone(self.draft["match_id"])

        self.ctx.bot.send_message.assert_called_once()
        kwargs = self.ctx.bot.send_message.call_args.kwargs
        buttons = [b.callback_data
                   for row in kwargs["reply_markup"].inline_keyboard
                   for b in row]
        self.assertEqual(buttons,
                         ["cipl_toss_bat_31_target", "cipl_toss_bowl_31_target"])

    def test_a_resume_that_also_fails_still_rolls_the_launch_back(self):
        self._patch({"mode": "cipl_approach"}, resume_ok=False)

        self._recover()

        self.assertEqual(self.cancelled, [77])
        self.assertFalse(self.draft["match_launched"])


if __name__ == "__main__":
    unittest.main()

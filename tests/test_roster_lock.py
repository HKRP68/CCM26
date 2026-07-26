"""Squad edits are frozen while a match is on, and only one release at a time.

Two independent guards, both about a user changing things underneath something
that is already running:

  • ``services.roster_lock`` — no XI swap, batting-order rewrite, captaincy
    change, trait change, or buying/selling/trading of players while that user
    has a match in progress.
  • ``handlers.release`` — one release/multi-release confirmation prompt at a
    time, because confirming one renumbers the roster the other is describing.
"""

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock

import handlers.release as rl
from services import roster_lock


class FakeMatch:
    def __init__(self, status="active"):
        self.status = status
        self.id = 1


class RosterLockTests(unittest.TestCase):
    def setUp(self):
        self._real = roster_lock.active_match_for

    def tearDown(self):
        roster_lock.active_match_for = self._real

    def _with_match(self, match):
        roster_lock.active_match_for = lambda session, user_id: match

    def test_no_match_means_no_lock(self):
        self._with_match(None)
        self.assertIsNone(roster_lock.match_lock_message(None, 5, "swap"))
        self.assertIsNone(roster_lock.match_lock_alert(None, 5, "swap"))

    def test_a_live_match_locks_the_action(self):
        self._with_match(FakeMatch("active"))
        msg = roster_lock.match_lock_message(None, 5, "change your Playing XI")
        self.assertIn("middle of a match", msg)
        self.assertIn("change your Playing XI", msg)

    def test_a_match_being_set_up_locks_it_too(self):
        """The pre-toss states are exactly when a late XI edit would do damage."""
        self._with_match(FakeMatch("toss"))
        msg = roster_lock.match_lock_message(None, 5, "swap players in your XI")
        self.assertIn("match starting", msg)

    def test_the_alert_variant_is_plain_single_line_text(self):
        self._with_match(FakeMatch("active"))
        alert = roster_lock.match_lock_alert(None, 5, "apply a trait")
        self.assertNotIn("<", alert)
        self.assertEqual(alert.count("\n"), 0)

    def test_the_market_wording_talks_about_the_squad_not_the_xi(self):
        self._with_match(FakeMatch("active"))
        msg = roster_lock.match_lock_message(None, 5, "buy players",
                                            reason=roster_lock.MARKET_REASON)
        self.assertIn("buy players", msg)
        self.assertIn("squad you started the match with", msg)
        self.assertNotIn("took the field", msg)

    def test_a_lookup_failure_never_locks_the_user_out(self):
        def boom(session, user_id):
            raise RuntimeError("db down")

        roster_lock.active_match_for = self._real   # exercise the real wrapper
        import handlers.match as hm
        real_lookup = hm._active_match_for_user
        hm._active_match_for_user = boom
        try:
            self.assertIsNone(roster_lock.match_lock_message(None, 5, "swap"))
        finally:
            hm._active_match_for_user = real_lookup


class TradeConfirmCancelsOnALateMatchTests(unittest.TestCase):
    """A trade waits up to 60s for the second tap — long enough for either
    captain to start a match, which must cancel it rather than swap the cards."""

    def setUp(self):
        import handlers.trade as ht
        self.ht = ht
        self._saved = {name: getattr(ht, name) for name in
                       ("get_session", "_load_users", "match_lock_message",
                        "complete_trade")}
        self.u1 = MagicMock()
        self.u1.id, self.u1.telegram_id, self.u1.username = 1, 11, "alpha"
        self.u2 = MagicMock()
        self.u2.id, self.u2.telegram_id, self.u2.username = 2, 22, "beta"

        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = self.u1
        ht.get_session = lambda: session
        ht._load_users = lambda s, state: (self.u1, self.u2)
        self.completed = []
        ht.complete_trade = lambda s, tid: self.completed.append(tid) or {
            "success": True, "initiator": self.u1, "receiver": self.u2,
            "init_player": MagicMock(), "recv_player": MagicMock(), "fee": 0}

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(self.ht, name, value)

    def _run(self, locked_user_id=None):
        self.ht.match_lock_message = (
            lambda session, user_id, action, **kw:
            "🔒 locked" if user_id == locked_user_id else None)

        query = MagicMock()
        query.data = "tcfrm_abc_1"
        query.from_user.id = 11
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update = MagicMock()
        update.callback_query = query

        context = MagicMock()
        context.bot_data = {self.ht.TRADE_STORE_KEY: {"abc": {
            "id": "abc", "user1_id": 1, "user2_id": 2,
            "user1_tg_id": 11, "user2_tg_id": 22, "chat_id": 9,
            "db_trade_id": 77, "expires_at": time.time() + 60,
            # Both have already confirmed, so this tap completes the trade.
            "confirmations": [1, 2], "status": "awaiting_confirmations",
        }}, self.ht.USER_TRADE_KEY: {1: "abc", 2: "abc"}}
        context.bot.send_message = AsyncMock()
        asyncio.run(self.ht.trade_confirm_callback(update, context))
        return query

    def test_an_unlocked_trade_still_completes(self):
        query = self._run(locked_user_id=None)
        self.assertEqual(self.completed, [77])

    def test_the_initiator_starting_a_match_cancels_the_trade(self):
        query = self._run(locked_user_id=1)
        self.assertEqual(self.completed, [])
        self.assertIn("in a match now", query.edit_message_text.call_args.args[0])

    def test_the_other_captain_starting_a_match_cancels_it_too(self):
        query = self._run(locked_user_id=2)
        self.assertEqual(self.completed, [])
        self.assertIn("beta", query.edit_message_text.call_args.args[0])


class _Msg:
    """Just enough of a telegram Message for the release handlers."""

    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        sent = MagicMock()
        sent.chat_id = 77
        sent.message_id = len(self.replies)
        return sent


class ReleaseOneAtATimeTests(unittest.TestCase):
    TG_ID = 4242

    def setUp(self):
        rl._open_prompts.clear()

    def tearDown(self):
        rl._open_prompts.clear()

    def test_no_prompt_open_by_default(self):
        self.assertIsNone(rl.pending_release(self.TG_ID))

    def test_an_open_prompt_blocks_the_next_release(self):
        rl._open_prompt(self.TG_ID, "release", 77)
        busy = rl.pending_release(self.TG_ID)
        self.assertIsNotNone(busy)
        self.assertIn("already have a release waiting", rl._busy_text(busy))

    def test_a_multi_release_prompt_is_named_as_one(self):
        rl._open_prompt(self.TG_ID, "multi", 77)
        self.assertIn("multi-release", rl._busy_text(rl.pending_release(self.TG_ID)))

    def test_answering_the_prompt_frees_the_next_one(self):
        rl._open_prompt(self.TG_ID, "release", 77)
        rl._close_prompt(self.TG_ID)
        self.assertIsNone(rl.pending_release(self.TG_ID))

    def test_an_abandoned_prompt_expires_with_its_buttons(self):
        rl._open_prompt(self.TG_ID, "release", 77)
        rl._open_prompts[self.TG_ID]["expires_at"] = time.monotonic() - 1
        self.assertIsNone(rl.pending_release(self.TG_ID))
        self.assertNotIn(self.TG_ID, rl._open_prompts)   # dropped on read

    def test_one_users_prompt_never_blocks_another(self):
        rl._open_prompt(self.TG_ID, "release", 77)
        self.assertIsNone(rl.pending_release(self.TG_ID + 1))

    def _update(self, msg):
        update = MagicMock()
        update.message = msg
        update.effective_user = MagicMock()
        update.effective_user.id = self.TG_ID
        return update

    def test_the_release_command_refuses_while_one_is_open(self):
        rl._open_prompt(self.TG_ID, "release", 77)
        msg = _Msg()
        context = MagicMock()
        context.args = ["Kohli"]
        asyncio.run(rl.releasepl_handler(self._update(msg), context))
        self.assertEqual(len(msg.replies), 1)
        self.assertIn("already have a release waiting", msg.replies[0])

    def test_the_multirelease_command_refuses_while_one_is_open(self):
        rl._open_prompt(self.TG_ID, "multi", 77)
        msg = _Msg()
        context = MagicMock()
        context.args = ["7", "11"]
        asyncio.run(rl.releasemultiple_handler(self._update(msg), context))
        self.assertEqual(len(msg.replies), 1)
        self.assertIn("already have a multi-release waiting", msg.replies[0])

    def test_cancelling_reopens_releases_immediately(self):
        rl._open_prompt(self.TG_ID, "release", 77)
        query = MagicMock()
        query.from_user.id = self.TG_ID
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update = MagicMock()
        update.callback_query = query
        asyncio.run(rl.release_cancel_callback(update, MagicMock()))
        self.assertIsNone(rl.pending_release(self.TG_ID))


if __name__ == "__main__":
    unittest.main()

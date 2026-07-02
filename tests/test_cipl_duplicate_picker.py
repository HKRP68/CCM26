"""Regression tests for the Challenge League (/cipl) duplicate-picker bug.

A re-prompt (a /rcl resume, or the edit-in-place fallback when a Telegram edit
fails) used to leave the previous bowler/approach picker in the chat while
sending a fresh one — two live pickers, each with a different scorecard, both
still tappable. These tests pin the "at most one live picker" invariant on the
low-level message helpers.

They use lightweight fakes for the Telegram bot so they run under plain
unittest without a network or a real Application.
"""

import asyncio
import unittest

from handlers import cipl_play as cp


class _Sent:
    def __init__(self, message_id):
        self.message_id = message_id


class _FakeBot:
    def __init__(self):
        self.sent = []       # list of message_ids sent
        self.deleted = []    # list of message_ids deleted
        self.edited = []     # list of message_ids edited
        self._next_id = 1000
        self.edit_should_fail = False

    async def send_message(self, chat_id, text, **kwargs):
        self._next_id += 1
        self.sent.append(self._next_id)
        return _Sent(self._next_id)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kwargs):
        if self.edit_should_fail:
            raise RuntimeError("simulated edit failure")
        self.edited.append(message_id)


class _FakeContext:
    def __init__(self):
        self.bot = _FakeBot()
        self.bot_data = {}
        self.job_queue = None


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class NewActionMessageTests(unittest.TestCase):
    def test_replaces_previous_picker(self):
        ctx = _FakeContext()
        state = {"chat_id": 42, "match_id": 7, "action_msg_id": 500,
                 "over_msg_ids": [500]}
        _run(cp._new_action_message(ctx, state, "pick a bowler", []))
        # The old picker (500) must be deleted, and exactly one new message sent.
        self.assertIn(500, ctx.bot.deleted)
        self.assertEqual(len(ctx.bot.sent), 1)
        # action_msg_id now points at the new message; the stale id is gone.
        self.assertEqual(state["action_msg_id"], ctx.bot.sent[0])
        self.assertNotIn(500, state["over_msg_ids"])
        self.assertIn(ctx.bot.sent[0], state["over_msg_ids"])

    def test_no_delete_when_no_previous(self):
        ctx = _FakeContext()
        state = {"chat_id": 42, "match_id": 7, "action_msg_id": None,
                 "over_msg_ids": []}
        _run(cp._new_action_message(ctx, state, "pick a bowler", []))
        self.assertEqual(ctx.bot.deleted, [])
        self.assertEqual(len(ctx.bot.sent), 1)


class EditActionMessageTests(unittest.TestCase):
    def test_edit_in_place_keeps_single_message(self):
        ctx = _FakeContext()
        state = {"chat_id": 42, "match_id": 7, "action_msg_id": 500,
                 "over_msg_ids": [500]}
        _run(cp._edit_action_message(ctx, state, "choose approach", []))
        # Successful edit → no new message, no delete, same id retained.
        self.assertEqual(ctx.bot.sent, [])
        self.assertEqual(ctx.bot.deleted, [])
        self.assertEqual(ctx.bot.edited, [500])
        self.assertEqual(state["action_msg_id"], 500)

    def test_edit_failure_falls_back_without_duplicate(self):
        ctx = _FakeContext()
        ctx.bot.edit_should_fail = True
        state = {"chat_id": 42, "match_id": 7, "action_msg_id": 500,
                 "over_msg_ids": [500]}
        _run(cp._edit_action_message(ctx, state, "choose approach", []))
        # Edit failed → the stale message is deleted and one fresh one sent, so
        # the chat is never left with two approach pickers.
        self.assertIn(500, ctx.bot.deleted)
        self.assertEqual(len(ctx.bot.sent), 1)
        self.assertEqual(state["action_msg_id"], ctx.bot.sent[0])
        self.assertNotIn(500, state["over_msg_ids"])


if __name__ == "__main__":
    unittest.main()

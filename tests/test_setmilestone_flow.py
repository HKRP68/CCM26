"""/setmilestone — the way out of the command, and text sent with a clip.

Two things this file pins, both of them about an admin standing in Telegram
rather than sitting at the /media page:

  * every screen has a button that closes the flow. /setmilestone parks what it
    is waiting for in ``user_data``; an admin who opened it by accident, or who
    changed their mind at the "send me a GIF" prompt, had nothing to press and
    had to remember to type /cancel — and until they did, their next message in
    the chat was swallowed by the flow.
  * a caption sent *with* an uploaded clip is that clip's message. Composing
    the two together is how a milestone is actually written on a phone, and the
    upload path used to throw the caption away.
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import handlers.setmilestone as sm


def _run(coro):
    # asyncio.run, not get_event_loop: the suite runs alongside
    # IsolatedAsyncioTestCase files that close the loop they were handed, and a
    # closed loop here fails these tests only when the whole suite runs.
    return asyncio.run(coro)


class _Query:
    def __init__(self, data):
        self.data = data
        self.answers = []
        self.edits = []
        self.markups = []

    async def answer(self, text="", **kwargs):
        self.answers.append(text)

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)
        self.markups.append(kwargs.get("reply_markup"))


class _Message:
    """An uploaded clip, with or without a caption."""

    def __init__(self, caption=None, caption_html=None):
        self.text = None
        self.caption = caption
        self.caption_html = caption_html
        self.animation = SimpleNamespace(file_id="FILEID")
        self.photo = None
        self.video = None
        self.document = None
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class _Row:
    def __init__(self, **kw):
        self.id = kw.get("id", 1)
        self.event_key = kw.get("event_key", "six")
        self.source_type = kw.get("source_type", "telegram")
        self.source = kw.get("source", "OLD")
        self.caption = kw.get("caption")
        self.media_type = kw.get("media_type")
        self.weight = kw.get("weight", 1)
        self.enabled = kw.get("enabled", True)
        self.label = kw.get("label", "")


class _FakeDb:
    """Just enough SQLAlchemy to run _store_media_from_message."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.added = []
        self.committed = False

    def query(self, _model):
        return self

    def filter(self, *_a, **_k):
        return self

    def order_by(self, *_a, **_k):
        return self

    def all(self):
        return list(self.rows)

    def first(self):
        return self.rows[-1] if self.rows else None

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        pass


def _labels(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


class CloseButtonTests(unittest.TestCase):
    """Every screen offers a way out."""

    def setUp(self):
        self._p = patch.object(sm, "_is_admin_update", lambda update: True)
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_the_milestone_list_carries_a_close_button(self):
        labels = [b.text for row in sm._keys_keyboard() for b in row]
        self.assertIn("✖️ Close", labels)

    def test_a_prompt_waiting_on_a_message_carries_back_and_cancel(self):
        labels = [b.text for row in sm._back_keyboard("six") for b in row]
        self.assertEqual(labels, ["⬅️ Back", "✖️ Cancel"])

    def test_the_cancel_button_is_answered_not_read_as_a_milestone(self):
        # msm_x_ carries no event key. Answering it after the key lookup would
        # reject the admin's own Cancel button as an unknown milestone.
        query = _Query(f"{sm.CB}_x_")
        context = SimpleNamespace(user_data={sm.AWAIT_KEY: {"mode": "media",
                                                           "event_key": "six"}})
        _run(sm.milestone_callback(
            SimpleNamespace(callback_query=query,
                            effective_user=SimpleNamespace(id=1)), context))
        self.assertNotIn(sm.AWAIT_KEY, context.user_data,
                         "closing must drop the pending wait")
        self.assertTrue(query.edits, "the menu should be edited away")
        self.assertIn("closed", query.edits[0].lower())
        self.assertNotIn("Unknown milestone.", query.answers)

    def test_closing_frees_the_admins_next_message(self):
        """The reply catcher must stop swallowing messages once Cancel is hit."""
        context = SimpleNamespace(user_data={sm.AWAIT_KEY: {"mode": "caption",
                                                            "event_key": "six"}})
        _run(sm.milestone_callback(
            SimpleNamespace(callback_query=_Query(f"{sm.CB}_x_"),
                            effective_user=SimpleNamespace(id=1)), context))
        msg = _Message()
        msg.text = "just chatting"
        consumed = _run(sm.milestone_reply_handler(
            SimpleNamespace(effective_message=msg,
                            effective_user=SimpleNamespace(id=1)), context))
        self.assertFalse(consumed)
        self.assertEqual(msg.replies, [])

    def test_the_back_button_returns_to_that_milestones_screen(self):
        # Back is the ordinary "open this milestone" callback, so it both
        # re-renders the screen and clears the pending wait.
        data = sm._back_keyboard("six")[0][0].callback_data
        self.assertEqual(data, f"{sm.CB}_k_six")


class UploadedCaptionTests(unittest.TestCase):
    """Text sent with the clip is that clip's message."""

    def test_a_caption_sent_with_the_clip_is_stored_on_it(self):
        db = _FakeDb()
        with patch.object(sm, "get_session", lambda: db):
            stored, caption_set = sm._store_media_from_message(
                _Message(caption="SIX! {player} goes big"), "six")
        self.assertTrue(stored)
        self.assertTrue(caption_set)
        self.assertEqual(db.added[0].caption, "SIX! {player} goes big")
        self.assertEqual(db.added[0].source, "FILEID")

    def test_the_html_caption_wins_so_formatting_survives(self):
        # fire_event_media sends captions with parse_mode="HTML"; the plain
        # text would arrive with the admin's bold/italic stripped out.
        db = _FakeDb()
        with patch.object(sm, "get_session", lambda: db):
            sm._store_media_from_message(
                _Message(caption="SIX!", caption_html="<b>SIX!</b>"), "six")
        self.assertEqual(db.added[0].caption, "<b>SIX!</b>")

    def test_an_uncaptioned_clip_keeps_the_message_already_set(self):
        db = _FakeDb([_Row(caption="Existing line")])
        with patch.object(sm, "get_session", lambda: db):
            stored, caption_set = sm._store_media_from_message(_Message(), "six")
        self.assertTrue(stored)
        self.assertFalse(caption_set)
        self.assertEqual(db.added[0].caption, "Existing line")

    def test_a_caption_overrides_the_one_already_set_for_this_clip(self):
        db = _FakeDb([_Row(caption="Old line")])
        with patch.object(sm, "get_session", lambda: db):
            sm._store_media_from_message(_Message(caption="New line"), "six")
        self.assertEqual(db.added[0].caption, "New line")
        self.assertEqual(db.rows[0].caption, "Old line",
                         "the other clip keeps its own message")

    def test_a_message_with_no_media_is_refused(self):
        msg = _Message()
        msg.animation = None
        stored, caption_set = sm._store_media_from_message(msg, "six")
        self.assertFalse(stored)
        self.assertFalse(caption_set)

    def test_the_admin_is_told_their_caption_was_saved(self):
        db = _FakeDb()
        context = SimpleNamespace(user_data={sm.AWAIT_KEY: {"mode": "media",
                                                            "event_key": "six"}})
        msg = _Message(caption="BOOM")
        with patch.object(sm, "_is_admin_update", lambda update: True), \
                patch.object(sm, "get_session", lambda: db), \
                patch.object(sm, "_invalidate", lambda: None):
            consumed = _run(sm.milestone_reply_handler(
                SimpleNamespace(effective_message=msg,
                                effective_user=SimpleNamespace(id=1)), context))
        self.assertTrue(consumed)
        self.assertIn("caption", msg.replies[0].lower())
        self.assertNotIn(sm.AWAIT_KEY, context.user_data)


if __name__ == "__main__":
    unittest.main()

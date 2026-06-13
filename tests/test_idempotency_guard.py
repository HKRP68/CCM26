"""Tests for the shared idempotency guard.

Covers the abuse fix: rapid/duplicate inline-button taps must apply an action
at most once (utils.idempotency). Duplicate player ownership itself is allowed
by the game, so there is no DB-level uniqueness to test here.
"""

import asyncio
import threading
import time
import unittest
from unittest.mock import patch

from utils.idempotency import claim_once, release, in_progress, _GUARD


class IdempotencyGuardTests(unittest.TestCase):
    def setUp(self):
        _GUARD.clear()

    def test_first_wins_second_loses(self):
        self.assertTrue(claim_once("k"))
        self.assertFalse(claim_once("k"))

    def test_release_allows_reclaim(self):
        self.assertTrue(claim_once("k"))
        release("k")
        self.assertTrue(claim_once("k"))

    def test_ttl_expiry_allows_reclaim(self):
        self.assertTrue(claim_once("k", ttl=0.01))
        self.assertFalse(claim_once("k", ttl=0.01))
        time.sleep(0.02)
        self.assertTrue(claim_once("k", ttl=0.01))

    def test_distinct_keys_independent(self):
        self.assertTrue(claim_once("a"))
        self.assertTrue(claim_once("b"))

    def test_in_progress(self):
        self.assertFalse(in_progress("k"))
        claim_once("k")
        self.assertTrue(in_progress("k"))
        release("k")
        self.assertFalse(in_progress("k"))

    def test_concurrent_only_one_winner(self):
        """Model concurrent_updates=16: many threads race one key, one wins."""
        _GUARD.clear()
        winners = []
        barrier = threading.Barrier(32)

        def worker():
            barrier.wait()
            if claim_once("same"):
                winners.append(1)

        threads = [threading.Thread(target=worker) for _ in range(32)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(winners), 1)


class _DummyMessage:
    def __init__(self, chat_id=100, message_id=200):
        self.chat_id = chat_id
        self.message_id = message_id
        self.replies = []

    async def reply_text(self, *a, **k):
        self.replies.append((a, k))


class _DummyQuery:
    def __init__(self, data, chat_id=100, message_id=200, user_id=7):
        self.data = data
        self.from_user = type("U", (), {"id": user_id, "username": "u", "first_name": "f"})()
        self.message = _DummyMessage(chat_id, message_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)

    async def edit_message_reply_markup(self, *a, **k):
        pass


class _DummyUpdate:
    def __init__(self, query):
        self.callback_query = query
        self.effective_chat = type("C", (), {"id": 100, "type": "private"})()


try:
    import telegram  # noqa: F401
    _HAVE_TELEGRAM = True
except Exception:
    _HAVE_TELEGRAM = False


@unittest.skipUnless(_HAVE_TELEGRAM, "python-telegram-bot not installed")
class BuyHandlerGuardTests(unittest.TestCase):
    """The core fix: a duplicate tap on the same Buy button must be rejected by
    the guard BEFORE any DB work (get_session) happens."""

    def setUp(self):
        _GUARD.clear()

    def test_duplicate_buy_tap_short_circuits_before_db(self):
        import handlers.buy as buy

        # Simulate the first tap already in flight by claiming its key.
        q = _DummyQuery(data="buypl_5_7", chat_id=100, message_id=200)
        key = f"buy_{q.message.chat_id}_{q.message.message_id}"
        self.assertTrue(claim_once(key))  # first tap won

        def _boom():
            raise AssertionError("get_session must not be called for a duplicate tap")

        with patch.object(buy, "get_session", _boom):
            asyncio.run(buy.buypl_confirm_callback(_DummyUpdate(q), None))

        # The duplicate was answered and never touched the DB.
        self.assertIn("Already processing…", q.answers)


if __name__ == "__main__":
    unittest.main()

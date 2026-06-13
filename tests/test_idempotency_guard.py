"""Tests for the shared idempotency guard and the user_roster dedup migration.

Covers the abuse fix: rapid/duplicate inline-button taps must apply an action
at most once (utils.idempotency), and the DB must collapse any pre-existing
duplicate roster rows and reject new ones (the migration in database.py).
"""

import asyncio
import sqlite3
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


class RosterDedupMigrationTests(unittest.TestCase):
    """The three SQL statements added to database.py's migration_sql list."""

    DEDUP = ("DELETE FROM user_roster WHERE id NOT IN ("
             "  SELECT MIN(id) FROM user_roster GROUP BY user_id, player_id)")
    RECOUNT = ("UPDATE users SET roster_count = ("
               "  SELECT COUNT(*) FROM user_roster WHERE user_roster.user_id = users.id)")
    INDEX = ("CREATE UNIQUE INDEX IF NOT EXISTS uq_user_roster_user_player "
             "ON user_roster (user_id, player_id)")

    def _build(self):
        con = sqlite3.connect(":memory:")
        con.executescript(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, roster_count INTEGER);"
            "CREATE TABLE user_roster (id INTEGER PRIMARY KEY, user_id INTEGER, player_id INTEGER);"
        )
        # User 1 owns player 10 THREE times (dupes) + player 11 once → wrong count 99.
        con.execute("INSERT INTO users VALUES (1, 99)")
        con.executemany("INSERT INTO user_roster (id, user_id, player_id) VALUES (?,?,?)",
                        [(1, 1, 10), (2, 1, 10), (3, 1, 10), (4, 1, 11)])
        return con

    def _migrate(self, con):
        con.execute(self.DEDUP)
        con.execute(self.RECOUNT)
        con.execute(self.INDEX)
        con.commit()

    def test_collapses_dupes_keeps_lowest_id(self):
        con = self._build()
        self._migrate(con)
        rows = con.execute(
            "SELECT id, player_id FROM user_roster ORDER BY player_id").fetchall()
        self.assertEqual(rows, [(1, 10), (4, 11)])  # lowest id (1) kept for player 10

    def test_recomputes_roster_count(self):
        con = self._build()
        self._migrate(con)
        count = con.execute("SELECT roster_count FROM users WHERE id=1").fetchone()[0]
        self.assertEqual(count, 2)

    def test_index_rejects_new_duplicate(self):
        con = self._build()
        self._migrate(con)
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("INSERT INTO user_roster (id, user_id, player_id) VALUES (5, 1, 11)")

    def test_migration_is_idempotent(self):
        con = self._build()
        self._migrate(con)
        self._migrate(con)  # second run must not error or change anything
        rows = con.execute("SELECT id, player_id FROM user_roster ORDER BY player_id").fetchall()
        self.assertEqual(rows, [(1, 10), (4, 11)])
        count = con.execute("SELECT roster_count FROM users WHERE id=1").fetchone()[0]
        self.assertEqual(count, 2)


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

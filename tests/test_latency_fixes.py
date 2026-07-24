"""Tests for the response-latency ("ping") fixes.

The bot's reported ping was far above comparable bots. The causes were all
self-inflicted work on the shared asyncio event loop, not the Telegram link:

  * every database session checkout paid an extra ``SELECT 1`` liveness round
    trip (pool_pre_ping), doubling the cost of a one-query operation against a
    database ~100ms away;
  * /botstatus itself ran two full-table COUNTs inline on the event loop;
  * the Mini App re-downloaded and re-encoded every player card on every view
    because the endpoint sent ``no-cache``, burning CPU in the bot's process.

There is also a monitor so the split between network and self-inflicted lag is
visible rather than guessed at.
"""

import asyncio
import importlib
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


# Same isolation dance as tests/test_night_outage_fixes.py — other modules in
# this suite leave stub ``models``/``database`` entries in sys.modules.
_REAL_MODULES = ("config", "database", "models")
_SAVED_MODULES = {}
_SAVED_DB_URL = None
_TMP_DB = None


def setUpModule():
    global _SAVED_DB_URL, _TMP_DB
    _SAVED_DB_URL = os.environ.get("DATABASE_URL")
    _TMP_DB = tempfile.TemporaryDirectory()
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.name}/lat.db"
    for name in _REAL_MODULES:
        _SAVED_MODULES[name] = sys.modules.pop(name, None)
    for name in _REAL_MODULES:
        importlib.import_module(name)


def tearDownModule():
    for name, mod in _SAVED_MODULES.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod
    if _SAVED_DB_URL is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _SAVED_DB_URL
    if _TMP_DB is not None:
        _TMP_DB.cleanup()


# ════════════════════════════════════════════════════════════════════
# Connection liveness checking
# ════════════════════════════════════════════════════════════════════

class ConnectionPingTests(unittest.TestCase):
    """A hot connection must not pay a liveness round trip; a cold one must."""

    def _engine(self, idle_after):
        from sqlalchemy import create_engine, event
        from sqlalchemy.exc import DisconnectionError

        eng = create_engine(f"sqlite:///{_TMP_DB.name}/ping.db")
        pings = []

        # Mirror database.py's hooks against a throwaway engine.
        @event.listens_for(eng, "checkin")
        def _checkin(dbapi_connection, connection_record):
            connection_record.info["returned_at"] = time.monotonic()

        @event.listens_for(eng, "checkout")
        def _checkout(dbapi_connection, connection_record, connection_proxy):
            returned_at = connection_record.info.get("returned_at")
            if returned_at is None:
                return
            if (time.monotonic() - returned_at) < idle_after:
                return
            pings.append(1)
            try:
                cur = dbapi_connection.cursor()
                try:
                    cur.execute("SELECT 1")
                finally:
                    cur.close()
            except Exception as exc:
                raise DisconnectionError() from exc

        return eng, pings

    def test_hot_checkouts_skip_the_liveness_query(self):
        from sqlalchemy import text

        eng, pings = self._engine(idle_after=30)
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        for _ in range(10):
            with eng.connect() as c:
                c.execute(text("SELECT 1"))
        self.assertEqual(pings, [],
                         "hot connections still paid a liveness round trip")

    def test_idle_checkout_is_validated(self):
        from sqlalchemy import text

        eng, pings = self._engine(idle_after=0.05)
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        time.sleep(0.1)
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        self.assertEqual(len(pings), 1,
                         "an idle connection was reused without validation")

    def test_engine_is_not_configured_with_pool_pre_ping(self):
        """pool_pre_ping would reinstate the per-checkout round trip."""
        import database

        self.assertFalse(getattr(database.engine.pool, "_pre_ping", False),
                         "pool_pre_ping is back on — every session pays an "
                         "extra round trip")

    def test_recycle_still_bounds_connection_age(self):
        """The idle ping replaces pre-ping, not the recycle guard."""
        import database

        if not database._is_postgres:
            self.skipTest("recycle is only configured for Postgres")
        self.assertGreater(database.engine.pool._recycle, 0)


# ════════════════════════════════════════════════════════════════════
# Event-loop lag monitor
# ════════════════════════════════════════════════════════════════════

class LoopMonitorTests(unittest.TestCase):
    def setUp(self):
        from services import loop_monitor
        loop_monitor._samples.clear()

    def test_no_samples_reports_no_data(self):
        from services import loop_monitor
        self.assertEqual(loop_monitor.describe(), "no data")
        self.assertEqual(loop_monitor.get_stats()["samples"], 0)

    def test_verdicts_track_severity(self):
        from services import loop_monitor

        for lag, expected in ((1, "healthy"), (120, "busy"),
                              (500, "congested"), (4000, "blocked")):
            loop_monitor._samples.clear()
            for _ in range(20):
                loop_monitor.record(lag)
            self.assertEqual(loop_monitor.describe(), expected,
                             f"{lag}ms of lag should read as {expected}")

    def test_stats_summarise_the_window(self):
        from services import loop_monitor

        for v in range(100):
            loop_monitor.record(v)
        stats = loop_monitor.get_stats()
        self.assertEqual(stats["samples"], loop_monitor.WINDOW)
        self.assertEqual(stats["max_ms"], 99)
        self.assertGreaterEqual(stats["p95_ms"], stats["avg_ms"])

    def test_negative_lag_is_clamped(self):
        from services import loop_monitor
        loop_monitor.record(-5)
        self.assertEqual(loop_monitor.get_stats()["max_ms"], 0.0)

    def test_monitor_detects_a_blocked_loop(self):
        """The whole point: blocking the loop must show up as lag."""
        from services import loop_monitor

        async def run():
            with patch.object(loop_monitor, "SAMPLE_INTERVAL", 0.02):
                loop_monitor.start()
                await asyncio.sleep(0.1)
                idle = loop_monitor.describe()

                loop_monitor._samples.clear()
                for _ in range(3):
                    time.sleep(0.3)          # a blocking call on the loop
                    await asyncio.sleep(0.02)
                blocked = loop_monitor.get_stats()["p95_ms"]
                loop_monitor.stop()
                return idle, blocked

        idle, blocked = asyncio.run(run())
        self.assertEqual(idle, "healthy")
        self.assertGreater(blocked, 200,
                           "a loop blocked for 300ms was not detected")


# ════════════════════════════════════════════════════════════════════
# /botstatus
# ════════════════════════════════════════════════════════════════════

class _FakeMessage:
    def __init__(self, sink):
        self.sink = sink
        self.chat_id = 1
        self.message_id = 1

    async def reply_text(self, text, **kw):
        self.sink.append(("reply", text))
        return self

    async def edit_text(self, text, **kw):
        self.sink.append(("edit", text))
        return self


class _FakeUpdate:
    def __init__(self, sink):
        self.message = _FakeMessage(sink)


class _FakeContext:
    bot_data = {}


class BotStatusTests(unittest.TestCase):
    def setUp(self):
        from handlers import botstatus
        botstatus._health_cache["at"] = None
        botstatus._health_cache["value"] = None

    def test_health_queries_run_off_the_event_loop(self):
        """Two full-table COUNTs inline would stall every other user."""
        from handlers import botstatus

        thread_ids = []

        def fake_health():
            import threading
            thread_ids.append(threading.get_ident())
            return 3, 1728, True, 12.5

        sink = []

        async def run():
            import threading
            main_thread = threading.get_ident()
            with patch.object(botstatus, "_gather_health", fake_health):
                await botstatus.botstatus_handler(_FakeUpdate(sink), _FakeContext())
            return main_thread

        main_thread = asyncio.run(run())
        self.assertEqual(len(thread_ids), 1)
        self.assertNotEqual(thread_ids[0], main_thread,
                            "health queries ran on the event loop")

    def test_ping_measures_only_the_telegram_call(self):
        """The reported ping must not include the health queries."""
        from handlers import botstatus
        import re

        def slow_health():
            time.sleep(0.4)          # expensive, but must not count as "ping"
            return 1, 1, True, 5.0

        sink = []

        async def run():
            with patch.object(botstatus, "_gather_health", slow_health):
                await botstatus.botstatus_handler(_FakeUpdate(sink), _FakeContext())

        asyncio.run(run())
        final = [t for kind, t in sink if kind == "edit"][-1]
        ping = float(re.search(r"Ping:</b> (\d+) ms", final).group(1))
        self.assertLess(ping, 300,
                        f"ping ({ping}ms) absorbed the health-query time")

    def test_status_reports_the_latency_breakdown(self):
        from handlers import botstatus
        from services import loop_monitor

        loop_monitor._samples.clear()
        for _ in range(10):
            loop_monitor.record(5.0)

        sink = []

        async def run():
            with patch.object(botstatus, "_gather_health",
                              lambda: (2, 1728, True, 42.0)):
                await botstatus.botstatus_handler(_FakeUpdate(sink), _FakeContext())

        asyncio.run(run())
        final = [t for kind, t in sink if kind == "edit"][-1]
        # Each component is broken out so a high ping can be attributed.
        self.assertIn("Telegram round-trip", final)
        self.assertIn("Event loop:", final)
        self.assertIn("42 ms", final)          # database round trip
        self.assertIn("healthy", final)
        self.assertIn("1,728", final)

    def test_status_still_answers_when_the_database_is_down(self):
        """A dead database must degrade the report, not break the command."""
        from handlers import botstatus

        sink = []

        async def run():
            for _ in range(3):
                await botstatus.botstatus_handler(_FakeUpdate(sink), _FakeContext())

        with patch.object(botstatus, "get_session", side_effect=RuntimeError("no db")):
            asyncio.run(run())

        edits = [t for k, t in sink if k == "edit"]
        self.assertEqual(len(edits), 3)
        self.assertIn("Unreachable", edits[-1])

    def test_cache_hit_avoids_a_second_query(self):
        from handlers import botstatus

        queries = []

        class _FakeSession:
            def execute(self, *a, **k):
                queries.append("probe")
                return self

            def scalar(self):
                return 1

            def query(self, *a, **k):
                return self

            def filter(self, *a, **k):
                return self

            def count(self):
                queries.append("count")
                return 7

            def close(self):
                pass

        with patch.object(botstatus, "get_session", lambda: _FakeSession()):
            first = botstatus._gather_health()
            after_first = len(queries)
            second = botstatus._gather_health()

        self.assertEqual(first, second)
        self.assertEqual(len(queries), after_first,
                         "cached health still hit the database")


# ════════════════════════════════════════════════════════════════════
# Mini App player-card caching
# ════════════════════════════════════════════════════════════════════

class PlayerCardCachingTests(unittest.TestCase):
    """Repeat card views must revalidate, not re-download and re-encode.

    Every one of those requests is served by a thread inside the bot's own
    process, so the wasted work competes with the event loop for CPU.
    """

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("ADMIN_PASSWORD", "test")
        try:
            import database
            database.init_db()
            import admin
        except Exception as exc:      # pragma: no cover - env-dependent
            raise unittest.SkipTest(f"admin app unavailable: {exc}")

        from models import Player
        cls.admin = admin
        session = database.get_session()
        try:
            player = session.query(Player).first()
            if player is None:
                player = Player(name="Cache Probe", rating=85, category="Batsman",
                                country="India", bat_hand="Right",
                                bowl_hand="Right", bowl_style="Medium",
                                is_active=True)
                session.add(player)
                session.commit()
            cls.player_id = player.id
        finally:
            session.close()

        admin.app.config["TESTING"] = True
        cls.client = admin.app.test_client()

    def _get(self, **kw):
        with patch("services.card_generator.generate_card",
                   lambda player: b"\x89PNG\r\n\x1a\n" + b"x" * 2000):
            return self.client.get(
                f"/webapp/player-card/{self.player_id}", **kw)

    def test_card_response_is_cacheable(self):
        resp = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.headers.get("ETag"))
        cache_control = resp.headers.get("Cache-Control", "")
        self.assertNotIn("no-cache", cache_control)
        self.assertIn("max-age=", cache_control)

    def test_revalidation_returns_304_with_no_body(self):
        first = self._get()
        etag = first.headers["ETag"]
        second = self._get(headers={"If-None-Match": etag})
        self.assertEqual(second.status_code, 304)
        self.assertEqual(len(second.data), 0)
        self.assertEqual(second.headers.get("ETag"), etag)

    def test_changed_art_produces_a_new_etag(self):
        """An admin re-upload must still invalidate the cached copy."""
        first = self._get()
        with patch("services.card_generator.generate_card",
                   lambda player: b"\x89PNG\r\n\x1a\n" + b"DIFFERENT"):
            changed = self.client.get(f"/webapp/player-card/{self.player_id}")
        self.assertNotEqual(first.headers["ETag"], changed.headers["ETag"])


if __name__ == "__main__":
    unittest.main()

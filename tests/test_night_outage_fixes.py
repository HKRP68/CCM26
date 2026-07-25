"""Tests for the fixes behind the recurring night-time bot outages.

Three separate mechanisms made the bot go quiet for a minute or two at peak
(evening IST) hours:

  1. Boot cost. ``init_db`` issued a transaction per ALTER — hundreds of round
     trips to a remote Postgres — so *any* restart took ~75s of total downtime.
  2. Notification blasts. ``fire_schedule`` sent to every target user in a
     tight loop on the bot's event loop, unpaced, holding one DB session open
     for the whole run, with ``last_fired_at`` uncommitted until the end (so
     the next heartbeat tick could start the same blast again).
  3. The cooldown notifier loaded the entire users table on every 5-minute
     tick and did all its DB work on the event loop; the job was observed
     overrunning its own interval.
"""

import asyncio
import importlib
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch


# Several test modules in this suite install stub ``models``/``database``
# modules into sys.modules at import time and never remove them, which makes
# the real ones un-importable for everything that runs afterwards. Load the
# genuine modules for the duration of this file, then put back whatever was
# there so the next test module sees what it expects.
_REAL_MODULES = ("config", "database", "models",
                 "services.notification_service", "services.cooldown_notifier")
_SAVED_MODULES = {}
_SAVED_DB_URL = None
_TMP_DB = None


def setUpModule():
    global _SAVED_DB_URL, _TMP_DB

    _SAVED_DB_URL = os.environ.get("DATABASE_URL")
    _TMP_DB = tempfile.TemporaryDirectory()
    # Point the module-level engine at a throwaway file so importing database
    # never touches a real one.
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.name}/import.db"

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
# 1. Boot cost — migrations must be skipped once they've been applied
# ════════════════════════════════════════════════════════════════════

class MigrationRoundTripTests(unittest.TestCase):
    """A warm start must not re-issue DDL for columns that already exist."""

    def setUp(self):
        """Bind database.py to a throwaway SQLite file built from the models.

        Patching the module's engine keeps the real ``_migrate_add_columns``
        under test — re-importing the module instead would leave ``models``
        bound to the original Base, so create_all would build nothing.
        """
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        import database as db
        import models  # noqa: F401 — populates Base.metadata

        self.db = db
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

        self.engine = create_engine(f"sqlite:///{self._tmp.name}/t.db")
        db.Base.metadata.create_all(self.engine)

        for target, value in (("engine", self.engine),
                              ("_is_postgres", False),
                              ("SessionLocal", sessionmaker(bind=self.engine))):
            p = patch.object(db, target, value)
            p.start()
            self.addCleanup(p.stop)

    def test_warm_start_issues_no_add_column_statements(self):
        from sqlalchemy import event

        self.db._migrate_add_columns()      # first pass

        seen = []
        event.listen(self.engine, "before_cursor_execute",
                     lambda c, cur, s, p, ctx, m: seen.append(s))
        self.db._migrate_add_columns()      # warm start

        adds = [s for s in seen if "ADD COLUMN" in s.upper()]
        self.assertEqual(
            adds, [],
            f"warm start still issued {len(adds)} ADD COLUMN statements")

    def test_existing_schema_is_read_in_one_query(self):
        """The skip only pays off if discovering the schema is itself cheap."""
        cols = self.db._load_existing_columns()
        self.assertIsNotNone(cols)
        self.assertIn("users", cols)
        self.assertIn("telegram_id", cols["users"])

    def test_missing_column_is_still_added(self):
        """Skipping known columns must not skip genuinely missing ones."""
        with self.engine.begin() as conn:
            conn.execute(self.db.text("CREATE TABLE mig_probe (id INTEGER)"))

        existing = self.db._load_existing_columns()
        self.assertNotIn("added_later", existing.get("mig_probe", set()))

        with self.engine.begin() as conn:
            conn.execute(self.db.text(
                "ALTER TABLE mig_probe ADD COLUMN added_later INTEGER"))

        refreshed = self.db._load_existing_columns()
        self.assertIn("added_later", refreshed["mig_probe"])

    def test_signature_gate_skips_applied_batches(self):
        stmts = ["SELECT 1", "SELECT 2"]
        done, sig = self.db._migration_signature_matches("unit-test", stmts)
        self.assertFalse(done, "a never-run batch must not be skipped")

        self.db._record_migration_signature("unit-test", sig)
        done_again, _ = self.db._migration_signature_matches("unit-test", stmts)
        self.assertTrue(done_again, "an applied batch should be skipped")

    def test_editing_a_batch_invalidates_the_marker(self):
        _, sig = self.db._migration_signature_matches("edit-test", ["SELECT 1"])
        self.db._record_migration_signature("edit-test", sig)

        # A new statement added to the batch must make it run again.
        done_after, _ = self.db._migration_signature_matches(
            "edit-test", ["SELECT 1", "SELECT 2"])
        self.assertFalse(done_after,
                         "changing the statements must re-run the batch")

    def test_failed_statements_are_isolated_and_reported(self):
        failures = self.db._run_isolated([
            "CREATE TABLE iso_ok_1 (id INTEGER)",
            "THIS IS NOT SQL",
            "CREATE TABLE iso_ok_2 (id INTEGER)",
        ])
        self.assertEqual(len(failures), 1)
        # The statements either side of the bad one still committed.
        from sqlalchemy import inspect
        names = inspect(self.engine).get_table_names()
        self.assertIn("iso_ok_1", names)
        self.assertIn("iso_ok_2", names)


# ════════════════════════════════════════════════════════════════════
# 2. Notification blasts — paced, non-blocking, single-fire
# ════════════════════════════════════════════════════════════════════

class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class _FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.telegram_id = 10_000_000_000 + uid   # deliberately > 2**31
        self.first_name = f"P{uid}"
        self.username = None
        self.total_coins = 0
        self.total_gems = 0
        self.win_streak = 0
        self.best_streak = 0
        self.matches_won = 0
        self.quest_points = 0


class NotificationBlastTests(unittest.TestCase):
    @staticmethod
    def _run_blast(bot, users, template, rate=1e6, **overrides):
        """Run a blast with pacing effectively disabled unless asked otherwise."""
        from services import notification_service as ns

        async def run():
            with patch.object(ns, "SENDS_PER_SECOND", rate), \
                 patch.object(ns, "_write_delivery_logs",
                              overrides.get("log_writer", lambda rows: None)), \
                 patch.object(ns, "LOG_CHUNK_SIZE",
                              overrides.get("chunk", ns.LOG_CHUNK_SIZE)):
                return await ns._blast(bot, users, template)

        return asyncio.run(run())

    def _snapshots(self, n):
        from services import notification_service as ns
        return [ns._UserSnapshot(_FakeUser(i)) for i in range(n)]

    def test_blast_is_paced_to_the_configured_rate(self):
        """Unpaced sending is what saturated Telegram and stalled the bot."""
        import time

        bot = _FakeBot()
        users = self._snapshots(5)

        started = time.monotonic()
        ok, failed = self._run_blast(bot, users, "hi", rate=50)
        elapsed = time.monotonic() - started

        self.assertEqual((ok, failed), (5, 0))
        # 5 sends at 50/s must take at least the 4 intervals between them.
        self.assertGreaterEqual(
            elapsed, 4 / 50,
            f"blast finished in {elapsed:.3f}s — pacing was not applied")

    def test_blast_renders_per_user_placeholders(self):
        from services import notification_service as ns

        bot = _FakeBot()
        users = [ns._UserSnapshot(_FakeUser(i)) for i in (1, 2)]
        self._run_blast(bot, users, "yo {first_name}")
        self.assertEqual([t for _, t in bot.sent], ["yo P1", "yo P2"])

    def test_blast_sends_to_large_telegram_ids(self):
        bot = _FakeBot()
        users = self._snapshots(1)
        self._run_blast(bot, users, "hi")
        self.assertGreater(bot.sent[0][0], 2 ** 31)

    def test_failed_sends_are_counted_not_raised(self):
        class _BrokenBot:
            async def send_message(self, **kw):
                raise RuntimeError("bot was blocked by the user")

        ok, failed = self._run_blast(_BrokenBot(), self._snapshots(3), "hi")
        self.assertEqual((ok, failed), (0, 3))

    def test_delivery_logs_are_written_in_chunks(self):
        """A blast must not accumulate one giant transaction at the end."""
        chunks = []
        self._run_blast(_FakeBot(), self._snapshots(25), "hi",
                        chunk=10, log_writer=lambda rows: chunks.append(len(rows)))
        self.assertEqual(chunks, [10, 10, 5])

    @staticmethod
    def _tick_harness(fire_impl):
        """Drive maybe_tick twice with a stub schedule + session."""
        from services import notification_service as ns

        class _Schedule:
            id = 1
            name = "evening blast"
            is_active = True

        class _Session:
            def query(self, *a, **k):
                return self

            def filter(self, *a, **k):
                return self

            def all(self):
                return [_Schedule()]

            def get(self, *a, **k):
                return _Schedule()

            def close(self):
                pass

        class _App:
            bot = _FakeBot()

        async def run():
            with patch.object(ns, "fire_schedule", fire_impl), \
                 patch.object(ns, "should_fire", lambda s: True), \
                 patch("database.get_session", lambda: _Session()):
                ns._LAST_TICK_MINUTE = None
                ns._BLAST_IN_PROGRESS = False
                await ns.maybe_tick(_App())     # starts the detached blast
                await asyncio.sleep(0)          # let it get going
                ns._LAST_TICK_MINUTE = None     # simulate the next minute's tick
                await ns.maybe_tick(_App())     # must be a no-op
                if ns._BLAST_TASK is not None:
                    await ns._BLAST_TASK

        asyncio.run(run())

    def test_tick_does_not_start_an_overlapping_blast(self):
        """The bug: a blast outlives many ticks, and each tick re-fired it."""
        started = []

        async def slow_fire(bot, session, sched):
            started.append(sched.name)
            await asyncio.sleep(0.05)   # blast still running across ticks
            return 1, 0

        self._tick_harness(slow_fire)
        self.assertEqual(started, ["evening blast"],
                         "a second tick started an overlapping blast")

    def test_tick_returns_without_waiting_for_the_blast(self):
        """Awaiting the blast here would stall the 30s heartbeat job."""
        import time
        from services import notification_service as ns

        blast_seconds = 0.3

        async def slow_fire(bot, session, sched):
            await asyncio.sleep(blast_seconds)
            return 1, 0

        class _Schedule:
            id = 1
            name = "evening blast"
            is_active = True

        class _Session:
            def query(self, *a, **k):
                return self

            def filter(self, *a, **k):
                return self

            def all(self):
                return [_Schedule()]

            def get(self, *a, **k):
                return _Schedule()

            def close(self):
                pass

        class _App:
            bot = _FakeBot()

        async def run():
            with patch.object(ns, "fire_schedule", slow_fire), \
                 patch.object(ns, "should_fire", lambda s: True), \
                 patch("database.get_session", lambda: _Session()):
                ns._LAST_TICK_MINUTE = None
                ns._BLAST_IN_PROGRESS = False
                # Time the tick itself, not the blast it spawns.
                started = time.monotonic()
                await ns.maybe_tick(_App())
                tick_seconds = time.monotonic() - started
                if ns._BLAST_TASK is not None:
                    await ns._BLAST_TASK       # don't leak the task
                return tick_seconds

        tick_seconds = asyncio.run(run())
        self.assertLess(
            tick_seconds, blast_seconds,
            f"maybe_tick took {tick_seconds:.2f}s for a {blast_seconds}s blast "
            "— it awaited the blast instead of detaching it")

    def test_manual_send_is_refused_while_a_blast_runs(self):
        """Two concurrent blasts would each pace to the limit and double it."""
        from services import notification_service as ns

        class _Schedule:
            id = 1
            name = "manual"
            message = "hi"
            target_filter = "all"
            schedule_type = "one_off"
            last_fired_at = None
            sent_count = 0

        class _Session:
            def get(self, *a, **k):
                return _Schedule()

            def commit(self):
                pass

            def close(self):
                pass

        async def run():
            ns._BLAST_IN_PROGRESS = True     # a scheduled blast is mid-flight
            try:
                with self.assertRaises(ns.BlastAlreadyRunning):
                    await ns.fire_one_off(_FakeBot(), _Session(), 1)
            finally:
                ns._BLAST_IN_PROGRESS = False

        asyncio.run(run())

    def test_manual_one_off_claims_the_schedule_before_sending(self):
        """should_fire() treats a one_off as due while last_fired_at is NULL."""
        from services import notification_service as ns

        claimed_before_send = []

        class _Schedule:
            id = 1
            name = "manual"
            message = "hi"
            target_filter = "all"
            schedule_type = "one_off"
            last_fired_at = None
            sent_count = 0

        sched = _Schedule()

        class _Session:
            def get(self, *a, **k):
                return sched

            def commit(self):
                pass

            def close(self):
                pass

        async def fake_blast(bot, users, template, schedule_id=None):
            # By the time any message goes out the schedule must be claimed,
            # otherwise a heartbeat tick fires the same blast underneath us.
            claimed_before_send.append(sched.last_fired_at is not None)
            return 1, 0

        async def run():
            ns._BLAST_IN_PROGRESS = False
            with patch.object(ns, "_snapshot_targets", lambda t: [object()]), \
                 patch.object(ns, "_blast", fake_blast):
                await ns.fire_one_off(_FakeBot(), _Session(), 1)

        asyncio.run(run())
        self.assertEqual(claimed_before_send, [True])
        self.assertFalse(ns._BLAST_IN_PROGRESS, "the overlap flag leaked")

    def test_manual_send_clears_the_flag_on_failure(self):
        from services import notification_service as ns

        class _Schedule:
            id = 1
            name = "manual"
            message = "hi"
            target_filter = "all"
            schedule_type = "daily"
            last_fired_at = None
            sent_count = 0

        class _Session:
            def get(self, *a, **k):
                return _Schedule()

            def commit(self):
                pass

            def close(self):
                pass

        async def boom(bot, users, template, schedule_id=None):
            raise RuntimeError("telegram exploded")

        async def run():
            ns._BLAST_IN_PROGRESS = False
            with patch.object(ns, "_snapshot_targets", lambda t: [object()]), \
                 patch.object(ns, "_blast", boom):
                with self.assertRaises(RuntimeError):
                    await ns.fire_one_off(_FakeBot(), _Session(), 1)

        asyncio.run(run())
        self.assertFalse(ns._BLAST_IN_PROGRESS,
                         "a failed manual send left notifications blocked")

    def test_blast_failure_clears_the_in_progress_flag(self):
        """A crashed blast must not wedge notifications off forever."""
        from services import notification_service as ns

        async def boom(bot, session, sched):
            raise RuntimeError("telegram exploded")

        self._tick_harness(boom)
        self.assertFalse(ns._BLAST_IN_PROGRESS,
                         "a failed blast left notifications permanently blocked")


# ════════════════════════════════════════════════════════════════════
# 3. Cooldown notifier — bounded scan, off the event loop
# ════════════════════════════════════════════════════════════════════

class CooldownScanTests(unittest.TestCase):
    def test_scan_is_bounded_and_ordered(self):
        """The scan must LIMIT and walk by id, not load the whole table."""
        import inspect as _inspect
        from services import cooldown_notifier as cn

        src = _inspect.getsource(cn._scan_slice)
        self.assertIn(".limit(SCAN_LIMIT)", src)
        self.assertIn(".order_by(User.id)", src)
        self.assertIn(".filter(User.id > after_id)", src)

    def test_db_work_runs_off_the_event_loop(self):
        """Only the Telegram sends may touch the loop; scans go to a thread."""
        import inspect as _inspect
        from services import cooldown_notifier as cn

        src = " ".join(_inspect.getsource(cn.run_cooldown_notifications).split())
        self.assertIn("to_thread( _scan_slice", src)
        self.assertIn("to_thread(_mark_notified", src)

    def test_cursor_advances_then_wraps(self):
        from services import cooldown_notifier as cn

        calls = []

        def fake_scan(after_id, quiet):
            calls.append(after_id)
            if after_id == 0:
                return [], 500, False     # a full slice; resume at 500
            return [], 640, True          # end of table; wrap to 0

        async def run():
            with patch.object(cn, "_scan_slice", fake_scan):
                cn._SCAN_AFTER_ID = 0
                await cn.run_cooldown_notifications(object())
                first = cn._SCAN_AFTER_ID
                await cn.run_cooldown_notifications(object())
                return first, cn._SCAN_AFTER_ID

        first, second = asyncio.run(run())
        self.assertEqual(calls, [0, 500])
        self.assertEqual(first, 500, "cursor did not advance after a full slice")
        self.assertEqual(second, 0, "cursor did not wrap at the end of the table")

    def test_quiet_hours_window(self):
        from services import cooldown_notifier as cn

        for hour, expected in ((23, True), (2, True), (6, True),
                               (7, False), (21, False), (22, False)):
            with patch.object(cn, "_ist_now",
                              lambda h=hour: datetime(2026, 7, 24, h, 0)):
                self.assertEqual(cn._in_quiet_hours(), expected,
                                 f"quiet-hours wrong at {hour}:00 IST")


# ════════════════════════════════════════════════════════════════════
# 4. Duplicate match finalization
# ════════════════════════════════════════════════════════════════════

class FinalizeLockTests(unittest.TestCase):
    def test_finalize_locks_the_match_row_before_checking_status(self):
        """Without the row lock, concurrent callers all re-finalize the match."""
        import inspect as _inspect
        from services import match_webapp_service as mws

        src = _inspect.getsource(mws.finalize_webapp_match)
        head = src[:src.index('if m.status == "completed"')]
        self.assertIn("with_for_update()", head,
                      "status is checked without holding the row lock")
        self.assertIn("populate_existing()", head,
                      "lock must re-read the row, not trust the identity map")


# ════════════════════════════════════════════════════════════════════
# 5. Telegram ids must fit the column
# ════════════════════════════════════════════════════════════════════

class TelegramIdWidthTests(unittest.TestCase):
    def test_adsgram_telegram_id_is_bigint(self):
        from sqlalchemy import BigInteger
        from models import AdsgramReward

        col = AdsgramReward.__table__.c.telegram_id
        self.assertIsInstance(col.type, BigInteger)

    def test_every_telegram_id_column_is_bigint(self):
        from sqlalchemy import BigInteger
        import models

        from database import Base
        offenders = []
        for table in Base.metadata.tables.values():
            for col in table.columns:
                if "telegram_id" in col.name or col.name in ("chat_id", "user_chat_id"):
                    if not isinstance(col.type, BigInteger):
                        offenders.append(f"{table.name}.{col.name}")
        self.assertEqual(offenders, [],
                         f"Telegram ids exceed INTEGER range: {offenders}")


if __name__ == "__main__":
    unittest.main()

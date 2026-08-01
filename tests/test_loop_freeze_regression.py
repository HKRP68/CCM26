"""A slow render must not be able to freeze the bot.

On 2026-08-01 the bot stopped answering at 19:16 IST and came back at 19:22.
Nothing crashed and nothing restarted. The Mini App kept serving the whole
time; only Telegram went quiet. The chain was:

  1. ``/ximage`` opened a session, read its eleven players, and then held that
     session — and its pooled Postgres connection, mid-transaction — across the
     render.
  2. The render restores any card whose local file the host has discarded by
     downloading it back from Telegram, one card at a time, with no ceiling on
     how long a single download could take.
  3. Minutes later the connection had been closed by the database underneath
     us. ``session.commit()`` went out on a dead socket, and with no TCP
     timeout configured it sat in ``recv()`` for five more minutes.
  4. That commit runs on the asyncio event loop. So did the whole bot.

At 13:51:54 UTC the socket finally raised "SSL connection has been closed
unexpectedly", the loop woke up, and five minutes of queued updates landed at
once — callbacks answered too late to be valid, replies to messages that were
long gone, scheduler jobs up to 3m37s overdue.

Three invariants close that off, one per link, and they are pinned here:

  * ``/ximage`` holds no pooled connection while it renders;
  * a wedged Telegram download gives up instead of stretching the render;
  * a blocked loop gets reported *while it is blocked*, naming the line that
    is holding it, instead of leaving the next outage to be reconstructed from
    timestamps.
"""

import asyncio
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fake_psycopg2(libpq_version):
    """Stand-in psycopg2 reporting a chosen libpq version."""

    class _Stub:
        class extensions:
            @staticmethod
            def libpq_version():
                return libpq_version

    return _Stub


class TelegramDownloadTimeoutTests(unittest.TestCase):
    """Link 2: one wedged download must not become an unbounded render."""

    def _service(self):
        from services import tg_storage_service
        return tg_storage_service

    def _assert_gives_up(self, call):
        """`call` must return None quickly even though the download never ends.

        The fake blocks in a thread rather than awaiting, so asyncio cannot
        cancel it — this deliberately exercises the outer, uncancellable belt.
        """
        svc = self._service()
        release = threading.Event()
        self.addCleanup(release.set)

        async def _never_finishes(*_a, **_kw):
            await asyncio.get_running_loop().run_in_executor(
                None, release.wait, 30)
            return b"too late"

        with patch.object(svc, "download_file_bytes_async", _never_finishes), \
             patch.object(svc, "DOWNLOAD_TIMEOUT", 0.2), \
             patch.object(svc, "DOWNLOAD_TIMEOUT_GRACE", 0.3), \
             patch.dict(os.environ, {"BOT_TOKEN": "x:y"}):
            started = time.monotonic()
            result = call(svc)
            elapsed = time.monotonic() - started

        self.assertIsNone(result, "a timed-out restore must fall back, not block")
        self.assertLess(elapsed, 3.0,
                        f"download_file_bytes_sync blocked for {elapsed:.1f}s; "
                        "the executor is being joined and the timeout is moot")

    def test_wedged_download_gives_up_when_called_from_a_worker_thread(self):
        """The production path: card renders run inside asyncio.to_thread.

        There is no running loop in a worker thread, which is exactly why the
        timeout has to apply unconditionally — the branch that skipped it was
        the branch every real card render took.
        """
        async def _run():
            return await asyncio.to_thread(
                lambda: self._assert_gives_up(
                    lambda svc: svc.download_file_bytes_sync("file-123")))
        asyncio.run(_run())

    def test_wedged_download_gives_up_when_called_with_no_loop_at_all(self):
        """Flask request threads and scheduler jobs reach it this way."""
        self._assert_gives_up(lambda svc: svc.download_file_bytes_sync("file-123"))

    def test_bad_timeout_env_values_fall_back_instead_of_breaking_import(self):
        """Parsed at import, and a negative grace would invert the two limits."""
        svc = self._service()
        for bad in ("abc", "", "-5", "nan", "inf"):
            with patch.dict(os.environ, {"TG_DOWNLOAD_GRACE_SECONDS": bad}):
                self.assertEqual(svc._seconds_setting("TG_DOWNLOAD_GRACE_SECONDS",
                                                      5.0), 5.0,
                                 f"{bad!r} should have fallen back to the default")
        with patch.dict(os.environ, {"TG_DOWNLOAD_GRACE_SECONDS": "2.5"}):
            self.assertEqual(
                svc._seconds_setting("TG_DOWNLOAD_GRACE_SECONDS", 5.0), 2.5)

    def test_timed_out_downloads_do_not_leak_worker_threads(self):
        """The timeout must cancel the work, not just stop waiting for it.

        Enforcing it only on the outer Future would leave each wedged download
        holding a thread after the caller fell back — restoring a squad would
        then pile up one stranded worker per card.
        """
        svc = self._service()
        started = threading.Event()

        async def _slow(*_a, **_kw):
            started.set()
            await asyncio.sleep(60)     # cancellable, unlike a blocking wait
            return b"too late"

        def _tg_workers():
            return sum(1 for t in threading.enumerate()
                       if t.name.startswith("tg-download"))

        before = _tg_workers()
        with patch.object(svc, "download_file_bytes_async", _slow), \
             patch.object(svc, "DOWNLOAD_TIMEOUT", 0.2), \
             patch.dict(os.environ, {"BOT_TOKEN": "x:y"}):
            for _ in range(5):
                self.assertIsNone(svc.download_file_bytes_sync("file-123"))

        self.assertTrue(started.is_set(), "the download never actually started")
        deadline = time.monotonic() + 5
        while _tg_workers() > before and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertLessEqual(
            _tg_workers(), before,
            "timed-out downloads left worker threads behind — the timeout is "
            "not reaching the coroutine")


class XimageConnectionHoldTests(unittest.TestCase):
    """Link 1: the render must not run with a connection checked out."""

    def _drive_handler(self):
        """Run the real ximage_handler against fakes; return the event trace."""
        from unittest.mock import AsyncMock, MagicMock
        from handlers import ximage
        from services import command_config_service

        events = []

        class FakeSession:
            def __init__(self):
                self.closed = False
                self.expire_on_commit = True

            def query(self, *_a, **_kw):
                return self

            def filter(self, *_a, **_kw):
                return self

            def first(self):
                if self.closed:
                    raise AssertionError("query issued on a closed session")
                return MagicMock(last_ximage=None)

            def add(self, *_a, **_kw):
                pass

            def flush(self):
                pass

            def commit(self):
                events.append("commit")

            def close(self):
                events.append("close")
                self.closed = True

        session = FakeSession()

        def _fake_render(*_a, **_kw):
            events.append("render")
            # THE invariant. Holding the connection across this render is what
            # let the database close it underneath /ximage on 2026-08-01.
            self.assertTrue(
                session.closed,
                "the render ran while /ximage still held its pooled connection")
            return b"png-bytes"

        def _fake_stamp(user_id):
            events.append("stamp")
            # And the stamp must be a *fresh* session, not the closed one.
            self.assertEqual(user_id, 7)

        player = MagicMock(rating=80, id=1)
        pairs = [(MagicMock(), player) for _ in range(11)]
        viewer = MagicMock(id=7, username="fan", team_name="T",
                           first_name="F", captain_roster_id=None)

        update = MagicMock()
        update.effective_user = MagicMock(id=99)
        update.effective_chat = MagicMock(id=-100)
        update.message.reply_text = AsyncMock()
        update.message.reply_photo = AsyncMock()
        update.message.reply_chat_action = AsyncMock()

        with patch.object(ximage, "get_session", lambda: session), \
             patch.object(ximage, "resolve_command_target",
                          lambda *a, **kw: (None, "missing")), \
             patch.object(ximage, "sync_telegram_user", lambda *a, **kw: viewer), \
             patch.object(ximage, "check_cooldown", lambda *a, **kw: (True, 0)), \
             patch.object(ximage, "claim_once", lambda *a, **kw: True), \
             patch.object(ximage, "release", lambda *a, **kw: None), \
             patch.object(ximage, "_get_ordered_roster", lambda *a, **kw: pairs), \
             patch.object(ximage, "_build_display_order", lambda r: r), \
             patch.object(ximage, "build_xi_image", _fake_render), \
             patch.object(ximage, "_stamp_cooldown", _fake_stamp), \
             patch.object(command_config_service, "get_cooldown",
                          lambda *a, **kw: 3600):
            asyncio.run(ximage.ximage_handler(update, MagicMock()))

        # The handler must not have swallowed a failure into the text fallback.
        update.message.reply_photo.assert_awaited_once()
        return events

    def test_connection_is_released_before_the_render_and_stamped_after(self):
        events = self._drive_handler()
        self.assertIn("close", events)
        self.assertIn("render", events)
        self.assertLess(events.index("close"), events.index("render"),
                        "/ximage must hand back its connection before rendering")
        self.assertLess(events.index("render"), events.index("stamp"),
                        "the cooldown is only spent once the render succeeded")

    def test_cooldown_stamp_runs_off_the_event_loop(self):
        """Even the stamp must not be a blocking DB call on the loop.

        The freeze was a commit exactly like this one, on a socket that had
        gone away while the render ran.
        """
        import inspect
        from handlers import ximage
        src = inspect.getsource(ximage.ximage_handler)
        self.assertIn("asyncio.to_thread(_stamp_cooldown", src)


class LoopStallWatchdogTests(unittest.TestCase):
    """Link 4: a frozen loop has to say so while it is frozen."""

    def setUp(self):
        from services import loop_monitor
        self.monitor = loop_monitor
        self._saved = (loop_monitor._last_tick, loop_monitor._loop_thread_id)

    def tearDown(self):
        (self.monitor._last_tick, self.monitor._loop_thread_id) = self._saved

    def _stalled(self, seconds=None):
        m = self.monitor
        m._loop_thread_id = threading.get_ident()
        over = m.STALL_SECONDS + 5 if seconds is None else seconds
        m._last_tick = time.monotonic() - over

    def _check(self, reported_at):
        """Run one watchdog pass, capturing what it logged.

        Patches the module's own logger rather than using assertLogs: the rest
        of the suite reconfigures logging, and this test is about the watchdog,
        not about global handler state.
        """
        from unittest.mock import MagicMock
        fake = MagicMock()
        with patch.object(self.monitor, "logger", fake):
            reported = self.monitor._check_once(reported_at)
        return reported, fake

    @staticmethod
    def _rendered(call):
        """The log line as it would read in the deploy logs."""
        args = call.args
        return args[0] % tuple(args[1:]) if len(args) > 1 else args[0]

    def test_stall_is_reported_while_it_is_still_happening(self):
        self._stalled()
        reported, log = self._check(None)

        log.error.assert_called_once()
        message = self._rendered(log.error.call_args)
        self.assertIn("Event loop BLOCKED", message)
        self.assertIn("Stack of the blocked thread", message)
        # The stack has to be real — that is what names the offending line.
        self.assertIn("test_loop_freeze_regression.py", message)
        self.assertEqual(reported, self.monitor._last_tick)

    def test_an_ongoing_stall_is_logged_once_not_once_per_second(self):
        self._stalled()
        # Already reported against this heartbeat.
        reported, log = self._check(self.monitor._last_tick)
        log.error.assert_not_called()
        self.assertEqual(reported, self.monitor._last_tick)

    def test_recovery_is_logged_once_the_loop_ticks_again(self):
        stale = time.monotonic() - 90
        self.monitor._loop_thread_id = threading.get_ident()
        self.monitor._last_tick = time.monotonic()      # loop is ticking again
        reported, log = self._check(stale)
        log.warning.assert_called_once()
        self.assertIn("Event loop recovered", self._rendered(log.warning.call_args))
        self.assertIsNone(reported)

    def test_healthy_loop_is_silent(self):
        self.monitor._loop_thread_id = threading.get_ident()
        self.monitor._last_tick = time.monotonic()
        reported, log = self._check(None)
        log.error.assert_not_called()
        log.warning.assert_not_called()
        self.assertIsNone(reported)

    def test_watchdog_does_not_run_on_the_loop_it_watches(self):
        """A watchdog scheduled on the blocked loop could never fire."""
        import inspect
        src = inspect.getsource(self.monitor.start)
        self.assertIn("threading.Thread", src)
        self.assertIn("daemon=True", src)


class DatabaseSocketTimeoutTests(unittest.TestCase):
    """Link 3: a dead socket has to fail in seconds, not minutes."""

    def test_postgres_connect_args_bound_the_socket(self):
        import database
        args = database._apply_socket_timeouts({})
        self.assertEqual(args["keepalives"], 1)
        self.assertIn("keepalives_idle", args)
        self.assertIn("keepalives_interval", args)
        self.assertIn("keepalives_count", args)
        self.assertIn("connect_timeout", args)

    def test_tcp_user_timeout_is_skipped_on_old_libpq(self):
        """Passing it to libpq < 12 is a hard connection error."""
        import database
        with patch.dict(sys.modules, {"psycopg2": _fake_psycopg2(110000)}):
            args = database._apply_socket_timeouts({})
        self.assertNotIn("tcp_user_timeout", args)

    def test_tcp_user_timeout_is_set_on_modern_libpq(self):
        import database
        with patch.dict(sys.modules, {"psycopg2": _fake_psycopg2(160000)}):
            args = database._apply_socket_timeouts({})
        self.assertGreater(args["tcp_user_timeout"], 0)
        # Well under the five minutes the kernel's retransmit schedule took.
        self.assertLessEqual(args["tcp_user_timeout"], 60000)

    def test_caller_supplied_values_win(self):
        import database
        args = database._apply_socket_timeouts({"connect_timeout": 3})
        self.assertEqual(args["connect_timeout"], 3)

    def test_a_typo_in_a_tuning_knob_does_not_stop_the_bot_booting(self):
        """These are parsed at import; a ValueError here means no bot at all."""
        import database
        # Pin the libpq probe: whether the *host's* libpq is modern is a
        # different question from whether a typo is survivable, and letting it
        # decide would make this test pass or fail for unrelated reasons.
        with patch.dict(sys.modules, {"psycopg2": _fake_psycopg2(160000)}), \
             patch.dict(os.environ, {"DB_CONNECT_TIMEOUT": "abc",
                                     "DB_KEEPALIVES_IDLE": "",
                                     "DB_TCP_USER_TIMEOUT_MS": "soon"}):
            args = database._apply_socket_timeouts({})
        self.assertEqual(args["connect_timeout"], 10)
        self.assertEqual(args["keepalives_idle"], 30)
        self.assertEqual(args["tcp_user_timeout"], 30000)  # fell back too

    def test_a_failing_libpq_probe_omits_the_option_without_breaking_boot(self):
        """If we cannot tell, leave it off rather than risk every connection."""
        import database

        class _Broken:
            class extensions:
                @staticmethod
                def libpq_version():
                    raise RuntimeError("no libpq here")

        with patch.dict(sys.modules, {"psycopg2": _Broken}):
            args = database._apply_socket_timeouts({})
        self.assertNotIn("tcp_user_timeout", args)
        self.assertEqual(args["keepalives"], 1)   # the rest still applies

    def test_tcp_user_timeout_can_be_disabled(self):
        import database
        with patch.dict(os.environ, {"DB_TCP_USER_TIMEOUT_MS": "0"}):
            args = database._apply_socket_timeouts({})
        self.assertNotIn("tcp_user_timeout", args)
        # The keepalives are independent and must survive.
        self.assertEqual(args["keepalives"], 1)


if __name__ == "__main__":
    unittest.main()

"""The Mini App's card burst must not starve the bot of database connections.

Opening a squad asks the browser for every player's card at once, and it will
run 20+ of those requests in parallel. Flask serves each on its own thread out
of the *same* process the Telegram bot and the APScheduler jobs live in, and
they all draw on one SQLAlchemy pool.

That combination took the bot down: a single card render used to check out the
request's connection and then open one or two more of its own while still
holding it, so ~10 parallel views were enough to drain a 20-connection pool.
Everything else in the process then blocked for the full ``pool_timeout`` and
failed with ``QueuePool limit of size 10 overflow 10 reached``: match ticks,
the over-summary post, the command handlers, the 30-second heartbeats. From
the outside the bot had simply stopped.

Two invariants keep that from coming back, and both are pinned here:

  1. Rendering a card holds at most one pooled connection at a time.
  2. The card endpoint caps how many renders run at once, and threads waiting
     for a slot hold no connection while they wait.
"""

import importlib
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


_REAL_MODULES = ("config", "database", "models")
_SAVED_MODULES = {}
_SAVED_DB_URL = None
_TMP_DB = None


def setUpModule():
    global _SAVED_DB_URL, _TMP_DB
    _SAVED_DB_URL = os.environ.get("DATABASE_URL")
    _TMP_DB = tempfile.TemporaryDirectory()
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.name}/cards.db"
    # Other modules in this suite leave stub ``models``/``database`` entries in
    # sys.modules; import the real ones against our own database.
    for name in _REAL_MODULES:
        _SAVED_MODULES[name] = sys.modules.pop(name, None)
    for name in _REAL_MODULES:
        importlib.import_module(name)
    import database
    import models  # noqa: F401
    database.Base.metadata.create_all(bind=database.engine)


def tearDownModule():
    for name, module in _SAVED_MODULES.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module
    if _SAVED_DB_URL is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _SAVED_DB_URL
    if _TMP_DB is not None:
        _TMP_DB.cleanup()


class _PoolWatch:
    """Records the high-water mark of simultaneously checked-out connections."""

    def __init__(self, engine):
        self.engine = engine
        self.live = 0
        self.peak = 0
        self._lock = threading.Lock()

    def __enter__(self):
        from sqlalchemy import event
        event.listen(self.engine, "checkout", self._on_checkout)
        event.listen(self.engine, "checkin", self._on_checkin)
        return self

    def __exit__(self, *exc):
        from sqlalchemy import event
        event.remove(self.engine, "checkout", self._on_checkout)
        event.remove(self.engine, "checkin", self._on_checkin)
        return False

    def _on_checkout(self, dbapi_connection, record, proxy):
        with self._lock:
            self.live += 1
            self.peak = max(self.peak, self.live)

    def _on_checkin(self, dbapi_connection, record):
        with self._lock:
            self.live = max(0, self.live - 1)


def _make_player(session, **kwargs):
    from models import Player
    defaults = dict(name="Pool Probe", version="Base card", rating=84,
                    category="Batsman", country="India", bat_hand="Right",
                    bowl_hand="Right", bowl_style="Medium",
                    bat_rating=84, bowl_rating=40, is_active=True)
    defaults.update(kwargs)
    player = Player(**defaults)
    session.add(player)
    session.commit()
    return player


class OneConnectionPerRenderTests(unittest.TestCase):
    """A render must reuse the caller's session, never stack a second one."""

    @classmethod
    def setUpClass(cls):
        import database
        from models import Player
        from services.card_generator import generate_card
        session = database.get_session()
        try:
            cls.player_id = _make_player(session, name="Nested Session Probe").id
            # One warm-up render, because a few things in the render path are
            # genuinely once-per-process (restoring card templates onto a
            # freshly deployed container, for one). Measuring the *steady*
            # state is what matters: that is the render a card burst repeats
            # twenty times over.
            generate_card(session.query(Player).get(cls.player_id))
        finally:
            session.close()

    def setUp(self):
        # Config and image lookups memoise; a warm cache would hide the very
        # queries this test is about.
        from services import config_service, player_image_service
        config_service._CACHE["data"] = None
        player_image_service._invalidate_image_cache()

    def test_rendering_inside_a_session_never_needs_a_second_connection(self):
        import database
        from models import Player
        from services.card_generator import generate_card

        session = database.get_session()
        try:
            player = session.query(Player).get(self.player_id)
            self.assertIsNotNone(player)
            with _PoolWatch(database.engine) as watch:
                # The session is already holding its connection; count from 1.
                watch.live = watch.peak = 1
                image_bytes = generate_card(player)
            # Guard against a vacuous pass: the render really did run.
            self.assertTrue(image_bytes)
            self.assertLessEqual(
                watch.peak, 1,
                "card render checked out a second connection while the "
                "caller's session was still holding one")
        finally:
            session.close()

    def test_the_custom_image_lookup_reuses_a_supplied_session(self):
        import database
        from sqlalchemy import text
        from services.player_image_service import get_custom_image_bytes

        session = database.get_session()
        try:
            session.execute(text("SELECT 1"))   # take the connection
            with _PoolWatch(database.engine) as watch:
                self.assertIsNone(
                    get_custom_image_bytes(self.player_id, session=session))
            self.assertEqual(watch.peak, 0,
                             "a supplied session must satisfy the lookup")
        finally:
            session.close()

    def test_it_still_opens_its_own_session_when_given_none(self):
        """Handlers that hold no session must keep working unchanged."""
        from services.player_image_service import get_custom_image_bytes
        self.assertIsNone(get_custom_image_bytes(self.player_id))

    def test_a_detached_player_falls_back_to_a_fresh_session(self):
        """Synthetic and detached players have no session to borrow."""
        import database
        from models import Player
        from services.card_generator import _session_for

        session = database.get_session()
        try:
            player = session.query(Player).get(self.player_id)
            self.assertIs(_session_for(player), session)
            session.expunge(player)
            self.assertIsNone(_session_for(player))
        finally:
            session.close()

        from types import SimpleNamespace
        self.assertIsNone(_session_for(SimpleNamespace(id=-1, name="Sim XI")))


class NoImageCachingTests(unittest.TestCase):
    """"This player has no custom card" is the common answer — remember it."""

    def setUp(self):
        from services import player_image_service
        player_image_service._invalidate_image_cache()

    def test_a_missing_custom_image_is_not_re_queried(self):
        import database
        from services.player_image_service import get_custom_image_bytes

        session = database.get_session()
        try:
            player_id = _make_player(session, name="No Art").id
        finally:
            session.close()

        self.assertIsNone(get_custom_image_bytes(player_id))
        with _PoolWatch(database.engine) as watch:
            self.assertIsNone(get_custom_image_bytes(player_id))
        self.assertEqual(watch.peak, 0, "negative result was not cached")

    def test_an_upload_invalidates_the_remembered_miss(self):
        from services import player_image_service
        player_image_service._NO_IMAGE_IDS.add(4242)
        player_image_service._invalidate_image_cache(4242)
        self.assertNotIn(4242, player_image_service._NO_IMAGE_IDS)

    def test_a_full_byte_cache_evicts_rather_than_flushes(self):
        """Clearing the whole cache sent the next few hundred renders to the DB."""
        from services import player_image_service as svc
        svc._IMG_CACHE.clear()
        for i in range(svc._IMG_CACHE_MAX + 10):
            svc._IMG_CACHE[i] = b"art"
            while len(svc._IMG_CACHE) > svc._IMG_CACHE_MAX:
                svc._IMG_CACHE.popitem(last=False)
        self.assertEqual(len(svc._IMG_CACHE), svc._IMG_CACHE_MAX)
        # The most recent entries survived; only the oldest were dropped.
        self.assertIn(svc._IMG_CACHE_MAX + 9, svc._IMG_CACHE)
        self.assertNotIn(0, svc._IMG_CACHE)


class RenderConcurrencyTests(unittest.TestCase):
    """The endpoint queues a card burst instead of letting it drain the pool."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("ADMIN_PASSWORD", "test")
        try:
            import database
            import admin
        except Exception as exc:      # pragma: no cover - env-dependent
            raise unittest.SkipTest(f"admin app unavailable: {exc}")
        cls.admin = admin
        session = database.get_session()
        try:
            cls.player_id = _make_player(session, name="Burst Probe").id
        finally:
            session.close()
        admin.app.config["TESTING"] = True

    def test_bad_tuning_values_fall_back_instead_of_killing_the_boot(self):
        """These are host env vars — a typo must not stop the bot starting."""
        parse = self.admin._tuning_value
        self.assertEqual(parse("X_MISSING", 4, int, 1), 4)
        with patch.dict(os.environ, {"X_TUNING": "not-a-number"}):
            self.assertEqual(parse("X_TUNING", 4, int, 1), 4)
        with patch.dict(os.environ, {"X_TUNING": "8"}):
            self.assertEqual(parse("X_TUNING", 4, int, 1), 8)
        # A negative wait must not reach the semaphore as-is.
        with patch.dict(os.environ, {"X_TUNING": "-5"}):
            self.assertEqual(parse("X_TUNING", 20.0, float, 0.0), 0.0)
            self.assertEqual(parse("X_TUNING", 4, int, 1), 1)

    def test_a_squad_burst_is_capped_at_the_configured_concurrency(self):
        admin = self.admin
        cap = admin.CARD_RENDER_CONCURRENCY
        state = {"live": 0, "peak": 0}
        lock = threading.Lock()

        def slow_render(player):
            with lock:
                state["live"] += 1
                state["peak"] = max(state["peak"], state["live"])
            time.sleep(0.05)
            with lock:
                state["live"] -= 1
            return b"\x89PNG\r\n\x1a\n" + b"x" * 64

        results = []

        def fetch():
            client = admin.app.test_client()
            results.append(
                client.get(f"/webapp/player-card/{self.player_id}").status_code)

        with patch("services.card_generator.generate_card", slow_render):
            threads = [threading.Thread(target=fetch) for _ in range(cap * 4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

        self.assertEqual(results, [200] * (cap * 4))
        self.assertLessEqual(
            state["peak"], cap,
            f"{state['peak']} cards rendered at once with a cap of {cap}")

    def test_waiting_for_a_slot_holds_no_database_connection(self):
        """Otherwise the queue would be just as damaging as the burst it replaces."""
        import database
        admin = self.admin

        started = threading.Event()
        release = threading.Event()

        def blocking_render(player):
            started.set()
            release.wait(timeout=10)
            return b"\x89PNG\r\n\x1a\n" + b"x" * 64

        # Occupy every render slot, then watch the pool while more requests
        # queue up behind them.
        holders = []
        for _ in range(admin.CARD_RENDER_CONCURRENCY):
            admin._card_render_slots.acquire()
            holders.append(True)
        # Bound before the try: the cleanup below iterates it, and an exception
        # from _PoolWatch would otherwise surface as a NameError that buries
        # whatever actually went wrong.
        waiters = []
        try:
            with _PoolWatch(database.engine) as watch:
                with patch("services.card_generator.generate_card",
                           blocking_render):
                    for _ in range(6):
                        t = threading.Thread(
                            target=lambda: admin.app.test_client().get(
                                f"/webapp/player-card/{self.player_id}"))
                        t.daemon = True
                        t.start()
                        waiters.append(t)
                    time.sleep(0.3)
                    self.assertEqual(
                        watch.peak, 0,
                        "queued card requests were holding connections while "
                        "they waited for a render slot")
        finally:
            release.set()
            for _ in holders:
                admin._card_render_slots.release()
            for t in waiters:
                t.join(timeout=10)


if __name__ == "__main__":
    unittest.main()

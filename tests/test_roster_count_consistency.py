"""Regression coverage for roster-size consistency across surfaces.

The roster size shown in chat (/myroster), the Mini App Home, and the Squad
screen used to disagree because Home read the denormalized ``User.roster_count``
cache while the other surfaces counted live ``UserRoster`` rows. When the cache
drifted the three numbers diverged.

These tests pin the single source of truth (``get_roster_count``) and the
self-healing reconcile (``reconcile_roster_count``) that keeps the cache in sync.
"""

import os
import sys
import tempfile
import unittest


class RosterCountConsistencyTest(unittest.TestCase):
    _MODULE_NAMES = ("database", "models", "config", "services.roster_service")

    @classmethod
    def setUpClass(cls):
        # Drop any stub database/models/config other tests left in sys.modules so
        # we import the real SQLAlchemy-backed modules regardless of ordering.
        cls._prev_database_url = os.environ.get("DATABASE_URL")
        cls._saved_modules = {name: sys.modules.get(name) for name in cls._MODULE_NAMES}
        for name in cls._MODULE_NAMES:
            sys.modules.pop(name, None)

        cls._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._tmp.close()
        os.environ["DATABASE_URL"] = f"sqlite:///{cls._tmp.name}"

        from database import Base, engine
        import models

        cls.engine = engine
        cls.models = models
        Base.metadata.create_all(bind=engine)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.engine.dispose()
        except Exception:
            pass
        if cls._prev_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = cls._prev_database_url
        for name, module in cls._saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        try:
            os.unlink(cls._tmp.name)
        except OSError:
            pass

    def _make_user(self, session, telegram_id, cached_count):
        from models import User
        user = User(telegram_id=telegram_id, first_name="T", roster_count=cached_count)
        session.add(user)
        session.flush()
        return user

    def _add_players(self, session, user, n, start_pid=1):
        from models import Player, UserRoster
        for i in range(n):
            pid = start_pid + i
            # Reuse an existing player row if one is already seeded for this id.
            player = session.query(Player).get(pid)
            if player is None:
                player = Player(id=pid, name=f"P{pid}", rating=75,
                                category="Batsman", country="India",
                                bat_hand="Right", bowl_hand="Right",
                                bowl_style="Medium")
                session.add(player)
                session.flush()
            session.add(UserRoster(user_id=user.id, player_id=player.id,
                                   order_position=i + 1))
        session.flush()

    def test_get_roster_count_uses_live_rows_not_cache(self):
        from database import get_session
        from services.roster_service import get_roster_count
        session = get_session()
        try:
            # Cache claims 25 but only 3 rows actually exist.
            user = self._make_user(session, 1001, cached_count=25)
            self._add_players(session, user, 3, start_pid=1)
            self.assertEqual(get_roster_count(session, user.id), 3)
        finally:
            session.rollback()
            session.close()

    def test_reconcile_heals_inflated_cache(self):
        from database import get_session
        from services.roster_service import reconcile_roster_count, get_roster_count
        session = get_session()
        try:
            user = self._make_user(session, 1002, cached_count=25)
            self._add_players(session, user, 4, start_pid=100)
            corrected = reconcile_roster_count(session, user)
            self.assertEqual(corrected, 4)
            self.assertEqual(user.roster_count, 4)
            self.assertEqual(user.roster_count, get_roster_count(session, user.id))
        finally:
            session.rollback()
            session.close()

    def test_reconcile_heals_deflated_cache(self):
        from database import get_session
        from services.roster_service import reconcile_roster_count
        session = get_session()
        try:
            # Cache under-counts (0) while rows exist — the "shows fewer" variant.
            user = self._make_user(session, 1003, cached_count=0)
            self._add_players(session, user, 6, start_pid=200)
            self.assertEqual(reconcile_roster_count(session, user), 6)
            self.assertEqual(user.roster_count, 6)
        finally:
            session.rollback()
            session.close()

    def test_reconcile_noop_when_already_correct(self):
        from database import get_session
        from services.roster_service import reconcile_roster_count
        session = get_session()
        try:
            user = self._make_user(session, 1004, cached_count=2)
            self._add_players(session, user, 2, start_pid=300)
            self.assertEqual(reconcile_roster_count(session, user), 2)
            self.assertEqual(user.roster_count, 2)
        finally:
            session.rollback()
            session.close()


if __name__ == "__main__":
    unittest.main()

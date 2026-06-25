"""Tests for the remember-my-XI persistence service (services/xi_memory_service)."""

import os
import sys
import tempfile
import unittest


class XiMemoryServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Other test modules stub database/models/config in the shared sys.modules
        # (no conftest isolates them); drop any such stubs so we import the real
        # SQLAlchemy-backed modules regardless of test ordering under discover.
        for name in ("database", "models", "config", "services.xi_memory_service"):
            sys.modules.pop(name, None)

        # Point the whole stack at an isolated temp SQLite file before importing
        # any project module that reads DATABASE_URL at import time.
        cls._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._tmp.close()
        os.environ["DATABASE_URL"] = f"sqlite:///{cls._tmp.name}"

        from database import Base, engine
        import models  # noqa: F401 — registers every Base subclass on the metadata

        cls.engine = engine
        # Brand-new table is created by create_all with no manual migration.
        Base.metadata.create_all(bind=engine, tables=[models.UserTeamLastXI.__table__])

        from services.xi_memory_service import save_last_xi, load_last_xi
        cls.save_last_xi = staticmethod(save_last_xi)
        cls.load_last_xi = staticmethod(load_last_xi)

    @classmethod
    def tearDownClass(cls):
        try:
            os.unlink(cls._tmp.name)
        except OSError:
            pass

    def _count_rows(self):
        from database import get_session
        from models import UserTeamLastXI
        session = get_session()
        try:
            return session.query(UserTeamLastXI).count()
        finally:
            session.close()

    def test_round_trip_preserves_order(self):
        ids = [5, 1, 9, 3, 7, 2, 8, 4, 11, 6, 10]
        self.assertTrue(self.save_last_xi(101, 1, ids))
        self.assertEqual(self.load_last_xi(101, 1), ids)

    def test_resave_updates_in_place(self):
        self.save_last_xi(202, 2, [1, 2, 3])
        before = self._count_rows()
        self.save_last_xi(202, 2, [4, 5, 6])
        after = self._count_rows()
        self.assertEqual(before, after)  # upsert, not a second row
        self.assertEqual(self.load_last_xi(202, 2), [4, 5, 6])

    def test_unknown_returns_empty(self):
        self.assertEqual(self.load_last_xi(999, 999), [])

    def test_isolation_per_team_and_per_user(self):
        # Same user, two teams -> two independent saved XIs (the multi-team case).
        self.save_last_xi(303, 10, [1, 2, 3])   # e.g. RCB
        self.save_last_xi(303, 20, [4, 5, 6])   # e.g. CSK
        self.assertEqual(self.load_last_xi(303, 10), [1, 2, 3])
        self.assertEqual(self.load_last_xi(303, 20), [4, 5, 6])
        # A different user's XI for the same team is separate.
        self.save_last_xi(404, 10, [7, 8, 9])
        self.assertEqual(self.load_last_xi(404, 10), [7, 8, 9])
        self.assertEqual(self.load_last_xi(303, 10), [1, 2, 3])

    def test_empty_payload_is_noop(self):
        self.assertFalse(self.save_last_xi(505, 5, []))
        self.assertEqual(self.load_last_xi(505, 5), [])


if __name__ == "__main__":
    unittest.main()

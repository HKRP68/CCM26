"""Access changes persist and official match identity cannot come from a client."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from database import Base
from models import AdminLog, Match, TournamentMatch, User
from services import tournament_only_service as service


class TournamentOnlyServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.session = self.sessions()
        self.user = User(telegram_id=888001, username="captain")
        self.session.add(self.user)
        self.session.commit()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def block(self, **kwargs):
        return service.set_restriction(self.session, self.user, enabled=True,
                                       admin_telegram_id=99123, **kwargs)

    def test_default_open_and_block_persists_across_sessions(self):
        self.assertFalse(service.is_restricted(self.user))
        self.block(reason="Play fixture 7")
        self.session.commit()
        with self.sessions() as reader:
            user = reader.get(User, self.user.id)
            self.assertTrue(service.is_restricted(user.id, session=reader))
            self.assertEqual(user.tournament_only_by, 99123)
            self.assertIsNotNone(user.tournament_only_at)
            self.assertIn("Play fixture 7", service.restriction_message(user))
            audit = reader.query(AdminLog).one()
            self.assertEqual(audit.target_id, user.id)
            self.assertIn("99123", audit.detail)

    def test_unblock_clears_current_reason_but_keeps_audit(self):
        self.block(reason="Pending matches")
        self.session.commit()
        service.set_restriction(self.session, self.user, enabled=False,
                                admin_telegram_id=99123)
        self.session.commit()
        self.assertFalse(service.is_restricted(self.user))
        self.assertIsNone(self.user.tournament_only_reason)
        self.assertIsNone(self.user.tournament_only_at)
        self.assertIsNone(self.user.tournament_only_by)
        self.assertEqual(self.session.query(AdminLog).count(), 2)

    def test_rollback_reverts_restriction_and_audit_together(self):
        self.block()
        self.session.rollback()
        self.session.refresh(self.user)
        self.assertFalse(service.is_restricted(self.user))
        self.assertEqual(self.session.query(AdminLog).count(), 0)

    def test_long_reason_rejected_without_changing_access(self):
        with self.assertRaises(ValueError):
            self.block(reason="x" * 501)
        self.assertFalse(service.is_restricted(self.user))

    def test_official_identity_uses_persisted_match_or_fixture(self):
        casual = Match(user1_id=self.user.id, user2_id=self.user.id)
        official = Match(user1_id=self.user.id, user2_id=self.user.id,
                         tournament_id=123)
        self.session.add_all([casual, official])
        self.session.flush()
        self.assertFalse(service.is_tournament_match(self.session, casual))
        self.assertTrue(service.is_tournament_match(self.session, official.id))
        self.session.add(TournamentMatch(tournament_id=123, match_id=casual.id))
        self.session.flush()
        self.assertTrue(service.is_tournament_match(self.session, casual))

    def test_private_series_is_not_an_official_tournament(self):
        friendly = SimpleNamespace(id=None, tournament_id=None,
                                   cl_tour_match_id=12, tour_match_id=13)
        self.assertFalse(service.is_tournament_match(self.session, friendly))
        self.assertFalse(service.is_tournament_match(self.session, None))

    def test_existing_database_upgrade_defaults_to_normal_access(self):
        import database
        user_id = self.user.id
        self.session.close()
        with self.engine.begin() as connection:
            for column in ("tournament_only", "tournament_only_reason",
                           "tournament_only_at", "tournament_only_by",
                           "tournament_only_fixture_ids_json", "tournament_only_starts_at"):
                connection.execute(text(f"ALTER TABLE users DROP COLUMN {column}"))
        with patch.object(database, "engine", self.engine), \
                patch.object(database, "SessionLocal", self.sessions), \
                patch.object(database, "_is_postgres", False):
            database._migrate_add_columns()
            database._migrate_add_columns()  # repeat boot must be safe
        with self.sessions() as reader:
            user = reader.get(User, user_id)
            self.assertFalse(service.is_restricted(user))
            self.assertIsNone(user.tournament_only_reason)
            self.assertEqual(user.username, "captain")


if __name__ == "__main__":
    unittest.main()

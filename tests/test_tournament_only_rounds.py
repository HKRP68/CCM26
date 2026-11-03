"""A round lock has a fixed completion target and a warning deadline."""
import json
import unittest
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from database import Base
from models import User, Tournament, TournamentTeam, TournamentMatch, AdminLog
from services import tournament_only_service as service


class RoundRestrictionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.user = User(telegram_id=12345)
        self.tour = Tournament(name="Cup", status="active")
        self.db.add_all([self.user, self.tour])
        self.db.flush()
        self.teams = [TournamentTeam(tournament_id=self.tour.id, name=str(i),
                                    user_tg_id=12345 if i == 0 else 20000+i) for i in range(4)]
        self.db.add_all(self.teams)
        self.db.flush()
        self.own = self.fixture(0, 1, 1)
        self.others = self.fixture(2, 3, 1)
        self.future = self.fixture(0, 2, 2)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def fixture(self, a, b, rnd):
        fx = TournamentMatch(tournament_id=self.tour.id, team1_id=self.teams[a].id,
                            team2_id=self.teams[b].id, round_no=rnd, match_no=rnd,
                            stage="league", status="scheduled")
        self.db.add(fx)
        self.db.flush()
        return fx

    def lock(self, hours=0):
        service.schedule_restriction(self.db, self.user, admin_telegram_id=999, grace_hours=hours)
        self.db.commit()

    def test_warning_grace_and_deadline(self):
        self.lock(24)
        self.assertTrue(self.user.tournament_only)
        self.assertFalse(service.is_restricted(self.user))
        self.assertIn("warning", service.restriction_message(self.user, session=self.db))
        self.user.tournament_only_starts_at = datetime.utcnow() - timedelta(seconds=1)
        self.assertTrue(service.is_restricted(self.user))

    def test_own_round_only_not_other_teams_or_next_round(self):
        self.lock()
        self.assertEqual(json.loads(self.user.tournament_only_fixture_ids_json), [self.own.id])
        self.own.status = "completed"
        service.refresh_restrictions(self.db)
        self.db.commit()
        self.assertFalse(self.user.tournament_only)
        self.assertEqual(self.future.status, "scheduled")
        self.assertEqual(self.others.status, "scheduled")
        self.assertEqual(self.db.query(AdminLog).filter_by(action="tournament_only_auto_unblock").count(), 1)

    def test_all_released_batch_fixtures_required_and_target_does_not_move(self):
        self.tour.fixture_round_window = 2
        self.lock()
        self.own.status = "completed"
        service.refresh_restrictions(self.db)
        self.assertTrue(service.is_restricted(self.user))
        extra = self.fixture(0, 3, 3)
        self.future.status = "completed"
        service.refresh_restrictions(self.db)
        self.assertFalse(self.user.tournament_only)
        self.assertEqual(extra.status, "scheduled")

    def test_starting_is_not_completion_and_rewarning_cannot_extend_grace(self):
        self.lock()
        self.own.status = "live"
        self.assertTrue(service.is_restricted(self.user))
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.lock(24)

    def test_no_fixture_no_lock(self):
        self.user.telegram_id = 999999
        with self.assertRaisesRegex(ValueError, "no unfinished"):
            self.lock()
        self.assertFalse(self.user.tournament_only)

    def test_cancelled_tournament_and_removed_fixture_release(self):
        self.lock()
        self.tour.status = "cancelled"
        service.refresh_restrictions(self.db)
        self.assertFalse(self.user.tournament_only)

    def test_co_owner_and_all_at_once_only_current_round(self):
        self.teams[0].user_tg_id = None
        self.teams[0].co_owner_ids_json = "[12345]"
        self.tour.fixtures_all_at_once = True
        self.lock()
        self.assertEqual(json.loads(self.user.tournament_only_fixture_ids_json), [self.own.id])

    def test_completion_and_unlock_rollback_together(self):
        self.lock()
        self.own.status = "completed"
        service.refresh_restrictions(self.db)
        self.db.rollback()
        self.assertTrue(self.user.tournament_only)
        self.assertEqual(self.own.status, "scheduled")

    def test_real_result_recording_unblocks_without_a_poll(self):
        from services import tournament_service
        self.lock()
        tournament_service.record_manual_result(
            self.db, self.own.id, inn1_runs=100, inn1_wickets=5, inn1_overs=20,
            inn2_runs=90, inn2_wickets=10, inn2_overs=19, winner_slot="1")
        self.db.commit()
        self.assertFalse(self.user.tournament_only)

    def test_legacy_lock_upgraded_to_current_round(self):
        service.set_restriction(self.db, self.user, enabled=True, admin_telegram_id=999)
        service.refresh_restrictions(self.db, upgrade_legacy=True)
        self.assertEqual(json.loads(self.user.tournament_only_fixture_ids_json), [self.own.id])
        self.own.status = "completed"
        service.refresh_restrictions(self.db)
        self.assertFalse(self.user.tournament_only)

    def test_bye_or_no_opponent_does_not_create_an_impossible_lock(self):
        self.own.team2_id = None
        with self.assertRaisesRegex(ValueError, "no unfinished"):
            self.lock()

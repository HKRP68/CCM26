"""Prizes, honours and the ceremony when a final is decided."""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _tour_fixture as F  # noqa: E402


def setUpModule():
    F.setup_module_db()


def tearDownModule():
    F.teardown_module_db()


class AwardsTests(F.FourTeamCase):
    def setUp(self):
        super().setUp()
        from services import tournament_awards, tournament_watch
        self.TA, self.tw = tournament_awards, tournament_watch
        self.TA.set_prize(self.tour, "champion", 5000, 50)
        self.TA.set_prize(self.tour, "runnerup", 2000, 0)
        self.TA.set_prize(self.tour, "mvp", 700, 7)
        self.session.commit()
        self.now = datetime(2026, 10, 5)
        self.tw.tick(self.session, self.tour, now=self.now)

    def _play_out(self, final_outcome="1", before_knockouts=None):
        self.finish_league()
        if before_knockouts:
            before_knockouts()
        for fx in self.fixtures(stage="semifinal"):
            self.ts.simulate_fixture(self.session, fx.id, "1")
        final = self.fixtures(stage="final")[0]
        self.ts.simulate_fixture(self.session, final.id, final_outcome)
        self.session.commit()
        return final

    def _make_legacy_unpaid(self, honour, owner):
        """Model an old unpaid honour, before per-recipient ledgers existed."""
        from models import TournamentPrizePayout
        owner.total_coins -= honour.prize_coins
        owner.total_gems -= honour.prize_gems
        self.session.query(TournamentPrizePayout).filter_by(honour_id=honour.id).delete(
            synchronize_session=False)
        honour.prize_coins = honour.prize_gems = 0
        honour.prize_snapshot_coins = honour.prize_snapshot_gems = None

    def test_prize_settings(self):
        table = self.TA.prizes(self.tour)
        self.assertEqual(table["champion"], {"coins": 5000, "gems": 50})
        self.assertNotIn("orange_cap", table)
        self.TA.set_prize(self.tour, "champion", 0, 0)
        self.assertNotIn("champion", self.TA.prizes(self.tour))
        for bad in (("nobody", 1, 1), ("mvp", -5, 0), ("mvp", 10**9, 0)):
            with self.assertRaises(ValueError):
                self.TA.set_prize(self.tour, *bad)

    def test_the_final_pays_the_champion_and_runner_up_once(self):
        from models import TournamentHonour, TournamentTeam
        final = self._play_out("2")
        champ = self.session.get(TournamentTeam, final.team2_id)
        runner = self.session.get(TournamentTeam, final.team1_id)
        owner_c = [o for n, o in self.owners.items() if n == champ.name][0]
        owner_r = [o for n, o in self.owners.items() if n == runner.name][0]
        self.session.refresh(owner_c)
        self.session.refresh(owner_r)
        self.assertEqual(owner_c.total_coins, 5000)
        self.assertEqual(owner_c.total_gems, 50)
        self.assertEqual(owner_r.total_coins, 2000)
        awards = {h.award for h in self.session.query(TournamentHonour)
                  .filter_by(tournament_id=self.tour.id)}
        self.assertTrue({"champion", "runner_up"} <= awards)
        # A second call never pays again.
        self.assertEqual(self.TA.finalize(self.session, self.tour), [])
        self.session.refresh(owner_c)
        self.assertEqual(owner_c.total_coins, 5000)

    def test_caps_and_mvp_come_from_the_scorecards(self):
        from models import TournamentPlayerStats, User
        star = User(telegram_id=F.next_tg(), username="star")
        self.session.add(star)
        self.session.flush()
        line = {"name": "Run Machine", "team_name": "Alpha", "user_id": star.id,
                "player_id": None, "batted": True, "bat_runs": 120,
                "bat_balls": 60, "bat_fours": 10, "bat_sixes": 6,
                "bowled": True, "bowl_wickets": 4, "bowl_runs": 20, "bowl_balls": 24}

        def add_card():
            # Simulated results carry no scorecard, so the star's card goes on a
            # finished league fixture before the knockouts.
            self.fixtures(1)[0].scorecard_json = json.dumps([line])
            self.session.flush()
        self.session.add(TournamentPlayerStats(
            tournament_id=self.tour.id, user_id=star.id, name="Run Machine",
            team_name="Alpha", matches=1, bat_innings=1, bat_runs=120, bat_balls=60,
            bat_fours=10, bat_sixes=6, bowl_innings=1, bowl_wickets=4,
            bowl_runs=20, bowl_balls=24))
        self.session.flush()
        self._play_out(before_knockouts=add_card)
        rows = {w[0]: w for w in self.TA.winners(self.session, self.tour)}
        self.assertEqual(rows["orange_cap"][2], "Run Machine")
        self.assertEqual(rows["purple_cap"][2], "Run Machine")
        self.assertEqual(rows["mvp"][2], "Run Machine")
        self.session.refresh(star)
        self.assertEqual(star.total_coins, 0)
        from services import tournament_payouts
        mvp_payments = [p for p in tournament_payouts.payout_rows(self.session, self.tour.id)
                        if p.award == "mvp"]
        self.assertEqual([(p.recipient_tg_id, p.coins) for p in mvp_payments],
                         [(self.owners["Alpha"].telegram_id, 700)])

    def test_unregistered_owner_can_receive_unpaid_prize_after_the_final(self):
        from models import TournamentHonour, TournamentTeam, User
        for team in self.teams.values():
            team.owner_tg_id = F.next_tg()
        self.session.commit()

        final = self._play_out()
        champ = self.session.get(TournamentTeam, final.winner_team_id)
        honour = self.session.query(TournamentHonour).filter_by(
            tournament_id=self.tour.id, award="champion").one()
        self.assertIsNotNone(self.tour.awards_given_at)
        self.assertIsNone(honour.user_id)
        self.assertEqual((honour.prize_coins, honour.prize_gems), (0, 0))

        owner = User(telegram_id=champ.owner_tg_id, username="late_owner",
                     total_coins=123, total_gems=4)
        self.session.add(owner)
        self.session.commit()

        self.assertEqual(self.TA.finalize(self.session, self.tour), [])
        self.session.commit()
        self.session.refresh(owner)
        self.session.refresh(honour)
        self.assertEqual((owner.total_coins, owner.total_gems), (5123, 54))
        self.assertEqual(honour.user_id, owner.id)
        self.assertEqual((honour.prize_coins, honour.prize_gems), (5000, 50))

        self.assertEqual(self.TA.finalize(self.session, self.tour), [])
        self.TA.ensure_ceremony_honours(self.session, self.tour)
        self.session.commit()
        self.session.refresh(owner)
        self.assertEqual((owner.total_coins, owner.total_gems), (5123, 54))

    def test_ceremony_repairs_named_announced_unpaid_honours_including_champion(self):
        from models import TournamentHonour, TournamentTeam, User
        self.TA.set_prize(self.tour, "captain", 120, 3)
        self.session.commit()
        final = self._play_out()
        champ = self.session.get(TournamentTeam, final.winner_team_id)
        owner = self.session.query(User).filter_by(telegram_id=champ.owner_tg_id).one()
        rows = self.session.query(TournamentHonour).filter(
            TournamentHonour.tournament_id == self.tour.id,
            TournamentHonour.award.in_(("champion", "winning_captain"))).all()
        self.assertEqual(len(rows), 2)
        for honour in rows:
            self.assertTrue(honour.team_name or honour.player_name)
            self._make_legacy_unpaid(honour, owner)
            honour.announced_at = self.now
        self.session.commit()
        self.assertEqual((owner.total_coins, owner.total_gems), (0, 0))

        self.TA.ensure_ceremony_honours(self.session, self.tour)
        self.session.commit()
        self.session.refresh(owner)
        self.assertEqual((owner.total_coins, owner.total_gems), (5120, 53))
        for honour in rows:
            self.session.refresh(honour)
            self.assertEqual(honour.announced_at, self.now)
            self.assertGreater(honour.prize_coins, 0)

        self.TA.ensure_ceremony_honours(self.session, self.tour)
        self.assertEqual(self.TA.finalize(self.session, self.tour), [])
        self.session.commit()
        self.session.refresh(owner)
        self.assertEqual((owner.total_coins, owner.total_gems), (5120, 53))

    def test_paid_honours_preserve_winners_and_amounts_when_retried(self):
        from models import TournamentTeam, User
        final = self._play_out()
        honours = self.TA.honours_for(self.session, self.tour.id)
        snapshot = [(h.id, h.award, h.team_name, h.player_name, h.user_id,
                     h.prize_coins, h.prize_gems) for h in honours]
        balances = {u.id: (u.total_coins, u.total_gems) for u in self.owners.values()}
        champ = self.session.get(TournamentTeam, final.winner_team_id)
        changed_owner = User(telegram_id=F.next_tg(), username="new_owner")
        self.session.add(changed_owner)
        self.session.flush()
        champ.owner_tg_id = changed_owner.telegram_id
        final.winner_team_id = (final.team2_id if final.winner_team_id == final.team1_id
                                else final.team1_id)
        self.TA.set_prize(self.tour, "champion", 9000, 90)
        self.TA.set_prize(self.tour, "runnerup", 4000, 40)
        self.session.commit()

        self.assertEqual(self.TA.finalize(self.session, self.tour), [])
        self.TA.ensure_ceremony_honours(self.session, self.tour)
        self.session.commit()
        after = self.TA.honours_for(self.session, self.tour.id)
        self.assertEqual([(h.id, h.award, h.team_name, h.player_name, h.user_id,
                           h.prize_coins, h.prize_gems) for h in after], snapshot)
        for uid, balance in balances.items():
            user = self.session.get(User, uid)
            self.session.refresh(user)
            self.assertEqual((user.total_coins, user.total_gems), balance)
        self.session.refresh(changed_owner)
        self.assertEqual((changed_owner.total_coins, changed_owner.total_gems), (0, 0))

    def test_prize_activity_records_the_actual_coin_and_gem_changes(self):
        from models import ActivityLog, TournamentTeam, User
        final = self._play_out()
        champ = self.session.get(TournamentTeam, final.winner_team_id)
        owner = self.session.query(User).filter_by(telegram_id=champ.owner_tg_id).one()
        entries = self.session.query(ActivityLog).filter_by(
            user_id=owner.id, action="tournament_prize").all()
        self.assertEqual(sum(row.coins_change for row in entries), 5000)
        self.assertEqual(sum(row.gems_change for row in entries), 50)

    def test_stale_tournament_in_another_session_cannot_repeat_payments(self):
        from database import get_session
        from models import Tournament, TournamentHonour, TournamentTeam, User
        other = get_session()
        try:
            stale = other.get(Tournament, self.tour.id)
            self.assertIsNone(stale.awards_given_at)
            final = self._play_out()
            champ = self.session.get(TournamentTeam, final.winner_team_id)
            owner = self.session.query(User).filter_by(telegram_id=champ.owner_tg_id).one()
            count = self.session.query(TournamentHonour).filter_by(
                tournament_id=self.tour.id).count()

            self.assertEqual(self.TA.finalize(other, stale), [])
            other.commit()
            self.session.refresh(owner)
            self.assertEqual((owner.total_coins, owner.total_gems), (5000, 50))
            self.assertEqual(self.session.query(TournamentHonour).filter_by(
                tournament_id=self.tour.id).count(), count)
        finally:
            other.close()

    def test_stale_unpaid_honour_in_another_session_cannot_repeat_payments(self):
        from database import get_session
        from models import Tournament, TournamentHonour, User
        self._play_out()
        honour = self.session.query(TournamentHonour).filter_by(
            tournament_id=self.tour.id, award="champion").one()
        owner = self.session.get(User, honour.user_id)
        self._make_legacy_unpaid(honour, owner)
        self.session.commit()
        other = get_session()
        try:
            stale_tour = other.get(Tournament, self.tour.id)
            stale_honour = other.get(TournamentHonour, honour.id)
            self.assertEqual((stale_honour.prize_coins, stale_honour.prize_gems), (0, 0))
            self.TA.finalize(self.session, self.tour)
            self.session.commit()

            self.assertEqual(self.TA.finalize(other, stale_tour), [])
            other.commit()
            self.session.refresh(owner)
            self.assertEqual((owner.total_coins, owner.total_gems), (5000, 50))
        finally:
            other.close()

    def test_concurrent_sqlite_retries_credit_one_prize_and_receipt(self):
        from database import get_session
        from models import ActivityLog, Tournament, TournamentHonour, User
        self._play_out()
        honour = self.session.query(TournamentHonour).filter_by(
            tournament_id=self.tour.id, award="champion").one()
        owner = self.session.get(User, honour.user_id)
        self._make_legacy_unpaid(honour, owner)
        self.session.commit()
        tid, hid, uid = self.tour.id, honour.id, owner.id
        updated_at = self.tour.updated_at
        previous_logs = {row.id for row in self.session.query(ActivityLog).filter_by(
            user_id=uid, action="tournament_prize")}
        honour_count = self.session.query(TournamentHonour).filter_by(tournament_id=tid).count()
        ready = Barrier(2)

        def retry():
            session = get_session()
            try:
                tour = session.get(Tournament, tid)
                existing = session.get(TournamentHonour, hid)
                before = (existing.prize_coins, existing.prize_gems)
                # Both workers hold the same unpaid snapshot before either
                # starts the payment transaction. No timing sleeps are needed.
                ready.wait(timeout=10)
                result = self.TA.finalize(session, tour)
                session.commit()
                return before, result
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(retry) for _ in range(2)]
            results = [future.result(timeout=20) for future in futures]

        self.assertEqual(results, [((0, 0), []), ((0, 0), [])])
        self.session.refresh(owner)
        self.session.refresh(honour)
        self.session.refresh(self.tour)
        self.assertEqual((owner.total_coins, owner.total_gems), (5000, 50))
        self.assertEqual((honour.prize_coins, honour.prize_gems), (5000, 50))
        self.assertEqual(self.tour.updated_at, updated_at)
        self.assertEqual(self.session.query(TournamentHonour).filter_by(
            tournament_id=tid).count(), honour_count)
        new_logs = [row for row in self.session.query(ActivityLog).filter_by(
            user_id=uid, action="tournament_prize") if row.id not in previous_logs]
        self.assertEqual([(row.coins_change, row.gems_change) for row in new_logs], [(5000, 50)])

    def test_completion_retries_unpaid_prizes_once(self):
        from models import TournamentTeam, User
        for team in self.teams.values():
            team.owner_tg_id = F.next_tg()
        self.session.commit()
        final = self._play_out()
        champ = self.session.get(TournamentTeam, final.winner_team_id)
        owner = User(telegram_id=champ.owner_tg_id, username="completion_owner",
                     total_coins=17, total_gems=2)
        self.session.add(owner)
        self.session.commit()

        for _ in range(2):
            self.ts.set_status(self.session, self.tour.id, "completed")
            self.session.commit()
            self.session.refresh(owner)
            self.assertEqual((owner.total_coins, owner.total_gems), (5017, 52))

    def test_prize_failure_rolls_back_balances_and_retains_retryable_receipts(self):
        from models import TournamentHonour, TournamentTeam, TournamentPrizePayout
        with patch("services.activity_service.log_activity", side_effect=RuntimeError("log failed")):
            final = self._play_out()
        self.assertEqual(final.status, "completed")
        self.assertIsNotNone(self.tour.awards_given_at)
        self.assertGreater(self.session.query(TournamentHonour).filter_by(
            tournament_id=self.tour.id).count(), 0)
        payments = self.session.query(TournamentPrizePayout).filter_by(
            tournament_id=self.tour.id).all()
        self.assertEqual({p.status for p in payments}, {"failed"})
        self.assertTrue(all(p.failure_reason and p.paid_at is None for p in payments))
        for owner in self.owners.values():
            self.session.refresh(owner)
            self.assertEqual((owner.total_coins, owner.total_gems), (0, 0))

        self.assertEqual(self.TA.finalize(self.session, self.tour), [])
        self.session.commit()
        champ = self.session.get(TournamentTeam, final.winner_team_id)
        owner = self.owners[champ.name]
        self.session.refresh(owner)
        self.assertEqual((owner.total_coins, owner.total_gems), (5000, 50))

    def test_retry_preserves_unflushed_prize_and_account_changes(self):
        from models import TournamentHonour, User
        self._play_out()
        champion = self.session.query(TournamentHonour).filter_by(
            tournament_id=self.tour.id, award="champion").one()
        owner = self.session.get(User, champion.user_id)
        owner.total_coins += 29
        owner.total_gems += 2
        self.TA.set_prize(self.tour, "captain", 321, 7)

        self.assertEqual(self.TA.finalize(self.session, self.tour), [])
        self.session.commit()
        self.session.refresh(owner)
        self.assertEqual((owner.total_coins, owner.total_gems), (5350, 59))
        self.assertEqual(self.TA.prizes(self.tour)["winning_captain"],
                         {"coins": 321, "gems": 7})

    def test_personal_award_with_known_team_uses_the_team_management_split(self):
        from models import TournamentHonour, User
        self._play_out()
        champion = self.session.query(TournamentHonour).filter_by(
            tournament_id=self.tour.id, award="champion").one()
        owner = self.session.get(User, champion.user_id)
        personal = self.session.query(TournamentHonour).filter_by(
            tournament_id=self.tour.id, award="player_of_final").one()
        personal.team_name = champion.team_name
        personal.player_name = "Unresolved player"
        personal.user_id = None
        self.TA.set_prize(self.tour, "final", 111, 2)
        self.session.commit()

        self.TA.finalize(self.session, self.tour)
        self.TA.ensure_ceremony_honours(self.session, self.tour)
        self.session.commit()
        self.session.refresh(owner)
        self.session.refresh(personal)
        self.assertEqual((owner.total_coins, owner.total_gems), (5111, 52))
        self.assertEqual((personal.prize_coins, personal.prize_gems), (111, 2))

    def test_ceremony_waits_for_the_admin_command(self):
        self._play_out()
        posts = self.tw.tick(self.session, self.tour, now=self.now + timedelta(hours=1))
        self.assertFalse(any(p.kind == "ceremony" for p in posts))
        self.assertIsNone(self.tour.awards_announced_at)
        self.assertIn("5,000 coins", self.TA.render_ceremony(self.session, self.tour))

    def test_completed_tournament_keeps_honours_for_manual_ceremony(self):
        self._play_out()
        self.tour.status = "completed"
        self.session.commit()
        self.assertTrue(self.TA.honours_for(self.session, self.tour.id))
        self.assertIsNone(self.tour.awards_announced_at)
        posts = self.tw.tick(self.session, self.tour, now=self.now + timedelta(hours=1))
        self.assertFalse(any(p.kind == "ceremony" for p in posts))

    def test_the_hall_of_fame_lists_the_honours(self):
        from handlers.halloffame import render_section
        self._play_out()
        text = render_section(self.session, "tour")
        self.assertIn("Watch Cup", text)
        self.assertIn("Champion", text)

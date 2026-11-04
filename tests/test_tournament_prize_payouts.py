"""Shared tournament prizes conserve money and retain each recipient's receipt."""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _tour_fixture as F


def setUpModule():
    F.setup_module_db()


def tearDownModule():
    F.teardown_module_db()


class SharedPrizeTests(F.FourTeamCase):
    def setUp(self):
        super().setUp()
        from services import tournament_awards, tournament_prize_splits
        self.A, self.S = tournament_awards, tournament_prize_splits
        self.coowners = {}

    @property
    def P(self):
        from services import tournament_payouts
        return tournament_payouts

    def add_coowners(self, count=1, registered=True):
        from models import User
        for name, team in self.teams.items():
            ids, users = [], []
            for _ in range(count):
                tg = F.next_tg()
                ids.append(tg)
                if registered:
                    user = User(telegram_id=tg, username=f"co{tg}")
                    self.session.add(user)
                    users.append(user)
            team.co_owner_ids_json = json.dumps(ids)
            self.coowners[name] = users if registered else ids
        self.session.commit()

    def finish(self, with_orange=False):
        from models import TournamentPlayerStats, TournamentTeam, User
        self.finish_league()
        for fixture in self.fixtures(stage="semifinal"):
            self.ts.simulate_fixture(self.session, fixture.id, "1")
        final = self.fixtures(stage="final")[0]
        champion = self.session.get(TournamentTeam, final.team1_id)
        controller = None
        if with_orange:
            # A match controller is deliberately outside the franchise's
            # management group: the team's awards must still share its split.
            controller = User(telegram_id=F.next_tg(), username="guest_controller")
            self.session.add(controller)
            self.session.flush()
            self.session.add(TournamentPlayerStats(
                tournament_id=self.tour.id, team_name=champion.name,
                name="Orange Star", user_id=controller.id, matches=1,
                bat_innings=1, bat_runs=120, bat_balls=60, bat_fours=10,
                bat_sixes=6))
            self.fixtures(1)[0].scorecard_json = json.dumps([{
                "name": "Orange Star", "team_name": champion.name,
                "user_id": controller.id, "player_id": None,
                "batted": True, "bat_runs": 120, "bat_balls": 60,
                "bat_fours": 10, "bat_sixes": 6, "bowled": False,
            }])
            self.session.flush()
        self.ts.simulate_fixture(self.session, final.id, "1")
        self.session.commit()
        return champion, controller

    def honour(self, award="champion"):
        from models import TournamentHonour
        return self.session.query(TournamentHonour).filter_by(
            tournament_id=self.tour.id, award=award).one()

    def rows(self, award=None):
        rows = self.P.payout_rows(self.session, self.tour.id)
        return [row for row in rows if award is None or row.award == award]

    def balances(self, user):
        self.session.refresh(user)
        return user.total_coins, user.total_gems

    def test_winner_and_orange_cap_pay_45000_to_each_manager(self):
        self.add_coowners()
        self.A.set_prize(self.tour, "champion", 60_000, 60)
        self.A.set_prize(self.tour, "orange", 30_000, 30)
        self.session.commit()
        team, controller = self.finish(with_orange=True)
        owner, coowner = self.owners[team.name], self.coowners[team.name][0]
        self.assertEqual(self.balances(owner), (45_000, 45))
        self.assertEqual(self.balances(coowner), (45_000, 45))
        self.assertEqual(self.balances(controller), (0, 0))
        rows = self.rows()
        self.assertEqual(len(rows), 4)
        self.assertEqual({row.status for row in rows}, {"paid"})
        self.assertEqual({row.recipient_tg_id for row in rows},
                         {owner.telegram_id, coowner.telegram_id})
        self.assertEqual(sum(row.coins for row in rows), 90_000)
        self.assertEqual(sum(row.gems for row in rows), 90)
        for row in rows:
            self.assertIsNotNone(row.paid_at)
            self.assertEqual(row.share_bps, 5000)
            self.assertIsNotNone(row.balance_coins_after)
            self.assertIsNotNone(row.balance_gems_after)

    def test_custom_percentages_apply_to_team_and_performance_awards(self):
        self.add_coowners()
        for name, team in self.teams.items():
            self.S.set_split(self.session, team.id, {
                team.owner_tg_id: "70", self.coowners[name][0].telegram_id: "30",
            })
        self.A.set_prize(self.tour, "champion", 60_000, 60)
        self.A.set_prize(self.tour, "orange", 30_000, 30)
        self.session.commit()
        team, _ = self.finish(with_orange=True)
        self.assertEqual(self.balances(self.owners[team.name]), (63_000, 63))
        self.assertEqual(self.balances(self.coowners[team.name][0]), (27_000, 27))

    def test_equal_three_way_split_preserves_whole_coin_and_gem_totals(self):
        self.add_coowners(count=2)
        self.A.set_prize(self.tour, "champion", 101, 5)
        self.session.commit()
        team, _ = self.finish()
        rows = self.rows("champion")
        self.assertEqual(len(rows), 3)
        self.assertEqual(sum(row.coins for row in rows), 101)
        self.assertEqual(sum(row.gems for row in rows), 5)
        self.assertEqual(sorted(row.coins for row in rows), [33, 34, 34])
        self.assertEqual(sorted(row.gems for row in rows), [1, 2, 2])
        owner_row = next(row for row in rows if row.recipient_tg_id == team.owner_tg_id)
        self.assertEqual((owner_row.coins, owner_row.gems), (34, 2))

    def test_pooling_rounds_across_awards_so_equal_managers_receive_equal_totals(self):
        self.add_coowners()
        self.A.set_prize(self.tour, "champion", 3, 1)
        self.A.set_prize(self.tour, "orange", 3, 1)
        self.session.commit()
        team, _ = self.finish(with_orange=True)
        self.assertEqual(self.balances(self.owners[team.name]), (3, 1))
        self.assertEqual(self.balances(self.coowners[team.name][0]), (3, 1))
        self.assertEqual(sum(row.coins for row in self.rows()), 6)
        self.assertEqual(sum(row.gems for row in self.rows()), 2)

    def test_later_configured_award_uses_cumulative_rounding_without_repaying_earlier_shares(self):
        self.add_coowners()
        self.A.set_prize(self.tour, "champion", 1, 1)
        self.session.commit()
        team, _ = self.finish(with_orange=True)
        owner, coowner = self.owners[team.name], self.coowners[team.name][0]
        initial_rows = [(row.id, row.recipient_tg_id, row.coins, row.gems,
                         row.status, row.paid_at) for row in self.rows("champion")]
        self.assertEqual(self.balances(owner), (1, 1))
        self.assertEqual(self.balances(coowner), (0, 0))

        self.A.set_prize(self.tour, "orange", 1, 1)
        for _ in range(2):
            self.P.retry_payouts(self.session, self.tour)
            self.session.commit()
        self.assertEqual(self.balances(owner), (1, 1))
        self.assertEqual(self.balances(coowner), (1, 1))
        self.assertEqual([(row.id, row.recipient_tg_id, row.coins, row.gems,
                           row.status, row.paid_at) for row in self.rows("champion")],
                         initial_rows)
        self.assertEqual(sum(row.coins for row in self.rows()), 2)
        self.assertEqual(sum(row.gems for row in self.rows()), 2)

    def test_team_rename_keeps_later_performance_prize_with_locked_managers(self):
        self.add_coowners()
        self.A.set_prize(self.tour, "champion", 60_000, 60)
        self.session.commit()
        team, controller = self.finish(with_orange=True)
        original_name = team.name
        owner, coowner = self.owners[original_name], self.coowners[original_name][0]
        orange = self.honour("orange_cap")
        self.assertEqual(orange.team_name, original_name)
        self.assertEqual(orange.tournament_team_id, team.id)
        team.name = "Renamed Champion"
        self.A.set_prize(self.tour, "orange", 30_000, 30)
        self.session.commit()

        self.P.retry_payouts(self.session, self.tour)
        self.session.commit()
        self.assertEqual(self.balances(owner), (45_000, 45))
        self.assertEqual(self.balances(coowner), (45_000, 45))
        self.assertEqual(self.balances(controller), (0, 0))
        self.assertEqual({row.recipient_tg_id for row in self.rows("orange_cap")},
                         {owner.telegram_id, coowner.telegram_id})
        self.session.refresh(orange)
        self.assertEqual(orange.team_name, original_name)

    def test_pending_share_keeps_its_amount_and_recipient_until_registration(self):
        from models import User
        self.add_coowners(registered=False)
        self.A.set_prize(self.tour, "champion", 60_000, 50)
        self.session.commit()
        team, _ = self.finish()
        owner = self.owners[team.name]
        old_coowner_tg = self.coowners[team.name][0]
        self.assertEqual(self.balances(owner), (30_000, 25))
        rows = self.rows("champion")
        pending = next(row for row in rows if row.recipient_tg_id == old_coowner_tg)
        self.assertEqual(pending.status, "pending")
        self.assertEqual((pending.coins, pending.gems), (30_000, 25))
        self.assertIsNone(pending.paid_at)
        honour = self.honour()
        self.assertEqual((honour.prize_snapshot_coins, honour.prize_snapshot_gems), (60_000, 50))

        # Neither owner edits nor prize edits can redirect an earned share.
        team.co_owner_ids_json = json.dumps([F.next_tg()])
        self.A.set_prize(self.tour, "champion", 99_000, 90)
        late = User(telegram_id=old_coowner_tg, username="late_coowner",
                    total_coins=8, total_gems=2)
        self.session.add(late)
        self.session.commit()
        for _ in range(2):
            self.P.retry_payouts(self.session, self.tour)
            self.session.commit()
        self.assertEqual(self.balances(late), (30_008, 27))
        self.assertEqual(self.balances(owner), (30_000, 25))
        self.session.refresh(pending)
        self.assertEqual(pending.status, "paid")
        self.assertEqual((pending.balance_coins_after, pending.balance_gems_after), (30_008, 27))
        self.assertEqual(len(self.rows("champion")), 2)

    def test_split_and_recipient_snapshot_lock_on_first_match(self):
        self.add_coowners()
        team = self.teams["Alpha"]
        owner, coowner = self.owners["Alpha"], self.coowners["Alpha"][0]
        self.S.set_split(self.session, team.id, {
            owner.telegram_id: "60", coowner.telegram_id: "40",
        })
        self.session.commit()
        self.finish_round(1)
        self.session.refresh(team)
        self.assertIsNotNone(team.prize_split_locked_at)
        with self.assertRaises(ValueError):
            self.S.set_split(self.session, team.id, {
                owner.telegram_id: "10", coowner.telegram_id: "90",
            })
        team.owner_tg_id = F.next_tg()
        team.co_owner_ids_json = None
        self.session.flush()
        recipients = self.S.recipients(self.session, self.tour, team)
        self.assertEqual(recipients, [
            {"telegram_id": owner.telegram_id, "weight": 6000},
            {"telegram_id": coowner.telegram_id, "weight": 4000},
        ])

    def test_split_locks_when_tournament_is_activated(self):
        self.add_coowners()
        self.ts.activate_tournament(self.session, self.tour.id)
        self.session.commit()
        for team in self.teams.values():
            self.session.refresh(team)
            self.assertIsNotNone(team.prize_split_locked_at)
            with self.assertRaises(ValueError):
                self.S.set_split(self.session, team.id, {})

    def test_reserving_the_first_live_match_locks_all_team_recipients(self):
        self.add_coowners()
        fixture = self.fixtures(1)[0]
        reserved = self.lss.reserve_fixture(self.session, self.tour.id,
                                            fixture.team1_id, fixture.team2_id)
        self.session.commit()
        self.assertIsNotNone(reserved)
        self.assertEqual(reserved.status, "live")
        for team in self.teams.values():
            self.session.refresh(team)
            self.assertIsNotNone(team.prize_split_locked_at)
            frozen = self.S.recipients(self.session, self.tour, team)
            expected = {team.owner_tg_id, self.coowners[team.name][0].telegram_id}
            self.assertEqual({row["telegram_id"] for row in frozen}, expected)
            with self.assertRaises(ValueError):
                self.S.set_split(self.session, team.id, {})

    def test_custom_split_validation_and_default_reset(self):
        self.add_coowners()
        team = self.teams["Alpha"]
        owner, coowner = team.owner_tg_id, self.coowners["Alpha"][0].telegram_id
        for shares in ({owner: "70", coowner: "20"},
                       {owner: "100"}, {owner: "0", coowner: "100"},
                       {owner: "50.001", coowner: "49.999"},
                       {owner: "50", F.next_tg(): "50"}):
            with self.assertRaises(ValueError):
                self.S.set_split(self.session, team.id, shares)
        self.S.set_split(self.session, team.id, {owner: "70", coowner: "30"})
        self.S.set_split(self.session, team.id, {})
        settings = self.S.split_settings(self.session, self.tour, team)
        self.assertFalse(settings["custom"])
        self.assertFalse(settings["locked"])
        self.assertEqual(settings["percentages"], {str(owner): "50.00", str(coowner): "50.00"})

    def test_failed_share_rolls_back_its_balance_and_can_be_retried(self):
        self.add_coowners()
        self.A.set_prize(self.tour, "champion", 6000, 6)
        self.session.commit()
        from services import activity_service
        original_log = activity_service.log_activity

        def fail_coowner(session, user_id, action, *args, **kwargs):
            from models import User
            user = session.get(User, user_id)
            if action == "tournament_prize" and user.username.startswith("co"):
                raise RuntimeError("temporary receipt failure")
            return original_log(session, user_id, action, *args, **kwargs)

        with patch.object(activity_service, "log_activity", side_effect=fail_coowner):
            team, _ = self.finish()
        owner, coowner = self.owners[team.name], self.coowners[team.name][0]
        self.assertEqual(self.balances(owner), (3000, 3))
        self.assertEqual(self.balances(coowner), (0, 0))
        failed = next(row for row in self.rows("champion") if row.recipient_tg_id == coowner.telegram_id)
        self.assertEqual(failed.status, "failed")
        self.assertTrue(failed.failure_reason)
        self.assertIsNone(failed.paid_at)
        self.assertEqual(self.P.payout_summary(self.session, self.tour.id)["failed"], 1)
        self.P.retry_payouts(self.session, self.tour)
        self.session.commit()
        self.assertEqual(self.balances(owner), (3000, 3))
        self.assertEqual(self.balances(coowner), (3000, 3))
        self.session.refresh(failed)
        self.assertEqual(failed.status, "paid")
        self.assertFalse(failed.failure_reason)

    def test_concurrent_retries_credit_a_registered_pending_share_once(self):
        from database import get_session
        from models import ActivityLog, Tournament, User
        self.add_coowners(registered=False)
        self.A.set_prize(self.tour, "champion", 6000, 6)
        self.session.commit()
        team, _ = self.finish()
        late = User(telegram_id=self.coowners[team.name][0], username="concurrent_coowner")
        self.session.add(late)
        self.session.commit()
        tid, uid = self.tour.id, late.id
        ready = Barrier(2)

        def retry():
            session = get_session()
            try:
                tour = session.get(Tournament, tid)
                self.P.payout_rows(session, tid)
                ready.wait(timeout=10)
                self.P.retry_payouts(session, tour)
                session.commit()
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(retry) for _ in range(2)]
            for future in futures:
                future.result(timeout=20)
        self.assertEqual(self.balances(late), (3000, 3))
        logs = self.session.query(ActivityLog).filter_by(user_id=uid, action="tournament_prize").all()
        self.assertEqual(len(logs), 1)
        self.assertEqual((logs[0].coins_change, logs[0].gems_change), (3000, 3))
        self.assertEqual(len(self.rows("champion")), 2)

    def test_legacy_paid_honour_without_ledger_is_never_paid_again(self):
        from models import TournamentHonour
        team = self.teams["Alpha"]
        owner = self.owners["Alpha"]
        owner.total_coins, owner.total_gems = 60_000, 60
        self.tour.awards_given_at = self.tour.created_at
        honour = TournamentHonour(
            tournament_id=self.tour.id, tournament_name=self.tour.name,
            award="champion", team_name=team.name, user_id=owner.id,
            prize_coins=60_000, prize_gems=60)
        self.session.add(honour)
        self.A.set_prize(self.tour, "champion", 60_000, 60)
        self.session.commit()
        self.A.finalize(self.session, self.tour)
        self.P.retry_payouts(self.session, self.tour)
        self.session.commit()
        self.assertEqual(self.balances(owner), (60_000, 60))
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].coins, rows[0].gems, rows[0].status), (60_000, 60, "paid"))

    def test_summary_reports_paid_pending_and_failed_recipient_counts(self):
        self.add_coowners(registered=False)
        self.A.set_prize(self.tour, "champion", 1000, 10)
        self.session.commit()
        self.finish()
        summary = self.P.payout_summary(self.session, self.tour.id)
        self.assertEqual(summary["paid"], 1)
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["failed"], 0)
        text = self.P.honour_payment_text(self.session, self.tour, self.honour())
        self.assertIn("Credited: 500 coins + 5 gems", text)
        self.assertIn("Pending: 500 coins + 5 gems", text)

    def test_private_receipts_show_only_that_users_share_and_balance_after_credit(self):
        self.add_coowners()
        self.A.set_prize(self.tour, "champion", 1000, 10)
        self.session.commit()
        team, _ = self.finish()
        owner, coowner = self.owners[team.name], self.coowners[team.name][0]
        rows = self.rows("champion")
        own_row = next(row for row in rows if row.recipient_tg_id == owner.telegram_id)
        co_row = next(row for row in rows if row.recipient_tg_id == coowner.telegram_id)
        text = self.P.recipient_receipts(self.session, owner.telegram_id)
        self.assertIn(f"#{own_row.id} ·", text)
        self.assertNotIn(f"#{co_row.id} ·", text)
        self.assertIn("Credited: 500 coins + 5 gems", text)
        self.assertIn("Balance after credit: 500 coins + 5 gems", text)
        self.assertIn(self.tour.name, text)

"""Reopened split editing changes money only through an atomic redistribution."""

import json
import os
import sys
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _tour_fixture as F


def setUpModule():
    F.setup_module_db()


def tearDownModule():
    F.teardown_module_db()


class PrizeRedistributionTests(F.FourTeamCase):
    @property
    def C(self):
        from services import tournament_prize_corrections
        return tournament_prize_corrections

    @property
    def P(self):
        from services import tournament_payouts
        return tournament_payouts

    def setup_team(self, registered=True, awards=True):
        from models import TournamentMatch, TournamentPlayerStats, User
        from services import tournament_awards, tournament_prize_splits
        self.team = self.teams["Alpha"]
        self.owner = self.owners["Alpha"]
        self.coowner_tg = F.next_tg()
        self.team.co_owner_ids_json = json.dumps([self.coowner_tg])
        self.coowner = None
        if registered:
            self.coowner = User(telegram_id=self.coowner_tg, username="redistribution_coowner")
            self.session.add(self.coowner)
        self.session.flush()
        if awards:
            tournament_awards.set_prize(self.tour, "champion", 60_000, 60)
            tournament_awards.set_prize(self.tour, "orange", 30_000, 30)
            self.session.add(TournamentPlayerStats(
                tournament_id=self.tour.id, team_name=self.team.name,
                name="Orange Winner", user_id=self.owner.id,
                matches=1, bat_innings=1, bat_runs=120, bat_balls=60,
                bat_fours=10, bat_sixes=6))
            self.session.add(TournamentMatch(
                tournament_id=self.tour.id, stage="final", status="completed",
                team1_id=self.team.id, team2_id=self.teams["Bravo"].id,
                winner_team_id=self.team.id, result_type="win"))
            self.session.flush()
            tournament_awards.finalize(self.session, self.tour)
        else:
            tournament_prize_splits.lock_splits(self.session, self.tour)
        self.session.commit()
        self.session.refresh(self.team)
        self.original_snapshot = self.team.prize_split_snapshot_json

    def reopen(self):
        self.C.reopen_split(self.session, self.tour, self.team.id, actor="split-admin")
        self.session.commit()
        self.session.refresh(self.team)

    def shares(self, owner="70", coowner="30"):
        return {self.owner.telegram_id: owner, self.coowner_tg: coowner}

    def redistribute(self, **changes):
        fields = dict(
            shares=self.shares(), reason="Correct the agreed owner percentages",
            request_key=uuid.uuid4().hex,
            expected_snapshot=self.team.prize_split_snapshot_json, actor="split-admin")
        fields.update(changes)
        result = self.C.redistribute_team(self.session, self.tour, self.team.id, **fields)
        self.session.commit()
        self.session.refresh(self.team)
        return result

    def balances(self, user):
        self.session.refresh(user)
        return user.total_coins, user.total_gems

    def rows(self):
        return [r for r in self.P.payout_rows(self.session, self.tour.id)
                if r.team_name == self.team.name]

    def batch_rows(self):
        from models import TournamentPrizeRedistribution
        return (self.session.query(TournamentPrizeRedistribution)
                .filter_by(tournament_id=self.tour.id)
                .order_by(TournamentPrizeRedistribution.id).all())

    def state(self):
        from models import ActivityLog, TournamentHonour, TournamentPrizeCorrection, User
        balances = [(u.id, u.total_coins, u.total_gems)
                    for u in self.session.query(User).order_by(User.id)]
        rows = [(r.id, r.honour_id, r.revision, r.recipient_tg_id,
                 r.coins, r.gems, r.status, r.paid_at, r.reversed_at,
                 r.balance_coins_after, r.balance_gems_after,
                 r.reversal_coins_after, r.reversal_gems_after) for r in self.rows()]
        honours = [(h.id, h.prize_revision, h.team_name, h.player_name,
                    h.prize_coins, h.prize_gems, h.prize_snapshot_coins,
                    h.prize_snapshot_gems) for h in self.session.query(TournamentHonour)
                   .filter_by(tournament_id=self.tour.id).order_by(TournamentHonour.id)]
        team = (self.team.prize_split_json, self.team.prize_split_snapshot_json,
                self.team.prize_split_locked_at, self.team.prize_split_reopened_at)
        return (balances, rows, honours, team,
                self.session.query(ActivityLog).count(),
                self.session.query(TournamentPrizeCorrection)
                .filter_by(tournament_id=self.tour.id).count(), len(self.batch_rows()))

    def test_reopening_and_editing_do_not_move_money_until_redistributed(self):
        from services import tournament_prize_splits as splits
        self.setup_team()
        self.reopen()
        before = self.team.prize_split_snapshot_json
        settings = splits.split_settings(self.session, self.tour, self.team)
        self.assertTrue(settings["reopened"])
        self.assertFalse(settings["locked"])
        splits.set_split(self.session, self.team.id, self.shares())
        self.P.retry_payouts(self.session, self.tour)
        self.session.commit()
        self.session.refresh(self.team)
        self.assertEqual(self.team.prize_split_snapshot_json, before)
        self.assertEqual(self.balances(self.owner), (45_000, 45))
        self.assertEqual(self.balances(self.coowner), (45_000, 45))
        self.assertTrue(self.team.prize_split_reopened_at)

    def test_all_awards_are_reversed_and_redistributed_70000_30000_percent(self):
        from models import TournamentHonour
        from services import tournament_prize_splits as splits
        self.setup_team()
        old_honours = {h.id: (h.team_name, h.player_name,
                             h.prize_snapshot_coins, h.prize_snapshot_gems)
                       for h in self.session.query(TournamentHonour)
                       .filter_by(tournament_id=self.tour.id)}
        self.reopen()
        corrections = self.redistribute()
        self.assertTrue(corrections)
        self.assertEqual(self.balances(self.owner), (63_000, 63))
        self.assertEqual(self.balances(self.coowner), (27_000, 27))
        previous = [row for row in self.rows() if row.revision == 0]
        current = [row for row in self.rows() if row.status == "paid"]
        self.assertEqual({row.status for row in previous}, {"reversed"})
        self.assertEqual(sum(row.coins for row in current), 90_000)
        self.assertEqual(sum(row.gems for row in current), 90)
        for honour in self.session.query(TournamentHonour).filter_by(tournament_id=self.tour.id):
            self.assertEqual((honour.team_name, honour.player_name,
                              honour.prize_snapshot_coins, honour.prize_snapshot_gems),
                             old_honours[honour.id])
        self.assertIsNone(self.team.prize_split_reopened_at)
        settings = splits.split_settings(self.session, self.tour, self.team)
        self.assertTrue(settings["locked"])
        self.assertFalse(settings["reopened"])
        self.assertEqual(settings["percentages"], {
            str(self.owner.telegram_id): "70.00", str(self.coowner_tg): "30.00"})
        self.assertEqual(len(self.batch_rows()), 1)

    def test_team_redistribution_requires_explicit_reopening(self):
        self.setup_team()
        before = self.state()
        with self.assertRaises(ValueError):
            self.redistribute()
        self.session.commit()
        self.session.expire_all()
        self.assertEqual(self.state(), before)

    def test_same_batch_request_replays_without_second_reversal(self):
        self.setup_team()
        self.reopen()
        key, snapshot = uuid.uuid4().hex, self.original_snapshot
        first = self.redistribute(request_key=key, expected_snapshot=snapshot)
        before = self.state()
        second = self.redistribute(request_key=key, expected_snapshot=snapshot)
        self.assertEqual([c.id for c in first], [c.id for c in second])
        self.assertEqual(self.state(), before)

    def test_old_split_snapshot_cannot_override_a_newer_redistribution(self):
        self.setup_team()
        original = self.original_snapshot
        self.reopen()
        self.redistribute()
        self.reopen()
        before = self.state()
        with self.assertRaises(ValueError):
            self.redistribute(shares=self.shares("80", "20"), expected_snapshot=original)
        self.session.commit()
        self.session.expire_all()
        self.assertEqual(self.state(), before)

    def test_missing_reason_or_nonmanager_recipient_preserves_the_open_editor(self):
        self.setup_team()
        self.reopen()
        before = self.state()
        for fields in ({"reason": " "}, {"shares": {F.next_tg(): "100"}},
                       {"shares": self.shares("70", "40")}):
            with self.subTest(fields=fields):
                with self.assertRaises(ValueError):
                    self.redistribute(**fields)
                self.session.commit()
                self.session.expire_all()
                self.assertEqual(self.state(), before)

    def test_insufficient_total_previous_balance_blocks_every_award(self):
        self.setup_team()
        self.owner.total_coins = 44_999
        self.session.commit()
        self.reopen()
        before = self.state()
        with self.assertRaises(ValueError):
            self.redistribute()
        self.session.commit()
        self.session.expire_all()
        self.assertEqual(self.state(), before)

    def test_second_award_failure_rolls_back_first_award_and_split_change(self):
        from services.activity_service import log_activity
        self.setup_team()
        self.reopen()
        before = self.state()
        replacements = 0

        def fail_later_credit(session, uid, action, *args, **kwargs):
            nonlocal replacements
            if action == "tournament_prize":
                replacements += 1
                if replacements == 3:
                    raise RuntimeError("later award credit unavailable")
            return log_activity(session, uid, action, *args, **kwargs)

        with patch("services.activity_service.log_activity", side_effect=fail_later_credit):
            with self.assertRaises((ValueError, RuntimeError)):
                self.redistribute()
        self.assertGreaterEqual(replacements, 3)
        self.session.commit()
        self.session.expire_all()
        self.assertEqual(self.state(), before)

    def test_unregistered_previous_shares_are_cancelled_and_replaced_exactly(self):
        from models import User
        self.setup_team(registered=False)
        self.reopen()
        self.redistribute()
        self.assertEqual(self.balances(self.owner), (63_000, 63))
        previous_pending = [r for r in self.rows() if r.revision == 0 and
                            r.recipient_tg_id == self.coowner_tg]
        self.assertEqual({row.status for row in previous_pending}, {"cancelled"})
        current_pending = [r for r in self.rows() if r.status == "pending"]
        self.assertEqual(sum(r.coins for r in current_pending), 27_000)
        self.assertEqual(sum(r.gems for r in current_pending), 27)
        late = User(telegram_id=self.coowner_tg, username="late_redistributed_owner")
        self.session.add(late)
        self.session.flush()
        for _ in range(2):
            self.P.retry_for_recipient(self.session, self.coowner_tg)
            self.P.retry_payouts(self.session, self.tour)
            self.session.commit()
        self.assertEqual(self.balances(late), (27_000, 27))
        self.assertEqual(self.balances(self.owner), (63_000, 63))

    def test_reopening_again_can_restore_equal_shares_without_reminting_awards(self):
        self.setup_team()
        self.reopen()
        self.redistribute()
        self.reopen()
        self.redistribute(shares=None, reason="Restore equal owner shares")
        self.assertEqual(self.balances(self.owner), (45_000, 45))
        self.assertEqual(self.balances(self.coowner), (45_000, 45))
        self.assertEqual(sum(row.coins for row in self.rows() if row.status == "paid"), 90_000)
        self.assertEqual(len(self.batch_rows()), 2)

    def test_reopened_team_without_awards_updates_the_split_and_closes_the_lock(self):
        from models import TournamentHonour
        from services import tournament_prize_splits as splits
        self.setup_team(awards=False)
        self.reopen()
        result = self.redistribute()
        self.assertEqual(result, [])
        self.assertEqual(self.balances(self.owner), (0, 0))
        self.assertEqual(self.balances(self.coowner), (0, 0))
        self.assertEqual(self.session.query(TournamentHonour).filter_by(tournament_id=self.tour.id).count(), 0)
        self.assertIsNone(self.team.prize_split_reopened_at)
        settings = splits.split_settings(self.session, self.tour, self.team)
        self.assertTrue(settings["locked"])
        self.assertEqual(settings["percentages"][str(self.owner.telegram_id)], "70.00")
        self.assertEqual(len(self.batch_rows()), 1)

    def test_ownerless_prize_does_not_pay_draft_recipients_until_redistribution_applies(self):
        from models import TournamentMatch
        from services import tournament_awards, tournament_prize_splits
        self.team, self.owner = self.teams["Alpha"], self.owners["Alpha"]
        self.coowner_tg = F.next_tg()
        self.team.owner_tg_id = None
        tournament_awards.set_prize(self.tour, "champion", 60_000, 60)
        self.session.add(TournamentMatch(
            tournament_id=self.tour.id, stage="final", status="completed",
            team1_id=self.team.id, team2_id=self.teams["Bravo"].id,
            winner_team_id=self.team.id, result_type="win"))
        self.session.flush()
        tournament_awards.finalize(self.session, self.tour)
        self.session.commit()
        self.assertIsNone(self.team.prize_split_snapshot_json)
        self.assertEqual({row.status for row in self.rows()}, {"pending"})

        self.team.owner_tg_id = self.owner.telegram_id
        self.session.commit()
        self.reopen()
        tournament_prize_splits.set_split(self.session, self.team.id,
                                          {self.owner.telegram_id: "100"})
        self.P.retry_payouts(self.session, self.tour)
        self.session.commit()
        self.assertEqual(self.balances(self.owner), (0, 0))
        self.assertIsNone(self.team.prize_split_snapshot_json)
        self.assertEqual({row.status for row in self.rows()}, {"pending"})

        self.redistribute(shares={self.owner.telegram_id: "100"}, expected_snapshot=None)
        self.assertEqual(self.balances(self.owner), (60_000, 60))
        self.assertIsNone(self.team.prize_split_reopened_at)
        self.assertEqual({row.status for row in self.rows()}, {"cancelled", "paid"})

    def test_clean_preaward_reset_clears_reopened_state_and_unlocks_normal_editing(self):
        from services import tournament_prize_splits
        self.setup_team(awards=False)
        self.reopen()
        self.ts.reset_tournament(self.session, self.tour.id)
        self.session.commit()
        self.session.refresh(self.team)
        self.assertIsNone(self.team.prize_split_reopened_at)
        self.assertIsNone(self.team.prize_split_snapshot_json)
        self.assertIsNone(self.team.prize_split_locked_at)
        tournament_prize_splits.set_split(self.session, self.team.id, self.shares())
        self.session.commit()
        settings = tournament_prize_splits.split_settings(self.session, self.tour, self.team)
        self.assertFalse(settings["locked"])
        self.assertFalse(settings["reopened"])
        self.assertEqual(settings["percentages"][str(self.owner.telegram_id)], "70.00")

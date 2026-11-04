"""Prize mistakes can be reversed and re-awarded without losing the audit trail."""

import json
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _tour_fixture as F


def setUpModule():
    F.setup_module_db()


def tearDownModule():
    F.teardown_module_db()


class PrizeCorrectionTests(F.FourTeamCase):
    @property
    def C(self):
        from services import tournament_prize_corrections
        return tournament_prize_corrections

    @property
    def P(self):
        from services import tournament_payouts
        return tournament_payouts

    def paid_award(self, coowner=True, registered=True, coins=60_000, gems=60):
        from models import TournamentHonour, TournamentMatch, User
        from services import tournament_awards
        self.coowners = {}
        if coowner:
            for name, team in self.teams.items():
                tg = F.next_tg()
                team.co_owner_ids_json = json.dumps([tg])
                if registered:
                    user = User(telegram_id=tg, username=f"correct_co{tg}")
                    self.session.add(user)
                    self.coowners[name] = user
                else:
                    self.coowners[name] = tg
        tournament_awards.set_prize(self.tour, "champion", coins, gems)
        self.session.add(TournamentMatch(
            tournament_id=self.tour.id, stage="final", status="completed",
            team1_id=self.teams["Alpha"].id, team2_id=self.teams["Bravo"].id,
            winner_team_id=self.teams["Alpha"].id, result_type="win"))
        self.session.flush()
        tournament_awards.finalize(self.session, self.tour)
        self.session.commit()
        self.honour = self.session.query(TournamentHonour).filter_by(
            tournament_id=self.tour.id, award="champion").one()
        return self.honour

    def rows(self, revision=None):
        from models import TournamentPrizePayout
        query = self.session.query(TournamentPrizePayout).filter_by(honour_id=self.honour.id)
        if revision is not None:
            query = query.filter_by(revision=revision)
        return query.order_by(TournamentPrizePayout.id).all()

    def corrections(self):
        from models import TournamentPrizeCorrection
        return (self.session.query(TournamentPrizeCorrection)
                .filter_by(honour_id=self.honour.id)
                .order_by(TournamentPrizeCorrection.id).all())

    def balance(self, user):
        self.session.refresh(user)
        return user.total_coins, user.total_gems

    def correct(self, **changes):
        fields = dict(
            coins=40_000, gems=40, reason="Correct mistaken prize amount",
            request_key=uuid.uuid4().hex, expected_revision=self.honour.prize_revision,
            actor="test-admin")
        fields.update(changes)
        result = self.C.correct_award(self.session, self.tour, self.honour.id, **fields)
        self.session.commit()
        self.session.refresh(self.honour)
        return result

    def state(self):
        from models import ActivityLog, User
        balances = {user.telegram_id: (user.total_coins, user.total_gems)
                    for user in self.session.query(User).all()}
        rows = [(r.id, r.revision, r.recipient_tg_id, r.coins, r.gems, r.status,
                 r.paid_at, r.reversed_at, r.balance_coins_after,
                 r.balance_gems_after, r.reversal_coins_after,
                 r.reversal_gems_after) for r in self.rows()]
        honour = (self.honour.prize_revision, self.honour.prize_coins,
                  self.honour.prize_gems, self.honour.prize_snapshot_coins,
                  self.honour.prize_snapshot_gems, self.honour.team_name,
                  self.honour.tournament_team_id, self.honour.player_name)
        activity = self.session.query(ActivityLog).count()
        return balances, rows, honour, activity, len(self.corrections())

    def test_paid_shares_are_deducted_before_corrected_shares_are_credited(self):
        from models import ActivityLog
        self.paid_award()
        owner, coowner = self.owners["Alpha"], self.coowners["Alpha"]
        old = [(r.id, r.coins, r.gems, r.paid_at,
                r.balance_coins_after, r.balance_gems_after) for r in self.rows(0)]
        correction = self.correct()
        self.assertEqual(self.balance(owner), (20_000, 20))
        self.assertEqual(self.balance(coowner), (20_000, 20))
        self.assertEqual(self.honour.prize_revision, 1)
        self.assertEqual((self.honour.prize_snapshot_coins, self.honour.prize_snapshot_gems),
                         (40_000, 40))
        self.assertEqual((self.honour.prize_coins, self.honour.prize_gems), (40_000, 40))
        previous, current = self.rows(0), self.rows(1)
        self.assertEqual({row.status for row in previous}, {"reversed"})
        self.assertEqual({row.status for row in current}, {"paid"})
        self.assertEqual([(r.id, r.coins, r.gems, r.paid_at,
                           r.balance_coins_after, r.balance_gems_after) for r in previous], old)
        for row in previous:
            self.assertIsNotNone(row.reversed_at)
            self.assertEqual((row.reversal_coins_after, row.reversal_gems_after), (0, 0))
        self.assertEqual(sum(r.coins for r in current), 40_000)
        self.assertEqual(sum(r.gems for r in current), 40)
        self.assertEqual(correction.previous_revision, 0)
        self.assertEqual(correction.revision, 1)
        self.assertEqual(correction.reason, "Correct mistaken prize amount")
        self.assertEqual(correction.corrected_by, "test-admin")
        self.assertTrue(json.loads(correction.old_details_json))
        self.assertTrue(json.loads(correction.new_details_json))
        activity = (self.session.query(ActivityLog)
                    .filter(ActivityLog.user_id.in_((owner.id, coowner.id))).all())
        self.assertEqual(sum(row.coins_change for row in activity), 40_000)
        self.assertEqual(sum(row.gems_change for row in activity), 40)
        reversals = [row for row in activity if row.action == "tournament_prize_reversal"]
        self.assertEqual(len(reversals), 2)
        self.assertEqual({(row.coins_change, row.gems_change) for row in reversals}, {(-30_000, -30)})

    def test_corrected_team_receives_the_prize_using_its_locked_split(self):
        self.paid_award()
        corrected = self.teams["Bravo"]
        self.correct(coins=50_000, gems=50, team_id=corrected.id,
                     reason="The award was assigned to the wrong team")
        self.assertEqual(self.balance(self.owners["Alpha"]), (0, 0))
        self.assertEqual(self.balance(self.coowners["Alpha"]), (0, 0))
        self.assertEqual(self.balance(self.owners["Bravo"]), (25_000, 25))
        self.assertEqual(self.balance(self.coowners["Bravo"]), (25_000, 25))
        self.assertEqual(self.honour.tournament_team_id, corrected.id)
        self.assertEqual(self.honour.team_name, corrected.name)
        self.assertEqual({r.recipient_tg_id for r in self.rows(1)},
                         {self.owners["Bravo"].telegram_id,
                          self.coowners["Bravo"].telegram_id})

    def test_same_amount_correction_preserves_fair_rounding_across_other_team_awards(self):
        from models import TournamentHonour
        from services import tournament_awards
        self.paid_award(coins=3, gems=3)
        orange = TournamentHonour(
            tournament_id=self.tour.id, tournament_team_id=self.teams["Alpha"].id,
            tournament_name=self.tour.name, award="orange_cap", team_name="Alpha",
            player_name="Orange Star", user_id=self.owners["Alpha"].id,
            prize_coins=0, prize_gems=0)
        self.session.add(orange)
        tournament_awards.set_prize(self.tour, "orange", 3, 3)
        tournament_awards.finalize(self.session, self.tour)
        self.session.commit()
        self.honour = orange
        self.assertEqual(self.balance(self.owners["Alpha"]), (3, 3))
        self.assertEqual(self.balance(self.coowners["Alpha"]), (3, 3))
        self.correct(coins=3, gems=3, reason="Confirm the same prize without changing its split")
        self.assertEqual(self.balance(self.owners["Alpha"]), (3, 3))
        self.assertEqual(self.balance(self.coowners["Alpha"]), (3, 3))

    def test_explicit_corrected_recipient_can_replace_the_original_owners(self):
        from models import User
        self.paid_award()
        replacement = User(telegram_id=F.next_tg(), username="correct_recipient",
                           total_coins=7, total_gems=3)
        self.session.add(replacement)
        self.session.commit()
        self.correct(shares={replacement.telegram_id: "100"},
                     reason="The recipient IDs were entered incorrectly")
        self.assertEqual(self.balance(self.owners["Alpha"]), (0, 0))
        self.assertEqual(self.balance(self.coowners["Alpha"]), (0, 0))
        self.assertEqual(self.balance(replacement), (40_007, 43))
        current = self.rows(1)
        self.assertEqual(len(current), 1)
        self.assertEqual((current[0].recipient_tg_id, current[0].share_bps),
                         (replacement.telegram_id, 10000))

    def test_pending_old_share_is_cancelled_without_debit_or_future_repayment(self):
        from models import User
        self.paid_award(registered=False)
        old_tg = self.coowners["Alpha"]
        old_pending = next(row for row in self.rows(0) if row.recipient_tg_id == old_tg)
        owner = self.owners["Alpha"]
        self.correct(shares={owner.telegram_id: "100"})
        self.assertEqual(self.balance(owner), (40_000, 40))
        self.session.refresh(old_pending)
        self.assertEqual(old_pending.status, "cancelled")
        self.assertIsNone(old_pending.paid_at)
        late = User(telegram_id=old_tg, username="old_cancelled_recipient")
        self.session.add(late)
        self.session.flush()
        for _ in range(2):
            self.P.retry_for_recipient(self.session, old_tg)
            self.P.retry_payouts(self.session, self.tour)
            self.session.commit()
        self.assertEqual(self.balance(late), (0, 0))
        self.assertEqual(self.balance(owner), (40_000, 40))
        self.assertEqual({r.status for r in self.rows(0)}, {"reversed", "cancelled"})

    def test_new_unregistered_recipient_retains_only_the_corrected_share(self):
        from models import User
        self.paid_award()
        new_tg = F.next_tg()
        self.correct(shares={new_tg: "100"})
        self.assertEqual(self.balance(self.owners["Alpha"]), (0, 0))
        self.assertEqual(self.balance(self.coowners["Alpha"]), (0, 0))
        current = self.rows(1)
        self.assertEqual(len(current), 1)
        self.assertEqual((current[0].coins, current[0].gems, current[0].status),
                         (40_000, 40, "pending"))
        late = User(telegram_id=new_tg, username="new_corrected_recipient", total_coins=2)
        self.session.add(late)
        self.session.flush()
        self.P.retry_for_recipient(self.session, new_tg)
        self.session.commit()
        self.assertEqual(self.balance(late), (40_002, 40))
        self.assertEqual(sum(r.coins for r in self.rows() if r.status == "paid"), 40_000)

    def test_same_correction_request_is_idempotent_even_with_old_displayed_revision(self):
        self.paid_award()
        key = uuid.uuid4().hex
        first = self.correct(request_key=key, expected_revision=0)
        after = self.state()
        again = self.correct(request_key=key, expected_revision=0)
        self.assertEqual(again.id, first.id)
        self.assertEqual(self.state(), after)

    def test_a_new_request_using_stale_revision_cannot_reverse_the_replacement(self):
        self.paid_award()
        self.correct()
        before = self.state()
        with self.assertRaises(ValueError):
            self.correct(expected_revision=0, reason="Stale second browser tab")
        self.session.commit()
        self.session.expire_all()
        self.assertEqual(self.state(), before)

    def test_a_correction_key_cannot_be_reused_for_a_different_amount(self):
        self.paid_award()
        key = uuid.uuid4().hex
        self.correct(request_key=key, expected_revision=0)
        before = self.state()
        with self.assertRaises(ValueError):
            self.correct(request_key=key, expected_revision=0, coins=40_001)
        self.session.commit()
        self.session.expire_all()
        self.assertEqual(self.state(), before)

    def test_missing_reason_and_invalid_allocations_never_change_paid_money(self):
        self.paid_award()
        before = self.state()
        for changes in (
                {"reason": "  "}, {"coins": -1}, {"gems": -1},
                {"shares": {self.owners["Alpha"].telegram_id: "90"}},
                {"shares": {0: "100"}}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.correct(**changes)
                self.session.commit()
                self.session.expire_all()
                self.assertEqual(self.state(), before)

    def test_insufficient_previous_coin_or_gem_balance_blocks_the_entire_correction(self):
        self.paid_award()
        owner = self.owners["Alpha"]
        for coins, gems in ((29_999, 30), (30_000, 29)):
            with self.subTest(coins=coins, gems=gems):
                owner.total_coins, owner.total_gems = coins, gems
                self.session.commit()
                before = self.state()
                with self.assertRaises(ValueError):
                    self.correct()
                self.session.commit()
                self.session.expire_all()
                self.assertEqual(self.state(), before)

    def test_replacement_payment_failure_rolls_back_debits_receipts_and_correction(self):
        from services.activity_service import log_activity
        self.paid_award()
        before = self.state()

        def fail_replacement(session, uid, action, *args, **kwargs):
            if action == "tournament_prize":
                raise RuntimeError("replacement credit unavailable")
            return log_activity(session, uid, action, *args, **kwargs)

        with patch("services.activity_service.log_activity", side_effect=fail_replacement):
            with self.assertRaises((RuntimeError, ValueError)):
                self.correct()
        self.session.commit()
        self.session.expire_all()
        self.assertEqual(self.state(), before)

    def test_reversal_activity_failure_rolls_back_every_balance_and_audit_change(self):
        self.paid_award()
        before = self.state()
        with patch("services.activity_service.log_activity", side_effect=RuntimeError("reversal log unavailable")):
            with self.assertRaises((RuntimeError, ValueError)):
                self.correct()
        self.session.commit()
        self.session.expire_all()
        self.assertEqual(self.state(), before)

    def test_repeat_corrections_keep_all_history_but_only_the_latest_money_active(self):
        self.paid_award()
        first = self.correct()
        first_reversed = [(r.id, r.reversed_at, r.reversal_coins_after,
                           r.reversal_gems_after) for r in self.rows(0)]
        second = self.correct(coins=10_000, gems=10, reason="Second reviewed correction")
        for _ in range(2):
            self.P.retry_payouts(self.session, self.tour)
            self.P.retry_for_recipient(self.session, self.owners["Alpha"].telegram_id)
            self.session.commit()
        self.assertEqual(self.balance(self.owners["Alpha"]), (5_000, 5))
        self.assertEqual(self.balance(self.coowners["Alpha"]), (5_000, 5))
        self.assertEqual(self.honour.prize_revision, 2)
        self.assertEqual({r.status for r in self.rows(0)}, {"reversed"})
        self.assertEqual({r.status for r in self.rows(1)}, {"reversed"})
        self.assertEqual({r.status for r in self.rows(2)}, {"paid"})
        self.assertEqual([(r.id, r.reversed_at, r.reversal_coins_after,
                           r.reversal_gems_after) for r in self.rows(0)], first_reversed)
        self.assertEqual([c.id for c in self.corrections()], [first.id, second.id])
        self.assertEqual(self.P.payout_summary(self.session, self.tour.id)["paid_coins"], 10_000)

    def test_correcting_to_zero_removes_the_prize_without_a_future_retry_remint(self):
        self.paid_award()
        self.correct(coins=0, gems=0, reason="This award should not have a cash prize")
        self.P.retry_payouts(self.session, self.tour)
        self.session.commit()
        self.assertEqual(self.balance(self.owners["Alpha"]), (0, 0))
        self.assertEqual(self.balance(self.coowners["Alpha"]), (0, 0))
        self.assertEqual((self.honour.prize_snapshot_coins, self.honour.prize_snapshot_gems), (0, 0))
        self.assertEqual(sum(r.coins for r in self.rows(1)), 0)
        self.assertEqual({r.status for r in self.rows(0)}, {"reversed"})

    def test_ownerless_pending_prize_can_be_cancelled_without_assigning_recipients(self):
        self.teams["Alpha"].owner_tg_id = None
        self.session.commit()
        self.paid_award(coowner=False)
        original = self.rows(0)
        self.assertEqual(len(original), 1)
        self.assertEqual((original[0].recipient_tg_id, original[0].status), (0, "pending"))
        self.correct(coins=0, gems=0, reason="Cancel the ownerless prize")
        self.assertEqual(self.rows(1), [])
        self.assertEqual({row.status for row in self.rows(0)}, {"cancelled"})
        self.teams["Alpha"].owner_tg_id = self.owners["Alpha"].telegram_id
        self.session.commit()
        for _ in range(2):
            self.P.retry_payouts(self.session, self.tour)
            self.session.commit()
        self.assertEqual(self.rows(1), [])
        self.assertEqual(self.balance(self.owners["Alpha"]), (0, 0))
        self.assertEqual((self.honour.prize_snapshot_coins, self.honour.prize_snapshot_gems), (0, 0))

    def test_legacy_paid_award_can_be_reversed_once_and_has_original_receipt_history(self):
        from models import TournamentPrizePayout
        self.paid_award(coowner=False)
        self.session.query(TournamentPrizePayout).filter_by(honour_id=self.honour.id).delete()
        self.honour.prize_snapshot_coins = self.honour.prize_snapshot_gems = None
        self.session.commit()
        self.correct(coins=30_000, gems=30)
        self.assertEqual(self.balance(self.owners["Alpha"]), (30_000, 30))
        old, new = self.rows(0), self.rows(1)
        self.assertEqual(len(old), 1)
        self.assertEqual((old[0].coins, old[0].gems, old[0].status), (60_000, 60, "reversed"))
        self.assertEqual((new[0].coins, new[0].gems, new[0].status), (30_000, 30, "paid"))

    def test_concurrent_duplicate_submissions_perform_only_one_reversal_and_credit(self):
        from database import get_session
        from models import Tournament
        self.paid_award()
        key, tid, hid = uuid.uuid4().hex, self.tour.id, self.honour.id
        barrier = Barrier(2)

        def submit():
            session = get_session()
            try:
                tour = session.get(Tournament, tid)
                barrier.wait(timeout=10)
                correction = self.C.correct_award(
                    session, tour, hid, coins=40_000, gems=40,
                    reason="Duplicate browser submission", request_key=key,
                    expected_revision=0, actor="test-admin")
                session.commit()
                return correction.id
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(results[0], results[1])
        self.session.expire_all()
        self.assertEqual(len(self.corrections()), 1)
        self.assertEqual(self.honour.prize_revision, 1)
        self.assertEqual(self.balance(self.owners["Alpha"]), (20_000, 20))
        self.assertEqual(self.balance(self.coowners["Alpha"]), (20_000, 20))
        self.assertEqual(len(self.rows()), 4)

    def test_recipient_text_parser_rejects_bad_percentages_and_duplicate_ids(self):
        tg1, tg2 = F.next_tg(), F.next_tg()
        parsed = self.C.parse_shares(f"{tg1}:50\n{tg2}:50")
        self.assertEqual(set(map(int, parsed)), {tg1, tg2})
        for raw in (f"{tg1}:50\n{tg1}:50", f"{tg1}:90", "0:100", f"{tg1}:NaN",
                    f"{tg1}:50.001\n{tg2}:49.999", f"{tg1}:0\n{tg2}:100"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    self.C.parse_shares(raw)

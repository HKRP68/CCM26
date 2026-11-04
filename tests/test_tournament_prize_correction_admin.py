"""Authenticated admin prize corrections reverse old credits before re-awarding."""

import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap
import _tour_fixture as F

_EXTRA_MODULES = ("admin", "handlers.tournament_admin")
_SAVED_MODULES = {}


def setUpModule():
    global _SAVED_MODULES
    os.environ.setdefault("BOT_TOKEN", "test-token")
    os.environ.setdefault("ADMIN_PASSWORD", "test")
    os.environ.setdefault("ADMIN_USERNAME", "admin")
    _SAVED_MODULES = _module_swap.save(_EXTRA_MODULES)
    _module_swap.unload(_EXTRA_MODULES)
    F.setup_module_db()


def tearDownModule():
    F.teardown_module_db()
    _module_swap.restore(_SAVED_MODULES)


class PrizeCorrectionAdminTests(F.FourTeamCase):
    @classmethod
    def setUpClass(cls):
        import admin
        admin.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        cls.admin = admin

    def setUp(self):
        super().setUp()
        from models import TournamentHonour, TournamentMatch, User
        from services import tournament_awards
        self.coowners = {}
        for name, team in self.teams.items():
            coowner = User(telegram_id=F.next_tg(), username=f"correct_admin_co_{name}")
            self.session.add(coowner)
            team.co_owner_ids_json = json.dumps([coowner.telegram_id])
            self.coowners[name] = coowner
        tournament_awards.set_prize(self.tour, "champion", 60_000, 60)
        self.session.add(TournamentMatch(
            tournament_id=self.tour.id, stage="final", status="completed",
            team1_id=self.teams["Alpha"].id, team2_id=self.teams["Bravo"].id,
            winner_team_id=self.teams["Alpha"].id, result_type="win"))
        self.session.flush()
        tournament_awards.finalize(self.session, self.tour)
        self.session.commit()
        self.honour = self.session.query(TournamentHonour).filter_by(
            tournament_id=self.tour.id, award="champion").one()
        self.client = self.admin.app.test_client()
        with self.client.session_transaction() as flask_session:
            flask_session["admin"] = True
            flask_session["admin_user"] = "prize-reviewer"

    def detail(self, data=None, query=""):
        url = f"/tournaments/{self.tour.id}{query}"
        response = (self.client.post(url, data=data, follow_redirects=True)
                    if data is not None else self.client.get(url))
        self.assertEqual(response.status_code, 200)
        self.session.expire_all()
        return response.get_data(as_text=True)

    def payload(self, **changes):
        fields = dict(action="correct_prize", honour_id=str(self.honour.id),
                      coins="40000", gems="40", reason="Fix the mistaken prize amount",
                      request_key=uuid.uuid4().hex,
                      expected_revision=str(self.honour.prize_revision),
                      team_id="", player_name="", recipient_shares="")
        fields.update(changes)
        return fields

    def balance(self, owner):
        self.session.refresh(owner)
        return owner.total_coins, owner.total_gems

    def rows(self, revision=None):
        from models import TournamentPrizePayout
        query = self.session.query(TournamentPrizePayout).filter_by(honour_id=self.honour.id)
        if revision is not None:
            query = query.filter_by(revision=revision)
        return query.order_by(TournamentPrizePayout.id).all()

    def correction_count(self):
        from models import TournamentPrizeCorrection
        return self.session.query(TournamentPrizeCorrection).filter_by(
            tournament_id=self.tour.id).count()

    def reopen(self):
        return self.detail({"action": "reopen_prize_split", "team_id": str(self.teams["Alpha"].id)})

    def redistribution(self, **changes):
        team = self.teams["Alpha"]
        fields = dict(
            action="redistribute_prizes", team_id=str(team.id), split_mode="custom",
            reason="Correct the agreed management percentages", request_key=uuid.uuid4().hex,
            expected_snapshot=team.prize_split_snapshot_json,
            **{f"share_{self.owners['Alpha'].telegram_id}": "70",
               f"share_{self.coowners['Alpha'].telegram_id}": "30"})
        fields.update(changes)
        return fields

    def test_manage_shows_correction_form_with_revision_and_duplicate_guard(self):
        body = self.detail()
        self.assertIn("Correct a prize", body)
        self.assertIn("Reverse &amp; re-award", body)
        self.assertIn('name="action" value="correct_prize"', body)
        self.assertIn('name="request_key"', body)
        self.assertIn('name="expected_revision" value="0"', body)
        self.assertIn('name="reason"', body)
        self.assertIn('name="recipient_shares"', body)
        self.assertIn("no balances change", body)

    def test_corrected_amount_debits_previous_shares_and_keeps_reversal_history(self):
        body = self.detail(self.payload())
        self.assertIn("Previous prize credits reversed", body)
        self.assertIn("Correction history", body)
        self.assertIn("Fix the mistaken prize amount", body)
        self.assertIn("web:prize-reviewer", body)
        self.assertEqual(self.balance(self.owners["Alpha"]), (20_000, 20))
        self.assertEqual(self.balance(self.coowners["Alpha"]), (20_000, 20))
        self.assertEqual(self.honour.prize_revision, 1)
        self.assertEqual({r.status for r in self.rows(0)}, {"reversed"})
        self.assertEqual({r.status for r in self.rows(1)}, {"paid"})
        body = self.detail(query="?payout_status=reversed")
        receipts = body.split('id="prize-payouts"', 1)[1].split("</section>", 1)[0]
        self.assertIn("Reversed 2", receipts)
        self.assertIn("After reversal: 0 coins / 0 gems", receipts)

    def test_corrected_team_receives_new_prize_using_its_split(self):
        body = self.detail(self.payload(team_id=str(self.teams["Bravo"].id),
                                        coins="50000", gems="50"))
        self.assertIn("Previous prize credits reversed", body)
        self.assertEqual(self.balance(self.owners["Alpha"]), (0, 0))
        self.assertEqual(self.balance(self.coowners["Alpha"]), (0, 0))
        self.assertEqual(self.balance(self.owners["Bravo"]), (25_000, 25))
        self.assertEqual(self.balance(self.coowners["Bravo"]), (25_000, 25))
        self.assertEqual(self.honour.tournament_team_id, self.teams["Bravo"].id)

    def test_explicit_recipient_override_is_parsed_and_credited(self):
        from models import User
        recipient = User(telegram_id=F.next_tg(), username="correct_http_recipient",
                         total_coins=7, total_gems=3)
        self.session.add(recipient)
        self.session.commit()
        body = self.detail(self.payload(recipient_shares=f"{recipient.telegram_id}:100"))
        self.assertIn("Previous prize credits reversed", body)
        self.assertEqual(self.balance(recipient), (40_007, 43))
        self.assertEqual(self.balance(self.owners["Alpha"]), (0, 0))
        self.assertEqual(self.balance(self.coowners["Alpha"]), (0, 0))
        self.assertEqual({row.recipient_tg_id for row in self.rows(1)}, {recipient.telegram_id})

    def test_missing_reason_and_invalid_recipient_total_leave_balances_unchanged(self):
        body = self.detail(self.payload(reason=""))
        self.assertIn("reason", body.lower())
        self.assertEqual(self.correction_count(), 0)
        body = self.detail(self.payload(recipient_shares=f"{self.owners['Alpha'].telegram_id}:90"))
        self.assertIn("100", body)
        self.assertEqual(self.correction_count(), 0)
        self.assertEqual(self.balance(self.owners["Alpha"]), (30_000, 30))
        self.assertEqual(self.balance(self.coowners["Alpha"]), (30_000, 30))
        self.assertEqual({row.status for row in self.rows()}, {"paid"})

    def test_insufficient_old_balance_blocks_entire_correction(self):
        owner = self.owners["Alpha"]
        owner.total_coins = 1
        self.session.commit()
        body = self.detail(self.payload())
        self.assertNotIn("Previous prize credits reversed", body)
        self.assertEqual(self.balance(owner), (1, 30))
        self.assertEqual(self.balance(self.coowners["Alpha"]), (30_000, 30))
        self.assertEqual(self.honour.prize_revision, 0)
        self.assertEqual(self.correction_count(), 0)
        self.assertEqual({row.status for row in self.rows()}, {"paid"})

    def test_honour_in_another_tournament_cannot_be_corrected(self):
        from models import Tournament, TournamentHonour
        other = Tournament(name="Foreign correction cup", status="completed")
        self.session.add(other)
        self.session.flush()
        foreign = TournamentHonour(tournament_id=other.id, tournament_name=other.name,
                                   award="champion", team_name="Other team")
        self.session.add(foreign)
        self.session.commit()
        body = self.detail(self.payload(honour_id=str(foreign.id)))
        self.assertNotIn("Previous prize credits reversed", body)
        self.assertEqual(self.correction_count(), 0)
        self.assertEqual(self.balance(self.owners["Alpha"]), (30_000, 30))
        self.assertEqual(self.balance(self.coowners["Alpha"]), (30_000, 30))

    def test_duplicate_post_is_idempotent_and_stale_new_post_is_rejected(self):
        fields = self.payload()
        for _ in range(2):
            body = self.detail(fields)
            self.assertIn("Previous prize credits reversed", body)
        self.assertEqual(self.correction_count(), 1)
        self.assertEqual(self.balance(self.owners["Alpha"]), (20_000, 20))
        self.assertEqual(self.balance(self.coowners["Alpha"]), (20_000, 20))
        body = self.detail(self.payload(expected_revision="0", coins="30000"))
        self.assertNotIn("Previous prize credits reversed", body)
        self.assertEqual(self.correction_count(), 1)
        self.assertEqual(self.honour.prize_revision, 1)
        self.assertEqual(self.balance(self.owners["Alpha"]), (20_000, 20))

    def test_reopen_percentages_changes_only_editor_and_keeps_paid_shares(self):
        team = self.teams["Alpha"]
        original_snapshot = team.prize_split_snapshot_json
        original_rows = [(r.id, r.revision, r.status, r.coins, r.gems) for r in self.rows()]
        body = self.reopen()
        self.assertIn("Editor reopened", body)
        self.assertIn("Apply redistribution &amp; lock", body)
        self.assertIn('name="action" value="redistribute_prizes"', body)
        self.assertIn('name="expected_snapshot"', body)
        self.assertIsNotNone(team.prize_split_reopened_at)
        self.assertEqual(team.prize_split_snapshot_json, original_snapshot)
        self.assertEqual([(r.id, r.revision, r.status, r.coins, r.gems) for r in self.rows()], original_rows)
        self.assertEqual(self.balance(self.owners["Alpha"]), (30_000, 30))
        self.assertEqual(self.balance(self.coowners["Alpha"]), (30_000, 30))

    def test_apply_redistribution_changes_percentages_and_relocks_without_duplicate_payment(self):
        team = self.teams["Alpha"]
        self.reopen()
        fields = self.redistribution()
        for _ in range(2):
            body = self.detail(fields)
            self.assertIn("Prize percentages updated and locked", body)
        self.assertIsNone(team.prize_split_reopened_at)
        self.assertEqual(self.balance(self.owners["Alpha"]), (42_000, 42))
        self.assertEqual(self.balance(self.coowners["Alpha"]), (18_000, 18))
        self.assertEqual(self.honour.prize_revision, 1)
        self.assertEqual(self.correction_count(), 1)
        self.assertEqual({r.status for r in self.rows(0)}, {"reversed"})
        self.assertEqual({r.status for r in self.rows(1)}, {"paid"})
        self.assertIn("Reopen percentages", body)
        self.assertNotIn("Editor reopened", body)
        self.assertIn("Redistribution history", body)
        self.assertIn("Correct the agreed management percentages", body)
        self.assertIn("web:prize-reviewer", body)

    def test_invalid_or_unrecoverable_redistribution_keeps_editor_open_and_money_unchanged(self):
        team = self.teams["Alpha"]
        self.reopen()
        body = self.detail(self.redistribution(reason=""))
        self.assertNotIn("Prize percentages updated and locked", body)
        owner = self.owners["Alpha"]
        fields = self.redistribution()
        fields[f"share_{owner.telegram_id}"] = "60"
        body = self.detail(fields)
        self.assertNotIn("Prize percentages updated and locked", body)
        self.assertEqual(self.balance(owner), (30_000, 30))
        owner.total_coins = 1
        self.session.commit()
        body = self.detail(self.redistribution())
        self.assertNotIn("Prize percentages updated and locked", body)
        self.assertIsNotNone(team.prize_split_reopened_at)
        self.assertEqual(self.balance(owner), (1, 30))
        self.assertEqual(self.balance(self.coowners["Alpha"]), (30_000, 30))
        self.assertEqual(self.honour.prize_revision, 0)
        self.assertEqual(self.correction_count(), 0)

    def test_reopened_stale_snapshot_form_does_not_reverse_corrected_receipts(self):
        self.reopen()
        old_fields = self.redistribution()
        self.detail(old_fields)
        self.reopen()
        old_fields["request_key"] = uuid.uuid4().hex
        body = self.detail(old_fields)
        self.assertNotIn("Prize percentages updated and locked", body)
        self.assertIsNotNone(self.teams["Alpha"].prize_split_reopened_at)
        self.assertEqual(self.balance(self.owners["Alpha"]), (42_000, 42))
        self.assertEqual(self.balance(self.coowners["Alpha"]), (18_000, 18))
        self.assertEqual(self.honour.prize_revision, 1)

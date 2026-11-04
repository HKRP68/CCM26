"""Prize sharing and recipient receipts through the authenticated admin UI."""

import json
import os
import sys
from unittest.mock import patch

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


class PrizeAdminTests(F.FourTeamCase):
    @classmethod
    def setUpClass(cls):
        import admin
        admin.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        cls.admin = admin

    def setUp(self):
        super().setUp()
        self.client = self.admin.app.test_client()
        with self.client.session_transaction() as flask_session:
            flask_session["admin"] = True

    def detail(self, data=None, query=""):
        url = f"/tournaments/{self.tour.id}{query}"
        response = (self.client.post(url, data=data, follow_redirects=True)
                    if data else self.client.get(url))
        self.assertEqual(response.status_code, 200)
        self.session.expire_all()
        return response.get_data(as_text=True)

    def add_coowner(self, team, registered=True):
        from models import User
        tg = F.next_tg()
        team.co_owner_ids_json = json.dumps([tg])
        if registered:
            user = User(telegram_id=tg, username=f"prize_coowner_{tg}")
            self.session.add(user)
        self.session.commit()
        return tg

    def set_custom(self, team, owner_share="60", coowner_share="40"):
        from services.tournament_service import co_owner_ids
        return self.detail({
            "action": "set_prize_split", "team_id": str(team.id),
            "split_mode": "custom", f"share_{team.owner_tg_id}": owner_share,
            f"share_{co_owner_ids(team)[0]}": coowner_share,
        })

    def test_manage_displays_all_prize_categories_and_sharing_controls(self):
        team = self.teams["Alpha"]
        self.add_coowner(team)
        body = self.detail()
        self.assertIn('name="prize_champion_coins"', body)
        self.assertIn('name="prize_runner_up_coins"', body)
        self.assertIn('id="prize-sharing"', body)
        self.assertIn('id="prize-payouts"', body)
        self.assertIn("60K Champion + 30K Orange Cap = 45K each", body)
        self.assertIn("Equal shares (default)", body)
        self.assertIn("Retry pending and failed payouts", body)

    def test_custom_percentage_post_is_saved_and_invalid_total_is_rejected(self):
        from services import tournament_prize_splits as splits
        team = self.teams["Alpha"]
        coowner_tg = self.add_coowner(team)
        body = self.set_custom(team)
        self.assertIn("Prize sharing saved for Alpha", body)
        settings = splits.split_settings(self.session, self.tour, team)
        self.assertEqual(settings["percentages"], {
            str(team.owner_tg_id): "60.00", str(coowner_tg): "40.00"})
        self.assertFalse(settings["locked"])

        body = self.set_custom(team, "60", "30")
        self.assertIn("Prize percentages must total 100%", body)
        settings = splits.split_settings(self.session, self.tour, team)
        self.assertEqual(settings["percentages"][str(coowner_tg)], "40.00")

        self.detail({"action": "set_prize_split", "team_id": str(team.id),
                     "split_mode": "equal"})
        settings = splits.split_settings(self.session, self.tour, team)
        self.assertFalse(settings["custom"])
        self.assertEqual(set(settings["percentages"].values()), {"50.00"})

    def test_started_tournament_rejects_split_changes_and_keeps_the_snapshot(self):
        from services import tournament_prize_splits as splits
        team = self.teams["Alpha"]
        self.add_coowner(team)
        self.set_custom(team, "70", "30")
        self.detail({"action": "start"})
        before = splits.split_settings(self.session, self.tour, team)
        self.assertTrue(before["locked"])
        self.assertIsNotNone(team.prize_split_locked_at)

        body = self.set_custom(team, "20", "80")
        self.assertIn("locked", body.lower())
        after = splits.split_settings(self.session, self.tour, team)
        self.assertEqual(after["percentages"], before["percentages"])
        self.assertEqual(team.prize_split_locked_at, before["locked_at"])

    def test_retry_pays_late_registered_recipient_and_updates_filtered_receipts(self):
        from models import TournamentTeam, User
        from services import tournament_awards as awards, tournament_payouts as payouts
        coowners = {team.name: self.add_coowner(team, registered=False)
                    for team in self.teams.values()}
        awards.set_prize(self.tour, "champion", 60_000, 60)
        self.session.commit()
        self.finish_league()
        for fixture in self.fixtures(stage="semifinal"):
            self.ts.simulate_fixture(self.session, fixture.id, "1")
        final = self.fixtures(stage="final")[0]
        self.ts.simulate_fixture(self.session, final.id, "1")
        self.session.commit()
        champion = self.session.get(TournamentTeam, final.winner_team_id)
        rows = payouts.payout_rows(self.session, self.tour.id)
        self.assertEqual({row.status for row in rows}, {"paid", "pending"})
        pending = next(row for row in rows if row.status == "pending")
        paid = next(row for row in rows if row.status == "paid")

        body = self.detail(query="?payout_status=pending")
        receipts = body.split('id="prize-payouts"', 1)[1].split("</section>", 1)[0]
        self.assertIn(f"#{pending.id}", receipts)
        self.assertNotIn(f"#{paid.id}<", receipts)
        self.assertIn("30,000 coins", receipts)
        self.assertIn("Recipient must register with /debut", receipts)

        late = User(telegram_id=coowners[champion.name], username="late_prize_admin_coowner",
                    total_coins=17, total_gems=2)
        self.session.add(late)
        self.session.commit()
        with patch("services.activity_service.log_activity", side_effect=RuntimeError("receipt unavailable")), \
                self.assertLogs("services.tournament_payouts", level="ERROR"):
            self.detail({"action": "retry_prize_payouts"})
        self.session.refresh(late)
        self.assertEqual((late.total_coins, late.total_gems), (17, 2))
        body = self.detail(query="?payout_status=failed")
        receipts = body.split('id="prize-payouts"', 1)[1].split("</section>", 1)[0]
        self.assertIn("Failed 1", receipts)
        self.assertIn("receipt unavailable", receipts)
        self.assertIn(f"#{pending.id}", receipts)

        body = self.detail({"action": "retry_prize_payouts"})
        self.session.refresh(late)
        self.assertEqual((late.total_coins, late.total_gems), (30_017, 32))
        self.assertIn("Payout retry complete", body)
        self.assertIn("Paid 2", body)
        self.assertIn("Pending 0", body)
        self.assertIn("late_prize_admin_coowner", body)
        self.assertIn("30,017 coins", body)
        self.assertIn("Balance after payment", body)

        self.detail({"action": "retry_prize_payouts"})
        self.session.refresh(late)
        self.assertEqual((late.total_coins, late.total_gems), (30_017, 32))
        self.assertEqual(len(payouts.payout_rows(self.session, self.tour.id)), 2)

    def test_post_cannot_change_a_team_in_another_tournament(self):
        from models import Tournament, TournamentTeam
        other = Tournament(name="Other prize cup", status="draft")
        self.session.add(other)
        self.session.flush()
        foreign = TournamentTeam(tournament_id=other.id, name="Foreign team",
                                 owner_tg_id=F.next_tg())
        self.session.add(foreign)
        self.session.commit()
        body = self.detail({"action": "set_prize_split", "team_id": str(foreign.id),
                            "split_mode": "equal"})
        self.assertIn("Team not found in this tournament", body)
        self.session.refresh(foreign)
        self.assertIsNone(foreign.prize_split_json)

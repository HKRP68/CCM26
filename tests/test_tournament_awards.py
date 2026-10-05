"""Prizes, honours and the ceremony when a final is decided."""

import json
import os
import sys
from datetime import datetime, timedelta

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
        self.assertEqual(star.total_coins, 700)

    def test_the_ceremony_is_posted_once(self):
        self._play_out()
        posts = [p for p in self.tw.tick(self.session, self.tour,
                                         now=self.now + timedelta(hours=1))
                 if p.kind == "ceremony"]
        self.assertEqual(len(posts), 1)
        self.assertIn("Awards Ceremony", posts[0].text)
        self.assertIn("5,000 coins", posts[0].text)
        self.assertEqual([p for p in self.tw.tick(self.session, self.tour,
                                                  now=self.now + timedelta(hours=2))
                          if p.kind == "ceremony"], [])

    def test_a_tournament_completed_right_after_the_final_still_gets_it(self):
        self._play_out()
        self.tour.status = "completed"
        self.session.commit()
        ids = [t.id for t in self.tw.running_tournaments(self.session)]
        self.assertIn(self.tour.id, ids)
        posts = self.tw.tick(self.session, self.tour, now=self.now + timedelta(hours=1))
        self.assertEqual([p.kind for p in posts if p.kind == "ceremony"], ["ceremony"])

    def test_the_hall_of_fame_lists_the_honours(self):
        from handlers.halloffame import render_section
        self._play_out()
        text = render_section(self.session, "tour")
        self.assertIn("Watch Cup", text)
        self.assertIn("Champion", text)

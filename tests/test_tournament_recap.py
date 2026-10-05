"""Round recaps, head-to-head tiebreak and the tracker against a real schedule."""

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


class RecapTests(F.FourTeamCase):
    def setUp(self):
        super().setUp()
        from services import tournament_watch
        self.tw = tournament_watch
        self.now = datetime(2026, 10, 5, 12, 0)
        self.tw.tick(self.session, self.tour, now=self.now)   # first look

    def recaps(self):
        return [p for p in self.tw.tick(self.session, self.tour,
                                        now=self.now + timedelta(minutes=5))
                if p.kind == "recap"]

    def test_one_recap_per_finished_round(self):
        self.assertEqual(self.recaps(), [])
        self.finish_round(1)
        posts = self.recaps()
        self.assertEqual(len(posts), 1)
        text = posts[0].text
        self.assertIn("Round 1 recap", text)
        self.assertIn("Round 2 — up next", text)
        self.assertIn("(simulated)", text)
        self.assertEqual(posts[0].chat_id, -100555)
        self.assertEqual(self.recaps(), [])                      # never twice

    def test_the_last_round_announces_the_playoffs(self):
        self.finish_round(1)
        self.recaps()
        self.finish_round(2)
        self.recaps()
        self.finish_round(3)
        text = self.recaps()[0].text
        self.assertIn("Playoffs are set", text)

    def test_an_existing_season_is_not_flooded(self):
        from models import Tournament
        fresh = Tournament(name="Old", status="active", kind="challenge",
                           league_format="single_rr", overs=20, max_teams=8)
        self.session.add(fresh)
        self.session.flush()
        self.assertEqual(self.tw.tick(self.session, fresh, now=self.now), [])

    def test_player_of_the_round_is_the_top_impact(self):
        from services import tournament_recap
        fx = self.fixtures(1)[0]
        lines = [
            {"name": "Big Hitter", "team_name": "Alpha", "user_id": 1,
             "player_id": 11, "batted": True, "bat_runs": 90, "bat_balls": 40,
             "bat_fours": 8, "bat_sixes": 5},
            {"name": "Tidy Bowler", "team_name": "Delta", "user_id": 2,
             "player_id": 12, "bowled": True, "bowl_wickets": 1,
             "bowl_runs": 30, "bowl_balls": 24},
        ]
        fx.scorecard_json = json.dumps(lines)
        self.session.flush()
        star = tournament_recap.player_of_round(self.session, self.tour, 1)
        self.assertEqual(star[0], "Big Hitter")
        self.assertIn("90 (40)", star[3])

    def test_movers(self):
        from services import tournament_recap
        self.finish_round(1, outcome="2")
        moved = tournament_recap.movers(self.session, self.tour, 1)
        self.assertTrue(moved)
        for old, new in moved.values():
            self.assertNotEqual(old, new)


class TiebreakTests(F.FourTeamCase):
    KNOCKOUT = None

    def _level(self):
        """Alpha and Bravo level on points; Bravo won their meeting, Alpha has
        the better NRR."""
        from models import TournamentMatch
        A, B, C, D = (self.teams[n].id for n in ("Alpha", "Bravo", "Charlie", "Delta"))
        self.session.query(TournamentMatch).filter_by(
            tournament_id=self.tour.id).delete()
        rows = [(B, A, 150, 149, B), (A, C, 250, 100, A), (C, B, 160, 150, C),
                (C, D, 200, 100, C)]
        for t1, t2, r1, r2, win in rows:
            self.session.add(TournamentMatch(
                tournament_id=self.tour.id, team1_id=t1, team2_id=t2,
                stage="league", status="completed", round_no=1,
                inn1_runs=r1, inn1_wickets=5, inn1_balls=120,
                inn2_runs=r2, inn2_wickets=8, inn2_balls=120, winner_team_id=win))
        self.session.flush()
        self.ts.recompute_standings(self.session, self.tour.id)
        return A, B

    def test_nrr_mode_keeps_wins_then_nrr(self):
        A, B = self._level()
        table = [t.id for t in self.ts.points_table(self.session, self.tour.id)]
        self.assertLess(table.index(A), table.index(B))

    def test_h2h_mode_puts_the_head_to_head_winner_first(self):
        A, B = self._level()
        self.tour.tiebreak = "h2h"
        self.session.flush()
        table = [t.id for t in self.ts.points_table(self.session, self.tour.id)]
        self.assertLess(table.index(B), table.index(A))


class TrackerDbTests(F.FourTeamCase):
    KNOCKOUT = "top4_sf"

    def test_marks_appear_once_decided(self):
        from services import qualification
        self.tour.knockout_type = "top4_sf"
        self.session.flush()
        # Four teams, top four go through: nobody can miss out → no tracker.
        self.assertEqual(qualification.tracker(self.session, self.tour), {})
        self.tour.knockout_type = "ipl_playoffs"
        self.session.flush()
        self.assertEqual(qualification.tracker(self.session, self.tour), {})

    def test_the_table_carries_the_legend_when_marked(self):
        from services import cl_tournament_view as ctv
        from services import qualification
        from unittest import mock
        from types import SimpleNamespace
        alpha = self.teams["Alpha"].id
        fake = {alpha: SimpleNamespace(mark="Q", wins_to_be_sure=0, max_points=6)}
        with mock.patch.object(qualification, "table_marks", return_value=(fake, 2)):
            text = ctv.render_table(self.session, self.tour)
        self.assertIn("Q = through to the playoffs", text)

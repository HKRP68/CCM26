"""Every record tile opens its full ranked, filtered and paginated board."""
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_league_seasons_page as pages
setUpModule = pages.setUpModule
tearDownModule = pages.tearDownModule


class RecordBrowserTests(pages.PageCase):
    def setUp(self):
        super().setUp()
        from models import TournamentMatch, TournamentTeam
        from services import league_season_service as LS, league_records as R, cricket_portal_cache
        cricket_portal_cache.invalidate()
        self.series = LS.create_series(self.session, f'Browser Cup {self.tag}')
        self.season = LS.attach_season(self.session, self.series, tournament=self.tour)
        self.a, self.b = self.session.query(TournamentTeam).filter_by(tournament_id=self.tour.id).all()
        lines = [dict(name=name, team_name=self.a.name, batted=True, bat_runs=runs,
                      bat_balls=40, bat_out=True, bat_fours=2, bat_sixes=1, bowled=True,
                      bowl_wickets=2, bowl_runs=12, bowl_balls=12)
                 for name, runs in [('Alpha',80),('Beta',80),('Gamma',60)]]
        self.session.add(TournamentMatch(tournament_id=self.tour.id, team1_id=self.a.id,
            team2_id=self.b.id, status='completed', stage='league', inn1_runs=220,
            inn2_runs=200, winner_team_id=self.a.id, scorecard_json=json.dumps(lines)))
        R.save_config(self.session, self.season, 30, 12)
        self.session.commit()
        self.base = f'/api/stats/{self.series.id}/leaderboard'
        with self.client.session_transaction() as s:
            s.clear()

    def test_full_board_keeps_exact_ties_and_original_ranks_when_searching(self):
        response = self.client.get(self.base + '?category=runs&limit=2')
        self.assertEqual(200, response.status_code)
        self.assertEqual('Most runs', response.json['title'])
        self.assertEqual([1,1], [r['rank'] for r in response.json['rows']])
        self.assertEqual(3, response.json['total'])
        page2 = self.client.get(self.base + '?category=runs&limit=2&page=2').json
        self.assertEqual(('Gamma',3), (page2['rows'][0]['name'], page2['rows'][0]['rank']))
        searched = self.client.get(self.base + '?category=runs&q=gamma').json
        self.assertEqual(1, searched['total'])
        self.assertEqual(3, searched['rows'][0]['rank'])

    def test_pages_and_categories_reuse_one_historical_build(self):
        from services import league_records as R
        with patch.object(R, 'boards', wraps=R.boards) as build:
            self.client.get(self.base + '?category=runs&limit=2')
            self.client.get(self.base + '?category=runs&limit=2&page=2')
            response = self.client.get(self.base + '?category=econ')
        self.assertEqual(1, build.call_count)
        self.assertIn('12', response.json['qualification'])
        self.assertEqual('6.00', response.json['rows'][0]['display'])

    def test_unknown_category_and_inactive_competition_are_rejected(self):
        self.assertEqual(400, self.client.get(self.base + '?category=secret').status_code)
        self.series.is_active = False
        self.session.commit()
        self.assertEqual(404, self.client.get(self.base + '?category=runs').status_code)

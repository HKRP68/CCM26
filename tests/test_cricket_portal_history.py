"""All seasons, honest comparisons and available public artwork."""
import json
import os
import sys
from datetime import datetime
from io import BytesIO
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_league_seasons_page as pages
setUpModule = pages.setUpModule
tearDownModule = pages.tearDownModule


class HistoryTests(pages.PageCase):
    def setUp(self):
        super().setUp()
        from models import TournamentTeam
        from services import league_season_service as LS, cricket_portal_cache
        cricket_portal_cache.invalidate()
        self.series = LS.create_series(self.session, f'History Cup {self.tag}')
        self.season = LS.attach_season(self.session, self.series, tournament=self.tour)
        self.a, self.b = self.session.query(TournamentTeam).filter_by(tournament_id=self.tour.id).all()
        self.session.commit()
        with self.client.session_transaction() as state:
            state.clear()
        self.base = f'/api/stats/{self.series.id}'

    def game(self, winner=None, result_type='win', duels=False):
        from models import TournamentMatch
        lines = [dict(name='Alpha', team_name=self.a.name, batted=True, bat_runs=60, bat_balls=30,
                      bat_out=False, bat_fours=4, bat_sixes=2, bowled=False),
                 dict(name='Beta', team_name=self.b.name, batted=True, bat_runs=20, bat_balls=20,
                      bat_out=True, bat_fours=1, bat_sixes=0, bowled=True, bowl_wickets=1,
                      bowl_runs=15, bowl_balls=12)]
        if duels:
            lines[0]['player_duels'] = [dict(batter={'name':'Alpha','team':self.a.name,'roster_id':7},
                bowler={'name':'Beta','team':self.b.name,'roster_id':8}, runs=12, balls=5,
                legal_balls=4, dots=1, fours=1, sixes=1, dismissals=1)]
        row = TournamentMatch(tournament_id=self.tour.id, team1_id=self.a.id, team2_id=self.b.id,
            winner_team_id=winner, status='completed', result_type=result_type,
            inn1_runs=150, inn2_runs=140, inn1_wickets=5, inn2_wickets=8,
            completed_at=datetime(2026, 1, 1), scorecard_json=json.dumps(lines))
        self.session.add(row)
        self.session.commit()
        return row

    def test_default_scope_and_season_archive_include_every_season(self):
        from models import LeagueSeason
        from services import cricket_portal_history as H
        self.game(winner=self.a.id)
        other = LeagueSeason(series_id=self.series.id, season_no=2, name='Second chapter')
        self.session.add(other)
        self.session.commit()
        page = self.client.get(f'/stats/{self.series.id}')
        self.assertIn('data-stats-season="all"', page.text)
        data = H.seasons_snapshot(self.session, self.series)
        self.assertEqual(2, data['total'])
        self.assertEqual(other.id, data['seasons'][0]['id'])
        old = next(s for s in data['seasons'] if s['id'] == self.season.id)
        self.assertEqual(1, old['matches'])
        self.assertTrue(old['records_url'].endswith(f'season={self.season.id}#records'))
        self.assertEqual(200, self.client.get(self.base + '/seasons').status_code)

    def test_team_comparison_counts_recorded_results_and_validates_sides(self):
        self.game(winner=self.a.id)
        self.game(winner=self.b.id)
        self.game(result_type='tie')
        self.game(result_type='no_result')
        result = self.client.get(self.base + '/compare', query_string=dict(mode='team', left=self.a.name, right=self.b.name))
        self.assertEqual(200, result.status_code)
        self.assertEqual((4, 1, 1, 1, 1), (result.json['matches'], result.json['left']['stats']['won'],
            result.json['right']['stats']['won'], result.json['tied'], result.json['no_result']))
        self.assertEqual(400, self.client.get(self.base + '/compare', query_string=dict(mode='team', left=self.a.name, right=self.a.name)).status_code)
        self.assertEqual(400, self.client.get(self.base + '/compare?mode=player').status_code)

    def test_player_comparison_has_totals_and_only_saved_direct_encounters(self):
        self.game(winner=self.a.id, duels=True)
        self.game(winner=self.b.id)
        response = self.client.get(self.base + '/compare?mode=player&left=Alpha&right=Beta')
        self.assertEqual(200, response.status_code)
        data = response.json
        self.assertEqual(120, data['left']['selected']['runs'])
        self.assertEqual(40, data['right']['selected']['runs'])
        self.assertEqual(2, data['shared_matches'])
        self.assertEqual(1, len(data['duels']))
        self.assertEqual((12, 5, 1), (data['duels'][0]['runs'], data['duels'][0]['balls'], data['duels'][0]['dismissals']))
        self.assertNotIn('roster_id', response.text)

    def test_preserved_archived_season_keeps_comparison_evidence(self):
        from services import league_records as R
        self.game(winner=self.a.id)
        R.preserve(self.session, self.season)
        self.season.tournament_id = None
        self.session.commit()
        response = self.client.get(self.base + '/compare', query_string=dict(mode='team', left=self.a.name, right=self.b.name))
        self.assertEqual(1, response.json['matches'])
        self.assertEqual(1, response.json['left']['stats']['won'])
        archive = self.client.get(self.base + '/seasons').json['seasons'][0]
        self.assertTrue(archive['archived'])
        self.assertEqual(1, archive['matches'])

    def test_saved_fixture_manifest_retains_no_results_and_simulated_meetings(self):
        from models import StatsSeason
        saved = StatsSeason(name='Saved history', franchise_results_json=json.dumps(dict(matches=[
            dict(fixture_id=81, team1=self.a.name, team2=self.b.name, winner=self.a.name,
                 result='win', simulated=True, score1={'runs':150,'wickets':4}, score2={'runs':140,'wickets':8}),
            dict(fixture_id=82, team1=self.a.name, team2=self.b.name, winner=None,
                 result='no_result', score1={}, score2={})])))
        self.session.add(saved)
        self.session.flush()
        self.season.stats_season_id = saved.id
        self.season.tournament_id = None
        self.session.commit()
        data = self.client.get(self.base + '/compare', query_string=dict(mode='team', left=self.a.name, right=self.b.name)).json
        self.assertEqual(2, data['matches'])
        self.assertEqual(1, data['simulated'])
        self.assertEqual(1, data['no_result'])
        self.assertIsNone(data['left']['stats']['runs'])

    def test_player_cards_and_team_images_use_the_available_artwork_scope(self):
        from models import Player
        from PIL import Image
        player = Player(name='Alpha', rating=85, category='Batsman', country='India',
                        bat_hand='Right', bowl_hand='Right', bowl_style='Fast')
        self.session.add(player)
        self.session.commit()
        image = Image.new('RGBA', (20, 20), (50, 120, 180, 255))
        png = BytesIO(); image.save(png, format='PNG')
        from services.tg_storage_service import remote_downloads_allowed
        def card(source):
            self.assertEqual(player.id, source.id)
            self.assertFalse(remote_downloads_allowed())
            return png.getvalue()
        with patch('services.card_generator.generate_card', side_effect=card) as generate:
            response = self.client.get(f'/stats/media/players/{player.id}.png')
        generate.assert_called_once()
        self.assertEqual(200, response.status_code)
        self.assertEqual('image/png', response.mimetype)
        with patch('services.card_identity.team_logo_png', return_value=png.getvalue()):
            response = self.client.get(f'/stats/media/teams/{self.a.id}.png')
        self.assertEqual(200, response.status_code)
        self.assertEqual('image/png', response.mimetype)
        self.assertEqual(404, self.client.get('/stats/media/players/999999.png').status_code)


    def test_complete_comparison_figures_count_milestones_and_keep_missing_dots_unknown(self):
        fixtures = [self.game(winner=self.a.id), self.game(winner=self.b.id)]
        for index, fixture in enumerate(fixtures):
            lines = json.loads(fixture.scorecard_json)
            lines[0].update(bat_runs=100 if index == 0 else 60, bat_balls=50 if index == 0 else 30,
                            bat_dots=20 if index == 0 else 9,
                            match_potm={'name':'Beta','team':self.b.name})
            lines[1].update(bowl_wickets=5 if index == 0 else 3, bowl_dots=6 if index == 0 else 4)
            fixture.scorecard_json = json.dumps(lines)
        self.session.commit()
        from services.cricket_portal_history import player_comparison
        data = player_comparison(self.session, self.series, self.season, 'Alpha', 'Beta')
        bat, bowl = data['left']['selected'], data['right']['selected']
        self.assertEqual((1, 1, 2, 29, 36.25),
            (bat['hundreds'], bat['fifties'], bat['not_outs'], bat['bat_dots'], bat['dot_percentage']))
        self.assertEqual((2, 1, 10, 2),
            (bowl['three_fers'], bowl['five_fers'], bowl['bowl_dots'], bowl['potm']))
        lines = json.loads(fixtures[0].scorecard_json)
        lines[0].pop('bat_dots')
        fixtures[0].scorecard_json = json.dumps(lines)
        self.session.commit()
        data = player_comparison(self.session, self.series, self.season, 'Alpha', 'Beta')
        self.assertIsNone(data['left']['selected']['dot_percentage'])
        self.assertEqual(160, data['left']['selected']['runs'])

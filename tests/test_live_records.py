"""Public score centre: safe live feed and independently cached record boards."""
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_league_seasons_page as pages
setUpModule = pages.setUpModule
tearDownModule = pages.tearDownModule


class LiveRecordsTests(pages.PageCase):
    def setUp(self):
        super().setUp()
        from models import LeagueSeries, LeagueSeason
        from services import live_records
        live_records._CACHE.clear()
        self.series = LeagueSeries(name=f'Live Cup {self.tag}')
        self.session.add(self.series)
        self.session.flush()
        self.season = LeagueSeason(series_id=self.series.id, season_no=1,
                                   tournament_id=self.tour.id, name='Current season')
        self.session.add(self.season)
        self.session.commit()
        with self.client.session_transaction() as s:
            s.clear()
        self.base = f'/api/stats/{self.series.id}'

    def live_match(self, private=False):
        from models import Match, MatchState
        match = Match(user1_id=1, user2_id=2, tournament_id=self.tour.id,
                      status='active', overs=20, chat_id=-100123)
        self.session.add(match)
        self.session.flush()
        state = dict(mode='cipl_approach', is_private=private, innings=2,
            bat_team_name='Chennai', bowl_team_name='Mumbai', total_runs=45, total_wickets=1,
            current_over=7, current_ball=2, overs=20, target=151,
            inn1_bat_team='Mumbai', inn1_runs=150, inn1_wickets=5,
            chat_id=-100123, bat_user_tg=502, batting_approach='SECRET', bowling_approach='PRIVATE',
            bat_xi=[dict(roster_id=12, name='Batter', player_id=123, rating=91)],
            bowl_xi=[dict(roster_id=13, name='Bowler')],
            bat_stats={'12': dict(runs=30, balls=25, fours=3, sixes=1, out=False)},
            bowl_stats={'13': dict(runs=20, balls=12, wickets=1)})
        stored = MatchState(match_id=match.id, state_json=json.dumps(state), version=7)
        self.session.add(stored)
        self.session.commit()
        return match, stored, state

    def test_public_pages_need_no_admin_login_and_have_no_edit_controls(self):
        hub = self.client.get('/stats')
        self.assertEqual(200, hub.status_code)
        self.assertIn(self.series.name, hub.text)
        page = self.client.get(f'/stats/{self.series.id}')
        self.assertEqual(200, page.status_code)
        self.assertIn('Live centre', page.text)
        self.assertIn('Player statistics', page.text)
        self.assertNotIn('csrf_token', page.text)
        self.assertNotIn('Save qualifications', page.text)
        self.assertEqual(405, self.client.post(f'/stats/{self.series.id}').status_code)

    def test_live_feed_updates_without_scanning_records_or_exposing_captain_choices(self):
        from services import league_records
        match, stored, state = self.live_match()
        with patch.object(league_records, 'boards', side_effect=AssertionError('History scan during live poll')):
            response = self.client.get(self.base + '/live')
            self.assertEqual(200, response.status_code)
            data = response.json['matches'][0]
            self.assertEqual(match.id, data['id'])
            self.assertEqual(45, data['runs'])
            self.assertEqual(38, data['balls'])
            self.assertEqual(30, data['batting'][0]['runs'])
            for secret in ('SECRET', 'PRIVATE', 'chat_id', 'bat_user_tg', 'roster_id', 'rating'):
                self.assertNotIn(secret, response.text)
            state['total_runs'] = 49
            state['current_ball'] = 3
            state['bat_stats']['12']['runs'] = 34
            stored.state_json = json.dumps(state)
            stored.version += 1
            self.session.commit()
            next_data = self.client.get(self.base + '/live').json['matches'][0]
            self.assertEqual(49, next_data['runs'])
            self.assertEqual(39, next_data['balls'])
            self.assertEqual(34, next_data['batting'][0]['runs'])
            self.assertEqual('no-store', response.headers['Cache-Control'])

    def test_private_matches_and_inactive_competitions_are_hidden(self):
        self.live_match(private=True)
        self.assertEqual([], self.client.get(self.base + '/live').json['matches'])
        self.series.is_active = False
        self.session.commit()
        self.assertEqual(404, self.client.get(self.base + '/live').status_code)
        self.assertEqual(404, self.client.get(f'/stats/{self.series.id}').status_code)
        self.assertNotIn(self.series.name, self.client.get('/stats').text)

    def test_other_season_and_invalid_feed_are_rejected(self):
        from models import LeagueSeries, LeagueSeason
        other = LeagueSeries(name=f'Other Cup {self.tag}')
        self.session.add(other)
        self.session.flush()
        season = LeagueSeason(series_id=other.id, season_no=1)
        self.session.add(season)
        self.session.commit()
        self.assertEqual(404, self.client.get(self.base + f'/live?season={season.id}').status_code)
        self.assertEqual(404, self.client.get(self.base + '/captain').status_code)

    def test_confirmed_boards_are_cached_separately_from_live_polling(self):
        from services import league_records
        with patch.object(league_records, 'boards', wraps=league_records.boards) as boards:
            first = self.client.get(self.base + '/records')
            second = self.client.get(self.base + '/records')
            self.assertEqual(200, first.status_code)
            self.assertEqual(first.json, second.json)
            self.assertEqual(1, boards.call_count)
            self.client.get(self.base + '/live')
            self.assertEqual(1, boards.call_count)

    def test_completed_result_invalidates_cached_records_immediately(self):
        from models import TournamentMatch
        from services import league_records
        match, _, _ = self.live_match()
        with patch.object(league_records, 'boards', wraps=league_records.boards) as boards:
            self.assertEqual(0, self.client.get(self.base + '/records').json['summary']['matches'])
            self.session.add(TournamentMatch(tournament_id=self.tour.id, match_id=match.id,
                status='completed', inn1_runs=150, inn2_runs=151, scorecard_json='[]'))
            match.status = 'completed'
            self.session.commit()
            result = self.client.get(self.base + '/records').json
            self.assertEqual(1, result['summary']['matches'])
            self.assertEqual(2, boards.call_count)
            self.assertEqual([], self.client.get(self.base + '/live').json['matches'])

    def test_player_only_correction_invalidates_after_outer_commit(self):
        from models import TournamentMatch
        from services import league_records, live_records
        line = dict(name='Alpha', team_name='Mumbai', batted=True, bat_runs=60,
                    bat_balls=30, bat_out=True)
        game = TournamentMatch(tournament_id=self.tour.id, match_id=90000+self.tag,
            status='completed', inn1_runs=150, inn2_runs=151, scorecard_json=json.dumps([line]))
        self.session.add(game)
        self.session.commit()
        with patch.object(league_records, 'boards', wraps=league_records.boards) as boards:
            first = self.client.get(self.base + '/records').json
            self.assertEqual('Alpha', first['players'][0]['name'])
            line['name'] = 'Bravo'  # same size, unchanged team totals/source signature
            game.scorecard_json = json.dumps([line])
            self.session.flush()
            live_records.invalidate_after_commit(self.session)
            with self.session.begin_nested():
                pass
            self.assertEqual('Alpha', self.client.get(self.base + '/records').json['players'][0]['name'])
            self.assertEqual(1, boards.call_count)
            self.session.commit()
            updated = self.client.get(self.base + '/records').json
            self.assertEqual('Bravo', updated['players'][0]['name'])
            self.assertEqual(2, boards.call_count)

    def test_hundred_live_progress_uses_five_ball_sets(self):
        _, stored, state = self.live_match()
        state.update(ball_format='The100', current_over=3, current_ball=4)
        stored.state_json = json.dumps(state)
        self.session.commit()
        data = self.client.get(self.base + '/live').json['matches'][0]
        self.assertEqual(14, data['balls'])
        self.assertEqual(5, data['balls_per_over'])

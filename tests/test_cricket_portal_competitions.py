"""Public competition pages use real fixture/roster data and approved news."""
import json
import os
import sys
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_league_seasons_page as pages
setUpModule = pages.setUpModule
tearDownModule = pages.tearDownModule


class CompetitionPortalTests(pages.PageCase):
    def setUp(self):
        super().setUp()
        from models import LeagueSeries, LeagueSeason, TournamentMatch, TournamentTeam
        from services import live_records
        live_records.invalidate_records_cache()
        self.series = LeagueSeries(name=f'Portal Cup {self.tag}')
        self.session.add(self.series)
        self.session.flush()
        self.season = LeagueSeason(series_id=self.series.id, season_no=1,
            league_id=self.league.id, tournament_id=self.tour.id, name='Current season')
        self.session.add(self.season)
        self.teams = self.session.query(TournamentTeam).filter_by(tournament_id=self.tour.id).order_by(TournamentTeam.id).all()
        a, b = self.teams
        self.game = TournamentMatch(tournament_id=self.tour.id, team1_id=a.id, team2_id=b.id,
            winner_team_id=a.id, status='completed', stage='league', match_no=1, round_no=1,
            result_text=f'{a.name} won by 10 runs', inn1_runs=160, inn2_runs=150,
            inn1_wickets=4, inn2_wickets=8, venue='Wankhede', pitch_type='Flat',
            planned_winner_team_id=a.id, host_user_id=1234567, target_user_id=9876543,
            completed_at=datetime(2026, 2, 1), scorecard_json=json.dumps([
                dict(name='Alpha', team_name=a.name, batted=True, bat_runs=80, bat_balls=40,
                     bat_fours=5, bat_sixes=4, bat_out=False, bowled=True, bowl_wickets=3,
                     bowl_runs=20, bowl_balls=24),
                dict(name='Beta', team_name=b.name, batted=True, bat_runs=80, bat_balls=50,
                     bat_out=True, bowled=True, bowl_wickets=1, bowl_runs=30, bowl_balls=24)]))
        self.future = TournamentMatch(tournament_id=self.tour.id, team1_id=b.id, team2_id=a.id,
            status='scheduled', stage='final', match_no=2, round_no=2, venue='Eden Gardens')
        self.session.add_all([self.game, self.future])
        self.session.commit()

    def test_fixtures_caps_playoffs_and_team_performance_are_real(self):
        from services.cricket_portal_competitions import portal_snapshot
        data = portal_snapshot(self.session, self.series, self.season)
        self.assertEqual({'completed', 'scheduled'}, {f['status'] for f in data['fixtures']})
        self.assertIsNone(next(f for f in data['fixtures'] if f['status'] == 'scheduled')['date'])
        self.assertEqual('Flat', data['fixtures'][0]['pitch'])
        self.assertEqual({'Alpha', 'Beta'}, {p['name'] for p in data['tournament']['orange_cap']})
        self.assertEqual(80, data['tournament']['orange_cap'][0]['value'])
        self.assertEqual('Alpha', data['tournament']['purple_cap'][0]['name'])
        self.assertEqual('final', data['tournament']['playoffs'][0]['stage'])
        team = next(t for t in data['teams'] if t['name'] == self.teams[0].name)
        self.assertEqual((1, 1, 160, 8), (team['played'], team['won'], team['runs'], team['wickets']))
        self.assertEqual(160, data['venues'][0]['average_first_innings'])
        victory = next(n for n in data['news'] if n['kind'] == 'match_victory')
        self.assertIn(self.teams[0].name, victory['title'])
        body = json.dumps(data)
        for hidden in ('planned_winner', 'owner_tg_id', 'host_user_id', 'target_user_id', '1234567', '9876543'):
            self.assertNotIn(hidden, body)

    def test_live_schedule_and_unresolved_playoff_slots_do_not_create_fake_teams(self):
        from models import TournamentMatch
        from services.cricket_portal_competitions import portal_snapshot
        self.session.add_all([
            TournamentMatch(tournament_id=self.tour.id, team1_id=self.teams[0].id,
                team2_id=self.teams[1].id, status='live', stage='league'),
            TournamentMatch(tournament_id=self.tour.id, status='scheduled', stage='qualifier2',
                slot1_label='Loser qualifier one', slot2_label='Winner eliminator'),
        ])
        self.session.commit()
        data = portal_snapshot(self.session, self.series, self.season)
        self.assertEqual({'completed', 'scheduled', 'live'}, {f['status'] for f in data['fixtures']})
        self.assertEqual({t.name for t in self.teams}, {t['name'] for t in data['teams']})
        live = next(f for f in data['fixtures'] if f['status'] == 'live')
        self.assertIsNone(live['runs1'])

    def test_team_dashboard_has_current_squad_captain_and_upcoming_fixture(self):
        from services.cricket_portal_competitions import team_profile
        from models import ChallengePlayer
        row = self.session.query(ChallengePlayer).filter_by(team_id=self.teams[0].challenge_team_id).first()
        row.details_json = json.dumps({'OVR': 91, 'BAT': 93, 'BOWL': 42})
        self.session.flush()
        profile = team_profile(self.session, self.series, self.season, self.teams[0].name.upper())
        self.assertEqual(self.teams[0].owner_name, profile['captain'])
        player = next(p for p in profile['squad'] if p['name'] == row.name)
        self.assertEqual((91, 93, 42), (player['ovr'], player['bat'], player['bowl']))
        self.assertEqual('current_roster', profile['squad_source'])
        self.assertEqual(1, len(profile['recent_results']))
        self.assertEqual('scheduled', profile['upcoming_fixtures'][0]['status'])
        self.assertEqual(100, profile['performance']['win_percentage'])
        self.assertIsNone(team_profile(self.session, self.series, self.season, 'Unrelated team'))
        self.assertNotIn('source_player_id', json.dumps(profile))

    def test_news_contains_only_published_editorial_and_recorded_injuries(self):
        from models import NewsArticle, TournamentInjury
        from services.cricket_portal_competitions import portal_snapshot
        self.session.add_all([
            NewsArticle(headline='A published interview', body='A real editorial article.', status='published',
                author_user_id=993355, author_telegram_id=11223344, published_at=datetime(2026, 2, 2)),
            NewsArticle(headline='Hidden pending headline', body='Moderation private', status='pending'),
            NewsArticle(headline='Hidden rejected headline', body='Moderation private', status='rejected'),
            TournamentInjury(tournament_id=self.tour.id, tournament_team_id=self.teams[0].id,
                roster_id=1, player_name='Injured Player', injury_type='Hamstring Strain',
                matches_out=2, matches_remaining=2),
        ])
        self.session.commit()
        data = portal_snapshot(self.session, self.series, self.season)
        articles = [n for n in data['news'] if n['kind'] == 'article']
        self.assertEqual(['A published interview'], [n['title'] for n in articles])
        self.assertEqual('Injured Player: Hamstring Strain', next(n for n in data['news'] if n['kind'] == 'injury')['title'])
        payload = json.dumps(data)
        self.assertNotIn('Moderation private', payload)
        self.assertNotIn('993355', payload)
        self.assertNotIn('11223344', payload)

    def test_archive_keeps_team_records_but_unknown_wickets_and_ratings_stay_unknown(self):
        from services import league_records as R, live_records
        from services.cricket_portal_competitions import portal_snapshot, team_profile
        from models import LeagueRecordConfig
        config = LeagueRecordConfig(season_id=self.season.id)
        self.session.add(config)
        config.archive_json = json.dumps([dict(fixture_id=99, match_id=None, stage='final',
            team1='Archived XI', team2='Rivals', winner='Archived XI', runs1=170, runs2=160,
            venue='Old Ground', date='2025-01-01', lines=[dict(name='Past Star', team_name='Archived XI',
                batted=True, bat_runs=90, bat_balls=50)])])
        self.season.tournament_id = None
        self.session.commit()
        live_records.invalidate_records_cache()
        data = portal_snapshot(self.session, self.series, self.season)
        archived = next(t for t in data['teams'] if t['name'] == 'Archived XI')
        self.assertEqual(170, archived['highest_score'])
        self.assertIsNone(archived['wickets'])
        profile = team_profile(self.session, self.series, self.season, 'Archived XI')
        self.assertEqual('Past Star', profile['squad'][0]['name'])
        self.assertIsNone(profile['squad'][0]['ovr'])
        self.assertIsNone(profile['captain'])


    def test_partial_historical_scores_are_not_presented_as_complete_team_totals(self):
        from models import TournamentMatch
        from services.cricket_portal_competitions import team_profile
        self.session.add(TournamentMatch(tournament_id=self.tour.id,
            team1_id=self.teams[0].id, team2_id=self.teams[1].id,
            winner_team_id=self.teams[1].id, status='completed',
            completed_at=datetime(2026, 2, 2), result_type='win'))
        self.session.commit()
        profile = team_profile(self.session, self.series, self.season, self.teams[0].name)
        performance = profile['performance']
        self.assertEqual(2, performance['played'])
        self.assertIsNone(performance['runs'])
        self.assertIsNone(performance['wickets'])
        self.assertEqual(160, performance['known_runs'])
        self.assertEqual(8, performance['known_wickets'])
        self.assertEqual(1, performance['innings_with_scores'])
        self.assertEqual(1, performance['innings_with_wickets'])
        self.assertEqual(160, performance['highest_recorded_score'])
        self.assertIn('Highest recorded total', {r['label'] for r in profile['records']})
        self.assertIn('Average recorded innings score', {r['label'] for r in profile['records']})

    def test_no_result_with_unrecorded_innings_keeps_score_coverage_explicit(self):
        from models import TournamentMatch
        from services.cricket_portal_competitions import team_profile
        self.session.add(TournamentMatch(tournament_id=self.tour.id,
            team1_id=self.teams[0].id, team2_id=self.teams[1].id,
            status='completed', result_type='no_result',
            completed_at=datetime(2026, 2, 2)))
        self.session.commit()
        performance = team_profile(self.session, self.series, self.season,
                                   self.teams[0].name)['performance']
        self.assertEqual(1, performance['no_result'])
        self.assertEqual(2, performance['played'])
        self.assertIsNone(performance['runs'])
        self.assertEqual(160, performance['known_runs'])
        self.assertEqual(1, performance['innings_with_scores'])

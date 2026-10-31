"""Statsguru filters and profiles use recorded totals and conservative identity."""
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_league_seasons_page as pages
setUpModule = pages.setUpModule
tearDownModule = pages.tearDownModule


class PortalStatisticsTests(pages.PageCase):
    def setUp(self):
        super().setUp()
        from models import LeagueSeason, TournamentTeam
        from services import league_season_service as LS, cricket_portal_statistics as S
        self.S = S
        self.series = LS.create_series(self.session, f'Stats Cup {self.tag}')
        self.season = LS.attach_season(self.session, self.series, tournament=self.tour)
        self.teams = self.session.query(TournamentTeam).filter_by(tournament_id=self.tour.id).all()
        self.a, self.b = self.teams
        self.session.commit()

    def line(self, name='Alpha', runs=60, balls=30, wickets=2, conceded=15, bowled=10, **extra):
        return dict(name=name, team_name=self.a.name, batted=True, bat_runs=runs,
            bat_balls=balls, bat_out=True, bat_fours=2, bat_sixes=1, bowled=True,
            bowl_wickets=wickets, bowl_runs=conceded, bowl_balls=bowled, **extra)

    def game(self, lines, pitch='Dry', venue='North Ground', runs1=150, runs2=151, day=1, stage='league', winner=None):
        from models import TournamentMatch
        row = TournamentMatch(tournament_id=self.tour.id, team1_id=self.a.id, team2_id=self.b.id,
            winner_team_id=winner or self.b.id, status='completed', stage=stage,
            pitch_type=pitch, venue=venue, completed_at=datetime(2026, 1, day),
            inn1_runs=runs1, inn2_runs=runs2, scorecard_json=json.dumps(lines))
        self.session.add(row)
        self.session.commit()
        return row

    def player(self, name='Alpha', **kwargs):
        from models import Player
        row = Player(name=name, rating=90, bat_rating=92, bowl_rating=85, category='All Rounder',
            country='India', bat_hand='Right', bowl_hand='Right', bowl_style='Fast', **kwargs)
        self.session.add(row)
        self.session.commit()
        return row

    def test_filters_are_combined_case_insensitively_and_rates_have_correct_denominators(self):
        p = self.player()
        self.game([self.line(player_id=p.id)])
        self.game([self.line(runs=90, balls=90, wickets=1, conceded=40, bowled=30, player_id=p.id)],
                  pitch='Green', venue='South Ground', day=2)
        result = self.S.explorer(self.session, self.series, self.season,
            dict(player='ALP', team=self.a.name.lower(), opponent=self.b.name.upper(), pitch='dry', venue='north ground'))
        row = result['rows'][0]
        self.assertEqual((1, 60, 30, 200.0, 9.0), (row['matches'], row['runs'], row['balls'], row['sr'], row['economy']))
        self.assertEqual((90, 92, 85), (row['ovr'], row['bat'], row['bowl']))
        self.assertEqual(['Dry', 'Green'], result['filters']['pitches'])
        all_rows = self.S.explorer(self.session, self.series, self.season)['rows'][0]
        self.assertEqual(125.0, all_rows['sr'])  # 150/120, never an average of innings SRs
        self.assertEqual(8.25, all_rows['economy'])
        self.assertEqual([], self.S.explorer(self.session, self.series, self.season, dict(opponent=self.a.name))['rows'])

    def test_unknown_historical_denominators_are_not_converted_into_zero(self):
        line = self.line()
        line.pop('bat_balls')
        line.pop('bat_out')
        line.pop('bowl_balls')
        self.game([line], pitch=None)
        row = self.S.explorer(self.session, self.series, self.season)['rows'][0]
        self.assertEqual(60, row['runs'])
        self.assertIsNone(row['balls'])
        self.assertIsNone(row['sr'])
        self.assertIsNone(row['average'])
        self.assertIsNone(row['economy'])
        self.assertEqual(1, self.S.explorer(self.session, self.series, self.season)['coverage']['unknown_pitch_matches'])

    def test_ambiguous_player_versions_never_get_an_arbitrary_rating(self):
        self.player(version='Base')
        self.player(version='World Cup')
        self.game([self.line()])
        profile = self.S.player_profile(self.session, self.series, self.season, 'alpha')
        self.assertTrue(profile['identity']['ambiguous'])
        self.assertIsNone(profile['identity']['ovr'])
        row = self.S.explorer(self.session, self.series, self.season)['rows'][0]
        self.assertIsNone(row['ovr'])

    def test_known_player_id_selects_the_recorded_version(self):
        p = self.player(version='Base')
        self.player(version='World Cup')
        self.game([self.line(player_id=p.id)])
        profile = self.S.player_profile(self.session, self.series, self.season, 'Alpha')
        self.assertFalse(profile['identity']['ambiguous'])
        self.assertEqual('Base', profile['identity']['version'])
        self.assertEqual(90, profile['identity']['ovr'])
        self.assertEqual(60, profile['form']['recent_runs'])

    def test_venue_profile_separates_team_totals_and_player_runs_and_unknown_phases(self):
        self.game([self.line()], runs1=150, runs2=151)
        self.game([self.line(runs=20)], pitch='Green', runs1=160, runs2=160, day=2)
        result = self.S.venue_profile(self.session, self.series, self.season, 'NORTH GROUND')
        self.assertEqual(2, result['summary']['matches'])
        self.assertEqual(621, result['summary']['runs'])
        self.assertEqual(155.0, result['summary']['average_first_innings'])
        self.assertEqual(1, result['summary']['successful_chases'])  # level scores settled by SO are not a chase
        self.assertEqual(80, result['players'][0]['runs'])
        self.assertEqual([], result['phases'])
        self.assertIsNone(self.S.venue_profile(self.session, self.series, self.season, 'Unknown Ground'))

    def test_search_pagination_is_bounded_and_read_only(self):
        self.game([self.line(name=f'Player {i}') for i in range(5)])
        result = self.S.explorer(self.session, self.series, self.season, dict(page=2, limit=2))
        self.assertEqual((5, 2, 2), (result['total'], result['page'], len(result['rows'])))
        self.assertEqual(100, self.S.explorer(self.session, self.series, self.season, dict(limit=100000))['page_size'])
        self.assertEqual(1, self.S.explorer(self.session, self.series, self.season, dict(page=-5))['page'])
        self.assertIsNone(self.S.player_profile(self.session, self.series, self.season, 'Unrecorded player'))

    def test_archived_evidence_keeps_profiles_and_unknown_surface(self):
        from services import league_records as R
        self.game([self.line()])
        R.preserve(self.session, self.season)
        self.season.tournament_id = None
        self.session.commit()
        profile = self.S.player_profile(self.session, self.series, self.season, 'Alpha')
        self.assertEqual(60, profile['career']['runs'])
        self.assertEqual('North Ground', profile['recent'][0]['venue'])
        # Pitch was not part of the old archive schema; do not guess Dry.
        self.assertIsNone(profile['recent'][0]['pitch'])


    def test_player_phase_metrics_use_only_the_players_recorded_deliveries(self):
        def event(innings, batter, bowler, team, opponent, runs, phase):
            return dict(innings=innings, wicket_number=1, pair=[], team_runs=runs,
                legal=True, batter=batter, bat_runs=runs, faced=1, bowler=bowler,
                conceded=runs, bowler_wicket=False, wicket=False, phase=phase,
                team=team, opponent=opponent)
        alpha = self.line(runs=6, balls=1, wickets=0, conceded=1, bowled=1)
        beta = self.line(name='Beta', runs=1, balls=1, wickets=0, conceded=6, bowled=1)
        beta['team_name'] = self.b.name
        alpha['bat_out'] = beta['bat_out'] = False
        alpha['record_detail'] = dict(version=1, ball_format='The100', overs=20,
            innings_starts={'1': {'complete': True}, '2': {'complete': True}},
            deliveries=[event(1, 'Alpha', 'Beta', self.a.name, self.b.name, 6, 'powerplay'),
                        event(2, 'Beta', 'Alpha', self.b.name, self.a.name, 1, 'death')])
        self.game([alpha, beta], runs1=6, runs2=1)
        profile = self.S.player_profile(self.session, self.series, self.season, 'Alpha')
        self.assertEqual(1, profile['coverage']['phase_matches'])
        self.assertEqual(2, len(profile['phases']))
        bat = next(p for p in profile['phases'] if p['side'] == 'bat')
        bowl = next(p for p in profile['phases'] if p['side'] == 'bowl')
        self.assertEqual(('powerplay', 6, 1, 600.0), (bat['phase'], bat['runs'], bat['balls'], bat['sr']))
        self.assertEqual(('death', 1, 1, 6.0), (bowl['phase'], bowl['runs'], bowl['balls'], bowl['economy']))
        self.assertEqual(6.0, profile['career']['economy'])

    def test_roster_only_player_has_ratings_without_inventing_appearances(self):
        player = next(iter(self.cards.values()))
        profile = self.S.player_profile(self.session, self.series, self.season, player.name)
        self.assertTrue(profile['roster_only'])
        self.assertEqual(player.rating, profile['identity']['ovr'])
        self.assertEqual(player.bat_rating, profile['identity']['bat'])
        self.assertEqual(0, profile['career']['matches'])
        self.assertEqual(0, profile['selected']['innings'])
        self.assertIsNone(profile['career']['highest_score'])
        self.assertEqual([], profile['records'])
        self.assertEqual([], profile['recent'])
        self.assertEqual([], profile['phases'])
        self.assertEqual(0, profile['form']['matches'])

    def test_roster_lookup_does_not_escape_the_selected_competition(self):
        from services import league_season_service as LS
        outsider = self.player(name='Unassigned Outsider')
        self.assertIsNone(self.S.player_profile(self.session, self.series, self.season, outsider.name))
        other = LS.create_series(self.session, f'Other Stats Cup {self.tag}')
        self.session.commit()
        player = next(iter(self.cards.values()))
        self.assertIsNone(self.S.player_profile(self.session, other, None, player.name))

    def test_same_name_roster_versions_leave_ratings_unknown(self):
        from models import ChallengePlayer
        first = self.player(version='Base')
        second = self.player(version='World Cup')
        self.session.add_all([
            ChallengePlayer(team_id=self.a.challenge_team_id, source_player_id=first.id, name='Alpha'),
            ChallengePlayer(team_id=self.b.challenge_team_id, source_player_id=second.id, name='Alpha')])
        self.session.commit()
        profile = self.S.player_profile(self.session, self.series, self.season, 'Alpha')
        self.assertTrue(profile['identity']['ambiguous'])
        self.assertIsNone(profile['identity']['ovr'])
        self.assertEqual(0, profile['career']['matches'])

    def test_reused_core_match_id_cannot_enrich_a_different_tournaments_evidence(self):
        from models import Match, Tournament
        other = Tournament(name='Unrelated competition', kind='challenge')
        self.session.add(other)
        self.session.flush()
        core = Match(user1_id=1, user2_id=2, tournament_id=other.id,
                     pitch_type='Unrelated Surface', stadium='Unrelated Ground')
        self.session.add(core)
        self.session.commit()
        fixture = self.game([self.line()], pitch=None, venue=None)
        fixture.match_id = core.id
        self.session.commit()
        profile = self.S.player_profile(self.session, self.series, self.season, 'Alpha')
        self.assertIsNone(profile['recent'][0]['pitch'])
        self.assertIsNone(profile['recent'][0]['venue'])

    def legacy(self, name='Old Player', season_no=2, **changes):
        from models import LeagueSeason, StatsSeason, StatsSeasonPlayer
        saved = StatsSeason(name=f'Legacy {self.tag}-{season_no}', kind='challenge')
        self.session.add(saved)
        self.session.flush()
        values = dict(name=name, team_name='Historic XI', matches=6, bat_innings=6,
            bat_runs=250, bat_balls=100, bat_outs=5, bat_fours=20, bat_sixes=5,
            highest_score=100, bowl_innings=3, bowl_wickets=3, bowl_runs=30,
            bowl_balls=20, best_bowl_wickets=2, best_bowl_runs=10)
        values.update(changes)
        self.session.add(StatsSeasonPlayer(season_id=saved.id, **values))
        season = LeagueSeason(series_id=self.series.id, season_no=season_no,
                             name=f'Legacy {season_no}', stats_season_id=saved.id)
        self.session.add(season)
        self.session.commit()
        return season

    def test_aggregate_only_legacy_player_has_career_totals_without_fake_scorecards(self):
        legacy = self.legacy()
        rows = self.S.explorer(self.session, self.series, legacy)
        player = rows['rows'][0]
        self.assertEqual(('Old Player', 6, 250, 250.0, 50.0, 9.0),
            (player['name'], player['matches'], player['runs'], player['sr'], player['average'], player['economy']))
        self.assertIsNone(rows['summary']['matches'])  # per-player aggregates cannot identify distinct fixtures
        self.assertEqual(0, rows['summary']['scorecard_matches'])
        self.assertEqual(1, rows['coverage']['aggregate_only_seasons'])
        profile = self.S.player_profile(self.session, self.series, legacy, 'OLD PLAYER')
        self.assertEqual(250, profile['selected']['runs'])
        self.assertEqual(250, profile['career']['runs'])
        self.assertEqual(100, profile['career']['highest_score'])
        self.assertEqual('2/10', profile['career']['best_bowling'])
        self.assertEqual([], profile['recent'])
        self.assertEqual([], profile['phases'])
        self.assertEqual([], profile['splits']['opponent'])
        self.assertIsNone(profile['form']['recent_runs'])
        self.assertIsNone(profile['identity']['ovr'])
        self.assertTrue(profile['seasons'][0]['aggregate_only'])
        self.assertIn('match-level', profile['coverage']['note'])

    def test_unsupported_legacy_match_filters_do_not_invent_pitch_or_opponent(self):
        legacy = self.legacy()
        for filters in (dict(pitch='Dry'), dict(venue='North Ground'),
                        dict(opponent='Other XI'), dict(stage='final')):
            self.assertEqual([], self.S.explorer(self.session, self.series, legacy, filters)['rows'])
        result = self.S.explorer(self.session, self.series, legacy,
                                dict(player='old', team='HISTORIC XI'))
        self.assertEqual(250, result['rows'][0]['runs'])
        self.assertEqual(['Historic XI'], result['filters']['teams'])

    def test_legacy_and_scorecards_merge_once_and_evidence_season_aggregates_are_not_added_twice(self):
        from models import TournamentPlayerStats
        self.game([self.line()])
        self.session.add(TournamentPlayerStats(tournament_id=self.tour.id, user_id=1,
            name='Alpha', team_name=self.a.name, matches=99, bat_runs=999, bat_balls=999))
        self.session.commit()
        self.legacy(name='Alpha')
        result = self.S.explorer(self.session, self.series)
        alpha = next(p for p in result['rows'] if p['name'] == 'Alpha')
        self.assertEqual(310, alpha['runs'])
        self.assertEqual(7, alpha['matches'])
        self.assertEqual(238.46, alpha['sr'])
        profile = self.S.player_profile(self.session, self.series, self.season, 'Alpha')
        self.assertEqual(60, profile['selected']['runs'])
        self.assertEqual(310, profile['career']['runs'])
        self.assertEqual(1, len(profile['recent']))
        self.assertEqual(2, len(profile['seasons']))
        filtered = self.S.explorer(self.session, self.series, filters=dict(pitch='Dry'))
        self.assertEqual(60, filtered['rows'][0]['runs'])
        self.assertEqual(0, filtered['coverage']['aggregate_only_seasons'])

    def test_legacy_missing_denominators_do_not_invent_rates_or_innings(self):
        legacy = self.legacy(bat_balls=0, bat_outs=0, bat_innings=0,
                             bowl_balls=0, bowl_innings=0)
        row = self.S.explorer(self.session, self.series, legacy)['rows'][0]
        self.assertEqual(250, row['runs'])
        self.assertEqual(3, row['wickets'])
        self.assertIsNone(row['sr'])
        self.assertIsNone(row['average'])
        self.assertIsNone(row['economy'])
        self.assertIsNone(row['innings'])
        self.assertIsNone(row['bowling_innings'])


    def test_pitch_filter_prefers_played_surface_over_scheduled_surface(self):
        from models import Match
        fixture = self.game([self.line()], pitch='Dry')
        match = Match(user1_id=1, user2_id=2, tournament_id=self.tour.id,
                      status='completed', pitch_type='Green')
        self.session.add(match)
        self.session.flush()
        fixture.match_id = match.id
        self.session.commit()
        data = self.S.explorer(self.session, self.series, self.season, dict(pitch='Green'))
        self.assertEqual(60, data['rows'][0]['runs'])
        self.assertEqual([], self.S.explorer(self.session, self.series, self.season, dict(pitch='Dry'))['rows'])

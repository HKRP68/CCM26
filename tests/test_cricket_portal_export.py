"""Public spreadsheet downloads retain versions, scope, numeric types and gaps."""
import csv
import io
import json
import os
import sys
import zipfile
from datetime import datetime
from unittest.mock import patch
from xml.etree import ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_league_seasons_page as pages
setUpModule = pages.setUpModule
tearDownModule = pages.tearDownModule


class PlayerExportTests(pages.PageCase):
    def setUp(self):
        super().setUp()
        from models import TournamentTeam
        from services import league_season_service as LS, cricket_portal_export as E
        self.E, self.LS = E, LS
        self.series = LS.create_series(self.session, f'Export Cup {self.tag}')
        self.season = LS.attach_season(self.session, self.series, tournament=self.tour)
        self.a, self.b = self.session.query(TournamentTeam).filter_by(tournament_id=self.tour.id).all()
        self.session.commit()
        self.url = f'/stats/{self.series.id}/players/download'
        with self.client.session_transaction() as state:
            state.clear()

    def player(self, version='Base', name=None, rating=90):
        from models import Player
        row = Player(name=name or f'Export Star {self.tag}', version=version,
            rating=rating, bat_rating=92, bowl_rating=85, category='All Rounder',
            country='India', bat_hand='Right', bowl_hand='Right', bowl_style='Fast')
        self.session.add(row)
        self.session.commit()
        return row

    def line(self, player, runs=60, balls=30, wickets=2, conceded=15, bowled=10, **extra):
        row = dict(name=player.name, player_id=player.id, team_name=self.a.name,
            batted=True, bat_runs=runs, bat_balls=balls, bat_out=True, bat_fours=2,
            bat_sixes=1, bowled=True, bowl_wickets=wickets, bowl_runs=conceded,
            bowl_balls=bowled, bowl_dots=2, hattricks=0)
        row.update(extra)
        return row

    def game(self, lines, **extra):
        from models import TournamentMatch
        values = dict(tournament_id=self.tour.id, team1_id=self.a.id,
            team2_id=self.b.id, winner_team_id=self.b.id, status='completed',
            stage='league', venue='Test Ground', completed_at=datetime(2026, 1, 1),
            inn1_runs=150, inn2_runs=151, scorecard_json=json.dumps(lines))
        values.update(extra)
        row = TournamentMatch(**values)
        self.session.add(row)
        self.session.commit()
        return row

    def data(self, season=None):
        return self.E.snapshot(self.session, self.series, season)

    def archive(self, player, **extra):
        from models import LeagueSeason, StatsSeason, StatsSeasonPlayer
        saved = StatsSeason(name=f'Archive {self.tag}', kind='challenge')
        self.session.add(saved)
        self.session.flush()
        season = LeagueSeason(series_id=self.series.id, season_no=2, name='Archived season', stats_season_id=saved.id)
        self.session.add(season)
        values = dict(season_id=saved.id, player_id=player.id, name=player.name,
            team_name=self.b.name, matches=2, bat_innings=2, bat_runs=40,
            bat_balls=40, bat_outs=1, highest_score=30, highest_score_not_out=True,
            fifties=None, hundreds=None, ducks=None, three_fers=None,
            five_fers=None, hattricks=None, potm=None, bowl_innings=2,
            bowl_wickets=1, bowl_runs=25, bowl_balls=30, best_bowl_wickets=1, best_bowl_runs=10)
        values.update(extra)
        self.session.add(StatsSeasonPlayer(**values))
        self.session.commit()
        return season

    def test_versions_remain_separate_and_rates_use_combined_denominators(self):
        base, gold = self.player(), self.player('Gold', rating=99)
        self.game([self.line(base, runs=50, balls=25, wickets=5, conceded=20, bowled=24,
                             balls_to_50=20, bat_out=False), self.line(gold, runs=20)])
        self.game([self.line(base, runs=100, balls=40, wickets=3, conceded=24, bowled=18,
                             balls_to_50=15, balls_to_100=38, hattricks=1)])
        data = self.data()
        self.assertEqual(2, len(data['totals']))
        base_row, gold_row = data['totals']
        self.assertEqual(('Base', 90, 150), (base_row['version'], base_row['rating'], base_row['runs']))
        self.assertEqual(('Gold', 99, 20), (gold_row['version'], gold_row['rating'], gold_row['runs']))
        self.assertEqual((2, 1, 1, 1), (base_row['matches'], base_row['not_outs'], base_row['fifties'], base_row['hundreds']))
        self.assertEqual((150.0, 230.77, 6.29), (base_row['average'], base_row['sr'], base_row['economy']))
        self.assertEqual((1, 1, 1, '5/20'), (base_row['three_fers'], base_row['five_fers'], base_row['hattricks'], base_row['best_bowling']))
        self.assertEqual((15, 38, False), (base_row['fastest_50'], base_row['fastest_100'], base_row['highest_score_not_out']))
        self.assertEqual(2, len(data['seasons_rows']))

    def test_all_seasons_include_archives_and_preserve_unknown_values(self):
        player = self.player()
        self.game([self.line(player, runs=60, balls=20, wickets=1, conceded=5, bowled=6)])
        archived = self.archive(player)
        data = self.data()
        row = data['totals'][0]
        self.assertEqual((2, 3, 100, 60, 2), (row['seasons'], row['matches'], row['runs'], row['balls'], len(data['seasons_rows'])))
        self.assertEqual((50.0, 166.67, 5.0), (row['average'], row['sr'], row['economy']))
        self.assertIsNone(row['fifties'])
        self.assertIsNone(row['catches'])
        self.assertIsNone(row['hattricks'])
        self.assertEqual((1, 1), (row['scorecard_matches'], row['aggregate_seasons']))
        self.assertIn(self.a.name, row['teams'])
        self.assertIn(self.b.name, row['teams'])
        self.assertEqual(60, self.data(self.season)['totals'][0]['runs'])
        self.assertEqual(40, self.data(archived)['totals'][0]['runs'])

    def test_unknown_versions_do_not_absorb_a_known_card_or_another_season(self):
        player = self.player()
        self.player('Gold')
        line = self.line(player)
        line.pop('player_id')
        line.pop('bat_balls')
        line.pop('bat_out')
        self.game([line, self.line(player, runs=10)])
        self.archive(player, player_id=None)
        rows = self.data()['totals']
        self.assertEqual(3, len(rows))
        known = next(row for row in rows if row['version'] == 'Base')
        unknown = [row for row in rows if row['version'] is None]
        self.assertEqual(10, known['runs'])
        self.assertTrue(all(row['rating'] is None for row in unknown))
        partial = next(row for row in unknown if row['source'] == 'Scorecards')
        self.assertIsNone(partial['balls'])
        self.assertIsNone(partial['sr'])
        self.assertIsNone(partial['average'])

    def test_wicketless_bests_zero_economy_and_undefined_rates_survive_archives(self):
        player = self.player()
        archived = self.archive(player, bat_runs=0, bat_outs=0, highest_score=0,
            bowl_wickets=0, bowl_runs=0, best_bowl_wickets=0, best_bowl_runs=0)
        row = self.data(archived)['totals'][0]
        self.assertEqual((0, 0.0, '0/0', 0.0), (row['highest_score'], row['sr'], row['best_bowling'], row['economy']))
        self.assertIsNone(row['average'])
        self.assertIsNone(row['bowling_average'])
        self.assertIsNone(row['bowling_strike_rate'])

    def test_deleted_tournament_keeps_its_scorecard_details_and_versions(self):
        from services import season_archive
        player = self.player('World Cup')
        self.game([self.line(player, balls_to_50=24)])
        season_archive.delete_tournament(self.session, self.tour, keep_stats=True)
        self.session.commit()
        self.assertIsNone(self.season.tournament_id)
        row = self.data()['totals'][0]
        self.assertEqual(('World Cup', 60, 24, 'Scorecards'),
                         (row['version'], row['runs'], row['fastest_50'], row['source']))
        self.assertEqual(0, row['aggregate_seasons'])

    def test_squad_overrides_and_custom_players_are_resolved_within_the_season(self):
        from models import ChallengePlayer, ChallengeTeam, Player
        roster = self.session.query(ChallengePlayer).join(ChallengeTeam).filter(
            ChallengeTeam.league_id == self.league.id).first()
        team = self.session.get(ChallengeTeam, roster.team_id)
        roster.details_json = json.dumps(dict(rating=88, bat=87, bowling=82))
        master = self.session.get(Player, roster.source_player_id)
        self.game([self.line(master, roster_id=roster.id, team_name=team.name)])
        row = self.data()['totals'][0]
        self.assertEqual((88, 87, 82, 'Base'), (row['rating'], row['bat_rating'], row['bowl_rating'], row['version']))
        custom = ChallengePlayer(team_id=team.id, name=f'Custom Star {self.tag}',
            details_json=json.dumps(dict(version='Custom', rating=84, role='Bowler')))
        self.session.add(custom)
        self.session.commit()
        custom_line = self.line(master, name=custom.name, player_id=None, roster_id=custom.id, team_name=team.name)
        self.game([custom_line])
        row = next(row for row in self.data()['totals'] if row['name'] == custom.name)
        self.assertEqual(('Custom', 84, 'Bowler'), (row['version'], row['rating'], row['role']))

    def test_fielding_dots_and_zeroes_require_verified_delivery_evidence(self):
        player = self.player()
        other = self.player(name=f'Opponent {self.tag}')
        first = self.line(player, runs=0, balls=1, wickets=1, conceded=0, bowled=1)
        second = self.line(other, runs=0, balls=1, wickets=0, conceded=0, bowled=1, team_name=self.b.name)
        first.pop('bowl_dots')
        second.pop('bowl_dots')
        event = dict(innings=1, wicket_number=1, pair=[], team_runs=0, legal=True,
            batter=player.name, bat_runs=0, faced=1, bowler=other.name,
            conceded=0, bowler_wicket=False, wicket=True, phase='powerplay',
            team=self.a.name, opponent=self.b.name, how='run out', fielder=other.name,
            keeper=False, direct_hit=True, assists=[])
        reverse = dict(event, innings=2, batter=other.name, bowler=player.name,
            bowler_wicket=True, team=self.b.name, opponent=self.a.name,
            how='caught', fielder=player.name, keeper=True, direct_hit=None)
        first['record_detail'] = dict(version=1, innings_starts={'1': {'complete': True}, '2': {'complete': True}}, deliveries=[event, reverse])
        self.game([first, second])
        row = next(row for row in self.data()['totals'] if row['name'] == player.name)
        self.assertEqual((1, 1, 100.0, 100.0), (row['bat_dots'], row['bowl_dots'], row['dot_percentage'], row['bowling_dot_percentage']))
        self.assertEqual((1, 1, 0, 0), (row['catches'], row['keeper_catches'], row['stumpings'], row['direct_runouts']))
        self.game([self.line(player)])
        self.assertIsNone(next(row for row in self.data()['totals'] if row['name'] == player.name)['catches'])

    def test_simulated_and_unfinished_games_and_private_fields_are_excluded(self):
        player = self.player()
        self.game([self.line(player, user_id=999, private_notes='secret-token')])
        self.game([self.line(player, runs=999)], is_simulated=True)
        self.game([self.line(player, runs=888)], status='live')
        data = self.data()
        self.assertEqual((1, 60), (data['totals'][0]['matches'], data['totals'][0]['runs']))
        text = self.E.csv_bytes(data).decode('utf-8-sig')
        self.assertNotIn('user_id', text)
        self.assertNotIn('secret-token', text)

    def test_download_routes_are_public_scoped_and_validate_options(self):
        player = self.player()
        self.game([self.line(player)])
        self.archive(player)
        response = self.client.get(self.url + '?format=csv&season=all&layout=seasons')
        self.assertEqual(200, response.status_code)
        self.assertEqual('no-store', response.headers['Cache-Control'])
        self.assertIn('attachment;', response.headers['Content-Disposition'])
        self.assertIn('all-seasons-player-stats-seasons-', response.headers['Content-Disposition'])
        records = list(csv.DictReader(io.StringIO(response.data.decode('utf-8-sig'))))
        self.assertEqual(2, len(records))
        self.assertEqual({'60', '40'}, {r['Runs'] for r in records})
        single = self.client.get(self.url + f'?format=csv&season={self.season.id}')
        self.assertEqual(1, len(list(csv.DictReader(io.StringIO(single.data.decode('utf-8-sig'))))))
        for suffix in ('?format=exe', '?layout=private', '?season=999999'):
            self.assertIn(self.client.get(self.url + suffix).status_code, (400, 404))
        other = self.LS.create_series(self.session, f'Other {self.tag}')
        self.session.commit()
        self.assertEqual(404, self.client.get(f'/stats/{other.id}/players/download?season={self.season.id}').status_code)
        self.assertEqual(405, self.client.post(self.url).status_code)
        self.series.is_active = False
        self.session.commit()
        self.assertEqual(404, self.client.get(self.url).status_code)

    def test_workbook_is_readable_and_preserves_types_filters_and_frozen_columns(self):
        player = self.player(name=f'=1+1 & <Star> {self.tag}')
        self.game([self.line(player)])
        response = self.client.get(self.url)
        self.assertEqual(200, response.status_code)
        self.assertIn('spreadsheetml.sheet', response.content_type)
        ns = {'s': self.E._NS}
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            for part in archive.namelist():
                ET.fromstring(archive.read(part))
            workbook = ET.fromstring(archive.read('xl/workbook.xml'))
            self.assertEqual(['Summary', 'Player Stats', 'Season Breakdown', 'Stats Guide'],
                [s.attrib['name'] for s in workbook.findall('s:sheets/s:sheet', ns)])
            sheet = ET.fromstring(archive.read('xl/worksheets/sheet2.xml'))
            self.assertEqual('C2', sheet.find('s:sheetViews/s:sheetView/s:pane', ns).attrib['topLeftCell'])
            self.assertIsNotNone(sheet.find('s:autoFilter', ns))
            row = sheet.findall('s:sheetData/s:row', ns)[1]
            cells = row.findall('s:c', ns)
            self.assertEqual('inlineStr', cells[0].attrib['t'])
            self.assertEqual(player.name, cells[0].find('s:is/s:t', ns).text)
            rating = cells[2]
            self.assertNotIn('t', rating.attrib)
            self.assertEqual('90', rating.find('s:v', ns).text)
            self.assertEqual([], sheet.findall('.//s:f', ns))
        text = self.client.get(self.url + '?format=csv').data.decode('utf-8-sig')
        exported = next(csv.DictReader(io.StringIO(text)))
        self.assertEqual("'" + player.name, exported['Player Name'])

    def test_empty_competition_exports_headers_and_the_current_page_scope_is_selected(self):
        response = self.client.get(self.url + '?format=csv')
        records = list(csv.reader(io.StringIO(response.data.decode('utf-8-sig'))))
        self.assertEqual(1, len(records))
        self.assertEqual(['Player Name', 'Version', 'Rating'], records[0][:3])
        self.assertEqual(len(self.E.COLUMNS), len(records[0]))
        page = self.client.get(f'/stats/{self.series.id}?season={self.season.id}')
        self.assertEqual(200, page.status_code)
        self.assertIn(b'Download Excel', page.data)
        self.assertIn(b'Download CSV', page.data)
        self.assertIn(f'action="{self.url}"'.encode(), page.data)
        select = page.text.split('id="export-season"', 1)[1].split('</select>', 1)[0]
        self.assertIn(f'value="{self.season.id}" selected', select)
        with patch.object(self.admin, 'get_session', return_value=self.session):
            response = self.client.get(self.url)
        self.assertEqual(200, response.status_code)

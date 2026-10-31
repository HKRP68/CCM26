"""Real fixture evidence, linked-season scope and durable franchise records."""
import asyncio
import json
import os
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _tour_fixture as F


def setUpModule(): F.setup_module_db()
def tearDownModule(): F.teardown_module_db()


class RivalryTests(F.FourTeamCase):
    def setUp(self):
        super().setUp()
        from services import franchise_rivalry as R
        self.R = R
        self.teams['Alpha'].short_name = 'ALP'
        self.teams['Bravo'].short_name = 'BRV'
        self.session.flush()

    def season(self, name='Old season', *, same_league=True, names=('Alpha', 'Bravo')):
        from models import Tournament, TournamentTeam
        tour = Tournament(name=name, league_id=self.tour.league_id if same_league else None,
                           status='completed', kind='challenge',
                           created_at=datetime(2025, 1, 1), completed_at=datetime(2025, 12, 1))
        self.session.add(tour)
        self.session.flush()
        teams = [TournamentTeam(tournament_id=tour.id, name=n) for n in names]
        self.session.add_all(teams)
        self.session.flush()
        return tour, teams

    def meeting(self, *, tour=None, teams=None, winner='a', runs=(180, 170),
                text=None, when=None, simulated=False, status='completed'):
        from models import TournamentMatch
        tour = tour or self.tour
        a, b = teams or (self.teams['Alpha'], self.teams['Bravo'])
        fixture = TournamentMatch(
            tournament_id=tour.id, team1_id=a.id, team2_id=b.id,
            winner_team_id=a.id if winner == 'a' else b.id if winner == 'b' else None,
            status=status, result_type=winner if winner in ('tie', 'no_result') else 'win',
            inn1_runs=runs[0], inn2_runs=runs[1], inn1_wickets=5, inn2_wickets=8,
            result_text=text, is_simulated=simulated,
            completed_at=when or datetime(2026, 10, 1), created_at=datetime(2025, 1, 1))
        self.session.add(fixture)
        self.session.flush()
        return fixture

    def compare(self, a='ALP', b='BRV'):
        return self.R.compare(self.session, self.tour, a, b)

    def test_names_span_rebuilt_teams_and_captains_and_scores_follow_franchises(self):
        old, teams = self.season(names=(' alpha ', 'BRAVO'))
        self.meeting()
        self.meeting(tour=old, teams=teams[::-1], winner='a', runs=(201, 190))
        self.teams['Alpha'].owner_tg_id = F.next_tg()
        self.teams['Bravo'].owner_tg_id = F.next_tg()
        foreign, other = self.season('Other league', same_league=False)
        self.meeting(tour=foreign, teams=other, runs=(300, 299))
        record = self.compare()
        self.assertEqual((record['played'], record['wins_a'], record['wins_b']), (2, 1, 1))
        self.assertEqual(record['highest']['a']['runs'], 190)
        self.assertEqual(record['highest']['b']['runs'], 201)
        self.assertEqual(record['available_seasons'], 2)
        self.assertEqual(len(record['seasons']), 2)
        reverse = self.compare('bravo', 'alpha')
        self.assertEqual(reverse['highest']['a']['runs'], 201)

    def test_ties_no_results_super_over_winners_and_unknown_scores_stay_distinct(self):
        self.meeting(winner='tie', runs=(180, 180))
        self.meeting(winner='no_result', runs=(250, None))
        self.meeting(winner='b', runs=(150, 150), text='Bravo won (Super Over)')
        self.meeting(winner='a', runs=(None, None))
        # Equal main-innings scores alone cannot prove a Super Over took place.
        self.meeting(winner='a', runs=(120, 120), text='Alpha won')
        r = self.compare()
        self.assertEqual((r['played'], r['wins_a'], r['wins_b'], r['ties'], r['no_results']),
                         (5, 2, 1, 1, 1))
        self.assertEqual((r['super_a'], r['super_b']), (0, 1))
        self.assertEqual(r['highest']['a']['runs'], 180)  # unfinished NR total excluded

    def test_super_over_marker_can_come_from_underlying_match(self):
        from models import Match
        match = Match(user1_id=self.owners['Alpha'].id, user2_id=self.owners['Bravo'].id,
                       status='completed', margin_type='super_over')
        self.session.add(match)
        self.session.flush()
        fixture = self.meeting(winner='a', runs=(160, 160))
        fixture.match_id = match.id
        self.session.flush()
        self.assertEqual(self.compare()['super_a'], 1)

    def test_unfinished_and_broken_results_do_not_count(self):
        self.meeting(status='live')
        self.meeting(status='scheduled')
        broken = self.meeting()
        broken.winner_team_id = self.teams['Charlie'].id
        no_teams = self.meeting()
        no_teams.team1_id = None
        self.session.flush()
        self.assertEqual(self.compare()['played'], 0)

    def test_explicit_links_replace_defaults_and_skip_missing_sources(self):
        from services import season_archive as SA
        kept, kept_teams = self.season('Included')
        excluded, excluded_teams = self.season('Excluded')
        self.meeting(tour=kept, teams=kept_teams, winner='b')
        self.meeting(tour=excluded, teams=excluded_teams, winner='a')
        SA.set_linked_refs(self.session, self.tour,
                           [SA.tour_ref(kept.id), SA.tour_ref(kept.id), 't:999999',
                            SA.tour_ref(self.tour.id)])
        record = self.compare()
        self.assertEqual((record['played'], record['wins_b'], record['selected_seasons']), (1, 1, 2))

    def test_keep_stats_preserves_result_history_and_links_without_double_counting(self):
        from services import season_archive as SA
        old, teams = self.season()
        self.meeting(tour=old, teams=teams, winner='b', text='Bravo won (Super Over)',
                      runs=(170, 170), simulated=True)
        SA.set_linked_refs(self.session, self.tour, [SA.tour_ref(old.id)])
        before = self.compare()
        saved = SA.delete_tournament(self.session, old, keep_stats=True)
        self.session.commit()
        self.assertEqual(len(json.loads(saved.franchise_results_json)['matches']), 1)
        after = self.compare()
        for field in ('played', 'wins_a', 'wins_b', 'ties', 'super_b', 'simulated', 'highest'):
            self.assertEqual(before[field], after[field], field)
        self.assertEqual(SA.linked_refs(self.tour), [SA.saved_ref(saved.id)])
        SA.delete_saved_season(self.session, saved.id)
        self.assertEqual(self.compare()['played'], 0)

    def test_delete_without_stats_does_not_retain_rivalry(self):
        from services import season_archive as SA
        old, teams = self.season()
        self.meeting(tour=old, teams=teams)
        self.assertEqual(self.compare()['played'], 1)
        SA.delete_tournament(self.session, old, keep_stats=False)
        self.assertEqual(self.compare()['played'], 0)

    def test_legacy_saved_seasons_report_missing_evidence(self):
        from models import StatsSeason
        for name, raw in [('Legacy', None), ('Broken', '{bad json'),
                          ('Empty', '{"teams": [], "matches": []}')]:
            self.session.add(StatsSeason(name=name, league_id=self.tour.league_id,
                                         franchise_results_json=raw))
        self.session.flush()
        r = self.compare()
        self.assertEqual((r['played'], r['available_seasons'], r['selected_seasons']), (0, 2, 4))

    def test_ambiguous_aliases_and_partial_names_require_full_name(self):
        self.teams['Charlie'].name = 'Alpha United'
        self.teams['Charlie'].short_name = 'ALP'
        self.session.flush()
        with self.assertRaisesRegex(ValueError, 'More than one'):
            self.compare('ALP', 'BRV')
        self.assertEqual(self.compare('Alpha', 'Bravo')['team_a'], 'Alpha')
        with self.assertRaisesRegex(ValueError, 'two different'):
            self.compare('Alpha', 'Alpha')
        with self.assertRaisesRegex(ValueError, 'not found'):
            self.compare('Missing', 'Bravo')

    def test_latest_five_are_chronological_and_simulated_replacement_counts_once(self):
        from models import TournamentMatch
        for day in range(1, 8):
            self.meeting(winner='a' if day % 2 else 'b', when=datetime(2026, 10, day),
                          simulated=(day == 7))
        latest = self.session.query(TournamentMatch).filter_by(
            tournament_id=self.tour.id, completed_at=datetime(2026, 10, 7)).one()
        self.assertEqual(self.compare()['simulated'], 1)
        latest.is_simulated = False  # a real replay replaces the same fixture
        latest.winner_team_id = self.teams['Bravo'].id
        self.session.flush()
        r = self.compare()
        self.assertEqual((r['played'], r['simulated']), (7, 0))
        self.assertEqual([m['when'][:10] for m in r['recent']],
                         [f'2026-10-{day:02}' for day in range(7, 2, -1)])

    def test_handler_renders_escaped_names_and_command_errors(self):
        from handlers import franchise_rivalry as H
        self.teams['Alpha'].name = '<Alpha>'
        self.tour.name = '<Cup>'
        self.meeting(winner='b', simulated=True)
        self.session.commit()
        update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
        context = SimpleNamespace(args=[f'#{self.tour.id}', 'ALP', '|', 'BRV'])
        asyncio.run(H.frivalry_handler(update, context))
        text = update.message.reply_text.call_args.args[0]
        self.assertIn('&lt;Alpha&gt;', text)
        self.assertIn('&lt;Cup&gt;', text)
        self.assertIn('Wins A / B: <b>0 / 1</b>', text)
        self.assertIn('[sim]', text)
        self.assertNotIn('<Alpha>', text)
        self.assertLess(len(text), 4096)
        context.args = [f'#{self.tour.id}', 'ALP']
        asyncio.run(H.frivalry_handler(update, context))
        self.assertIn('Use /frivalry', update.message.reply_text.call_args.args[0])

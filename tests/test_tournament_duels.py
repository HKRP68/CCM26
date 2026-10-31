import asyncio
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _tour_fixture as F


def setUpModule(): F.setup_module_db()
def tearDownModule(): F.teardown_module_db()


class DuelTests(F.FourTeamCase):
    def snapshot(self):
        from services.tournament_duels import record_ball
        s = dict(tournament_id=self.tour.id, bat_team_id=1, bowl_team_id=2,
                 bat_team_name='Alpha', bowl_team_name='Bravo')
        bat = dict(roster_id=11, name='Virat Kohli')
        bowl = dict(roster_id=22, name='Jasprit Bumrah')
        for oc in [dict(type='run', runs=4), dict(type='run', runs=0),
                   dict(type='extra', is_extra=True, extra_type='Wide', runs=2),
                   dict(type='extra', is_extra=True, extra_type='No Ball', runs=6),
                   dict(type='extra', is_extra=True, extra_type='Leg Bye', runs=2),
                   dict(type='wicket', runs=1, wicket_type='Run Out'),
                   dict(type='wicket', runs=0, wicket_type='Caught')]:
            record_ball(s, bat, bowl, oc)
        return s

    def test_deliveries_distinguish_runs_extras_balls_and_bowler_dismissals(self):
        from services.tournament_duels import compare
        s = self.snapshot()
        row = compare(s['tournament_duels'], 'Kohli', 'Bumrah')
        self.assertEqual({k: row[k] for k in ('runs', 'balls', 'legal_balls', 'dots', 'fours', 'sixes', 'dismissals')},
                         dict(runs=11, balls=6, legal_balls=5, dots=2, fours=1, sixes=1, dismissals=1))

    def test_archive_is_tournament_scoped_and_ignores_unfinished_matches(self):
        from services.tournament_duels import archived_duels, compare
        fixture = self.fixtures(1)[0]
        s = self.snapshot()
        fixture.scorecard_json = json.dumps([dict(name='Virat Kohli', player_duels=s['tournament_duels'])])
        self.session.flush()
        self.assertEqual(archived_duels(self.session, self.tour.id), [])
        fixture.status = 'completed'
        self.session.flush()
        row = compare(archived_duels(self.session, self.tour.id), 'Virat Kohli', 'Jasprit Bumrah')
        self.assertEqual(row['runs'], 11)
        self.assertEqual(archived_duels(self.session, -99), [])

    def test_duplicate_player_names_require_the_team(self):
        from services.tournament_duels import compare
        s = self.snapshot()
        other = json.loads(json.dumps(s['tournament_duels'][0]))
        other['batter'].update(team='Charlie', roster_id=33)
        rows = s['tournament_duels'] + [other]
        with self.assertRaises(ValueError): compare(rows, 'Virat Kohli', 'Bumrah')
        self.assertEqual(compare(rows, 'Virat Kohli @ Alpha', 'Bumrah')['runs'], 11)

    def test_command_renders_real_encounters_and_escapes_names(self):
        from handlers import tournament_duels as H
        from database import get_session
        fixture = self.fixtures(1)[0]
        fixture.status = 'completed'
        fixture.scorecard_json = json.dumps([{'player_duels': self.snapshot()['tournament_duels']}])
        self.tour.name = '<Cup>'
        self.session.commit()
        update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
        context = SimpleNamespace(args=[f'#{self.tour.id}', 'Virat', 'Kohli', '|', 'Bumrah'])
        with patch.object(H, 'get_session', get_session):
            asyncio.run(H.tvs_handler(update, context))
        text = update.message.reply_text.call_args.args[0]
        self.assertIn('&lt;Cup&gt;', text)
        self.assertIn('Runs: <b>11</b>', text)
        self.assertIn('Dismissals: <b>1</b>', text)

    def test_captain_record_uses_super_over_winner_and_either_batting_order(self):
        from handlers.tournament_duels import captain_record
        def m(host, target, winner):
            return SimpleNamespace(host_user_id=host, target_user_id=target,
                                   team1_id=10, team2_id=20, winner_team_id=winner)
        self.assertEqual(captain_record([m(1, 2, 10), m(2, 1, 20), m(1, 2, 20),
                                        m(2, 1, None), m(3, 1, 20)], 1, 2), (2, 1, 1))

    def test_season_scope_merges_rosters_and_survives_archive_and_delete(self):
        from models import Tournament, TournamentMatch
        from services import season_archive as SA, tournament_duels as D
        fixture = self.fixtures(1)[0]
        fixture.status = 'completed'
        fixture.scorecard_json = json.dumps([{'player_duels': self.snapshot()['tournament_duels']}])
        previous = Tournament(name='Previous season', league_id=self.tour.league_id,
                              status='completed', kind='challenge')
        unrelated = Tournament(name='Other competition', status='completed')
        self.session.add_all([previous, unrelated])
        self.session.flush()
        rows = self.snapshot()['tournament_duels']
        for r in rows:
            r['batter']['roster_id'] += 100
            r['bowler']['roster_id'] += 100
        for t in (previous, unrelated):
            self.session.add(TournamentMatch(tournament_id=t.id, status='completed',
                scorecard_json=json.dumps([{'player_duels': rows}])))
        self.session.flush()
        encounters, recorded, total = D.season_duels(self.session, self.tour)
        self.assertEqual((recorded, total), (2, 2))
        self.assertEqual(D.compare(encounters, 'Kohli', 'Bumrah', across_seasons=True)['runs'], 22)
        # An explicit link excludes the unrelated tournament, even after deletion.
        SA.set_linked_refs(self.session, self.tour, [SA.tour_ref(previous.id)])
        saved = SA.delete_tournament(self.session, previous, keep_stats=True)
        encounters, recorded, total = D.season_duels(self.session, self.tour)
        self.assertEqual(SA.linked_refs(self.tour), [SA.saved_ref(saved.id)])
        self.assertEqual((recorded, total), (2, 2))
        self.assertEqual(D.compare(encounters, 'Kohli', 'Bumrah', across_seasons=True)['runs'], 22)

    def test_svs_coverage_includes_legacy_seasons_without_inventing_deliveries(self):
        from models import StatsSeason
        from services import tournament_duels as D
        from handlers import tournament_duels as H
        legacy = StatsSeason(name='Legacy', league_id=self.tour.league_id)
        self.session.add(legacy)
        fixture = self.fixtures(1)[0]
        fixture.status = 'completed'
        fixture.scorecard_json = json.dumps([{'player_duels': self.snapshot()['tournament_duels']}])
        self.session.commit()
        self.assertEqual(D.season_duels(self.session, self.tour)[1:], (1, 2))
        update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
        context = SimpleNamespace(args=[f'#{self.tour.id}', 'Kohli', '|', 'Bumrah'])
        asyncio.run(H.svs_handler(update, context))
        text = update.message.reply_text.call_args.args[0]
        self.assertIn('Runs: <b>11</b>', text)
        self.assertIn('1/2 seasons', text)

    def test_season_duplicate_names_still_require_team(self):
        from services.tournament_duels import compare
        rows = self.snapshot()['tournament_duels']
        other = json.loads(json.dumps(rows[0]))
        other['batter']['team'] = 'Charlie'
        with self.assertRaises(ValueError):
            compare(rows + [other], 'Kohli', 'Bumrah', across_seasons=True)
        self.assertEqual(compare(rows + [other], 'Kohli @ Alpha', 'Bumrah',
                                 across_seasons=True)['runs'], 11)

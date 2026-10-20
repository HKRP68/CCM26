"""Ceremony award selection uses actual statistics and explicit catch evidence."""
import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _tour_fixture as F


def setUpModule(): F.setup_module_db()
def tearDownModule(): F.teardown_module_db()


class CategoryTests(F.FourTeamCase):
    def data(self):
        from models import TournamentTeam, ChallengePlayer, User
        self.finish_league()
        for fx in self.fixtures(stage='semifinal'): self.ts.simulate_fixture(self.session, fx.id, '1')
        final = self.fixtures(stage='final')[0]
        self.ts.simulate_fixture(self.session, final.id, '1')
        team = self.session.get(TournamentTeam, final.winner_team_id)
        other = self.session.get(TournamentTeam, final.team2_id)
        user = self.owners[team.name]
        cp = ChallengePlayer(team_id=team.challenge_team_id, name='Rookie Star', details_json='{"rating":80}')
        self.session.add(cp); self.session.flush()
        def line(name, team_name=team.name, **stats):
            row = dict(name=name, team_name=team_name, user_id=user.id, player_id=None,
                roster_id=cp.id if name == 'Rookie Star' else None, batted=False, bat_runs=0, bat_balls=0,
                bat_fours=0, bat_sixes=0, bat_out=False, bowled=False, bowl_runs=0, bowl_balls=0, bowl_wickets=0, bowl_dots=0)
            row.update(stats); return row
        event = dict(seq=1, innings=1, phase='powerplay', team=other.name, opponent=team.name,
            pair=[dict(name='Caught Batter')], batter='Caught Batter', bowler='Quick Bowler', legal=True,
            team_runs=0, bat_runs=0, faced=1, conceded=0, wicket=True, bowler_wicket=True,
            wicket_number=1, how='caught', fielder='Safe Hands', keeper=False)
        lines = [line('Rookie Star', batted=True, bat_runs=100, bat_balls=40, bat_fours=10, bat_sixes=6,
                      bowled=True, bowl_wickets=3, bowl_balls=24, bowl_runs=20, bowl_dots=18),
                 line('Finisher', batted=True, bat_runs=15, bat_balls=25),
                 line('Caught Batter', team_name=other.name, batted=True, bat_balls=1, bat_out=True),
                 line('Quick Bowler', bowled=True, bowl_wickets=1, bowl_balls=1, bowl_dots=1), line('Safe Hands')]
        lines[0]['record_detail'] = dict(version=1, innings_starts={}, deliveries=[event])
        lines[0]['match_potm'] = dict(name='Finisher', team=team.name)
        final.scorecard_json = json.dumps(lines)
        captain = User(telegram_id=F.next_tg(), first_name='Actual Final Captain')
        self.session.add(captain); self.session.flush(); final.host_user_id = captain.id
        self.ts.recompute_tournament(self.session, self.tour.id); self.session.commit()
        return final, team, user, captain

    def test_all_eleven_categories_choose_the_correct_evidence(self):
        from services import tournament_awards as A
        final, team, user, captain = self.data()
        choices = A.catch_choices(self.session, self.tour.id)
        self.assertEqual(len(choices), 1)
        A.set_best_catch(self.session, self.tour, choices[0]['key'])
        rows = {r[0]: r for r in A.winners(self.session, self.tour)}
        for award in ('orange_cap', 'purple_cap', 'most_sixes', 'most_fours', 'mvp', 'emerging_player', 'highest_sr', 'most_dots'):
            self.assertEqual(rows[award][2], 'Rookie Star', award)
        self.assertIn('250.00', rows['highest_sr'][4])
        self.assertEqual(rows['player_of_final'][2], 'Finisher')
        self.assertEqual(rows['best_catch'][2], 'Safe Hands')
        self.assertEqual(rows['most_dots'][4], '18 dot balls')
        self.assertEqual(rows['winning_captain'][2:4], ('Actual Final Captain', captain.id))
        self.assertEqual(len(A.CEREMONY_AWARDS), 11)

    def test_highest_strike_rate_obeys_the_tournament_minimum(self):
        from services import tournament_awards as A
        self.data(); self.tour.min_balls_for_sr = 50
        rows = {r[0]:r for r in A.winners(self.session, self.tour)}
        self.assertIsNone(rows['highest_sr'][2])
        self.assertIn('No qualifying', rows['highest_sr'][4])

    def test_best_catch_rejects_invalid_or_corrected_evidence(self):
        from services import tournament_awards as A
        final, team, user, captain = self.data()
        with self.assertRaises(ValueError): A.set_best_catch(self.session, self.tour, '99999:2')
        choice = A.catch_choices(self.session, self.tour.id)[0]
        A.set_best_catch(self.session, self.tour, choice['key'])
        self.assertEqual(A.selected_catch(self.session, self.tour)['name'], 'Safe Hands')
        lines = json.loads(final.scorecard_json); lines[2]['bat_runs'] = 9
        final.scorecard_json = json.dumps(lines)
        self.assertIsNone(A.selected_catch(self.session, self.tour))

    def test_new_awards_backfill_finished_tournament_and_pay_only_once(self):
        from services import tournament_awards as A
        from models import TournamentHonour
        final, team, user, captain = self.data()
        old = ('champion','runner_up','orange_cap','purple_cap','mvp','emerging_player')
        self.session.query(TournamentHonour).filter(TournamentHonour.tournament_id == self.tour.id,
            ~TournamentHonour.award.in_(old)).delete(synchronize_session=False)
        A.set_prize(self.tour, 'sixes', 800, 4); self.session.commit()
        before = user.total_coins or 0
        A.ensure_ceremony_honours(self.session, self.tour); self.session.commit()
        self.assertEqual(user.total_coins, before+800)
        A.ensure_ceremony_honours(self.session, self.tour); self.session.commit()
        self.assertEqual(user.total_coins, before+800)
        self.assertEqual(len(A.ceremony_honours(self.session, self.tour.id)), 11)

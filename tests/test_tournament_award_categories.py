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
        rows = {r[0]: r for r in A.winners(self.session, self.tour)}
        for award in ('orange_cap', 'purple_cap', 'most_sixes', 'most_fours', 'mvp', 'emerging_player', 'highest_sr', 'most_dots'):
            self.assertEqual(rows[award][2], 'Rookie Star', award)
        self.assertIn('250.00', rows['highest_sr'][4])
        self.assertEqual(rows['player_of_final'][2], 'Finisher')
        self.assertEqual(rows['most_catches'][2:4], ('Safe Hands', user.id))
        self.assertEqual(rows['most_catches'][4], '1 catch')
        self.assertEqual(rows['most_dots'][4], '18 dot balls')
        self.assertEqual(rows['winning_captain'][2:4], ('Actual Final Captain', captain.id))
        self.assertEqual(len(A.CEREMONY_AWARDS), 11)

    def test_highest_strike_rate_obeys_the_tournament_minimum(self):
        from services import tournament_awards as A
        self.data(); self.tour.min_balls_for_sr = 50
        rows = {r[0]:r for r in A.winners(self.session, self.tour)}
        self.assertIsNone(rows['highest_sr'][2])
        self.assertIn('No qualifying', rows['highest_sr'][4])

    def test_most_catches_excludes_corrected_evidence(self):
        from services import tournament_awards as A
        final, team, user, captain = self.data()
        self.assertEqual(A.catch_leaders(self.session, self.tour.id)[0]['name'], 'Safe Hands')
        lines = json.loads(final.scorecard_json); lines[2]['bat_runs'] = 9
        final.scorecard_json = json.dumps(lines)
        self.assertEqual(A.catch_leaders(self.session, self.tour.id), [])

    def test_catches_accumulate_across_captains_and_include_keeper_and_caught_bowled(self):
        from services import tournament_awards as A
        from models import TournamentMatch
        final, team, user, captain = self.data()
        lines = json.loads(final.scorecard_json)
        detail = lines[0]['record_detail']
        # A partial historical stream can still prove individual fielding events.
        detail['innings_starts'] = {'1': {'complete': False}}
        first = detail['deliveries'][0]
        detail['deliveries'] = [dict(first, seq=i, how=how, keeper=keeper)
            for i, (how, keeper) in enumerate([('caught behind', True), ('c&b', False),
                                             ('stumped', True), ('run out', False)], 1)]
        # Another controlling user does not split one player's tournament tally.
        lines[-1]['user_id'] = captain.id
        match = TournamentMatch(tournament_id=self.tour.id, stage='league', status='completed',
            team1_id=final.team1_id, team2_id=final.team2_id, scorecard_json=json.dumps(lines))
        self.session.add(match); self.session.flush()
        leaders = A.catch_leaders(self.session, self.tour.id)
        self.assertEqual([(r['name'], r['catches']) for r in leaders], [('Safe Hands', 3)])
        match.status = 'live'
        self.session.flush()
        self.assertEqual(A.catch_leaders(self.session, self.tour.id)[0]['catches'], 1)

    def test_previous_catch_prize_carries_to_replacement(self):
        from services import tournament_awards as A
        self.tour.prizes_json = json.dumps({'best_catch': {'coins': 120, 'gems': 2}})
        self.assertEqual(A.prizes(self.tour)['most_catches'], {'coins': 120, 'gems': 2})
        self.assertEqual(A.set_prize(self.tour, 'catches', 400, 1), 'most_catches')
        self.assertNotIn('best_catch', json.loads(self.tour.prizes_json))
        self.assertEqual(A.prizes(self.tour)['most_catches']['coins'], 400)

    def test_finished_ceremony_replaces_old_category_without_repaying_other_awards(self):
        from services import tournament_awards as A
        from models import TournamentHonour
        self.data()
        self.session.query(TournamentHonour).filter_by(tournament_id=self.tour.id, award='most_catches').delete()
        self.session.add(TournamentHonour(tournament_id=self.tour.id, tournament_name=self.tour.name,
            award='best_catch', player_name='Old manual selection'))
        A.set_prize(self.tour, 'catches', 120, 2)
        self.session.commit()
        catch_winner = next(r for r in A.winners(self.session, self.tour) if r[0] == 'most_catches')
        from models import User
        user = self.session.get(User, catch_winner[3])
        before = user.total_coins or 0
        for _ in range(2):
            A.ensure_ceremony_honours(self.session, self.tour)
            self.session.commit()
        self.assertEqual(user.total_coins, before+120)
        awards = [h.award for h in A.ceremony_honours(self.session, self.tour.id)]
        self.assertEqual(awards, list(A.CEREMONY_AWARDS))
        self.assertNotIn('best_catch', awards)

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

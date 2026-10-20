"""Fixture conditions, private tie control and resumable ceremony behavior."""
import asyncio
import io
import json
import os
import random
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _tour_fixture as F


def setUpModule():
    F.setup_module_db()


def tearDownModule():
    F.teardown_module_db()


class PresentationTests(F.FourTeamCase):
    def test_night_day_rotation_and_playoff_conditions(self):
        from models import TournamentMatch
        from services import tournament_match_settings as S
        from services.pitch_report import generate_conditions
        self.tour.playoff_conditions_json = json.dumps({'pitches': ['Green'], 'stadiums': ['Wankhede Stadium']})
        fixtures = self.lss.list_fixtures(self.session, self.tour.id)
        self.assertEqual([S.fixture_period(self.session, f) for f in fixtures], ['Night'] * 4 + ['Day'] * 2)
        playoff = TournamentMatch(tournament_id=self.tour.id, stage='final', status='scheduled', match_no=7)
        self.session.add(playoff)
        self.session.flush()
        self.lss.assign_fixture_venues(self.session, self.tour.id, overwrite=True)
        self.assertEqual(playoff.pitch_type, 'Green')
        self.assertEqual(playoff.venue, 'Wankhede Stadium')
        draft = {}
        S.apply_to_draft(self.session, playoff, draft)
        self.assertEqual(draft['conditions']['day_night'], 'Night')
        self.assertEqual(draft['pitch_type'], 'Green')
        # Replanning preferred league pitches must preserve playoff surfaces.
        self.tour.pitch_mode = 'tour_preferred'
        self.lss.assign_fixture_venues(self.session, self.tour.id, overwrite=True)
        self.assertEqual(playoff.pitch_type, 'Green')
        for seed in range(10):
            self.assertEqual(generate_conditions('Green', random.Random(seed), day_night='Day')['dew'], None)

    def test_settings_validate_scope_and_unstarted_status_and_stay_private(self):
        from services import tournament_match_settings as S, tournament_webapp
        fx = self.fixtures(1)[0]
        S.set_super_overs(self.session, self.tour, fx.id, 2)
        draft = {}
        S.apply_to_draft(self.session, fx, draft)
        self.assertEqual(draft['_planned_super_overs'], 2)
        public = tournament_webapp._fixture(fx, {}, fx.id)
        self.assertNotIn('planned_super_overs', json.dumps(public))
        from services.match_webapp_service import strip_hidden_plans
        self.assertNotIn('_planned_super_overs', strip_hidden_plans({'mode': 'cipl_approach', '_planned_super_overs': 2}))
        with self.assertRaises(ValueError):
            S.set_super_overs(self.session, SimpleNamespace(id=-1), fx.id, 1)
        with self.assertRaises(ValueError):
            S.set_super_overs(self.session, self.tour, fx.id, 3)
        fx.status = 'live'
        with self.assertRaises(ValueError):
            S.set_super_overs(self.session, self.tour, fx.id, 1)

    def test_chase_control_finishes_level_without_rewriting_scores(self):
        from services.tournament_match_settings import constrain_tie_outcome
        rng = random.Random(8)
        # Include extras and repeated terminal wickets, and test very low targets.
        for par in [0, 1, 10, 36, 95, 240, 360]:
            score, wickets = 0, 0
            for remaining in range(60, 0, -1):
                oc = rng.choice([dict(type='wicket', runs=0, batter_out=True),
                                 dict(type='extra', runs=6, is_extra=True, extra_type='No Ball'),
                                 dict(type='runs', runs=6)])
                oc = constrain_tie_outcome(oc, runs=score, par=par, remaining=remaining,
                                          wickets=wickets, wicket_limit=10)
                self.assertFalse(oc.get('is_extra'))
                score += oc['runs']
                wickets += int(bool(oc.get('batter_out')))
                self.assertLessEqual(score, par)
                if wickets == 10:
                    break
            self.assertEqual(score, par)

    def test_playoffs_are_separate_in_html_and_rich_views(self):
        from models import TournamentMatch
        from services import cl_tournament_view as V, cl_tournament_rich as R
        fx = TournamentMatch(tournament_id=self.tour.id, stage='final', round_no=1,
                             match_no=7, status='scheduled')
        self.session.add(fx)
        self.session.flush()
        text = V.render_fixtures(self.session, self.tour)
        self.assertIn('League stage', text)
        self.assertGreater(text.index('Playoffs — Final'), text.index('League stage'))
        self.assertIn('Playoffs — Final', str(R.fixtures_blocks(self.session, self.tour)))

    def finish_tournament(self):
        self.finish_league()
        for fx in self.fixtures(stage='semifinal'):
            self.ts.simulate_fixture(self.session, fx.id, '1')
        self.ts.simulate_fixture(self.session, self.fixtures(stage='final')[0].id, '1')
        self.session.commit()

    def test_ceremony_resumes_after_failed_photo_and_never_repays(self):
        from services import tournament_awards as A, tournament_ceremony as C
        A.set_prize(self.tour, 'champion', 5000, 0)
        self.finish_tournament()
        bot = SimpleNamespace(send_photo=AsyncMock(side_effect=[SimpleNamespace(message_id=1), RuntimeError('network')]))
        context = SimpleNamespace(bot=bot)
        with patch.object(C, 'award_photo', return_value=b'photo'), patch.object(C.asyncio, 'sleep', new=AsyncMock()):
            with self.assertRaises(RuntimeError):
                asyncio.run(C.present(context, self.tour.id, -10055))
            self.session.expire_all()
            honours = A.honours_for(self.session, self.tour.id)
            self.assertIsNotNone(honours[0].announced_at)
            self.assertIsNone(honours[1].announced_at)
            bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=2))
            asyncio.run(C.present(context, self.tour.id, -10055))
            self.assertEqual(bot.send_photo.await_count, len(honours) - 1)
            with self.assertRaises(ValueError):
                asyncio.run(C.present(context, self.tour.id, -10055))
        self.session.expire_all()
        self.assertEqual(sum(o.total_coins for o in self.owners.values()), 5000)

    def test_emerging_player_uses_low_rated_performance(self):
        from models import ChallengePlayer
        from services import tournament_awards as A
        from services import tournament_mvp as M
        low = ChallengePlayer(team_id=self.teams['Alpha'].challenge_team_id, name='Rookie', details_json='{"rating":80}')
        high = ChallengePlayer(team_id=self.teams['Bravo'].challenge_team_id, name='Star', details_json='{"rating":95}')
        self.session.add_all([low, high])
        self.session.flush()
        rows = [SimpleNamespace(name=p.name, team_name=team, player_id=None, matches=5, points=points)
                for p, team, points in [(low, 'Alpha', 500), (high, 'Bravo', 600)]]
        with patch.object(M, 'mvp_rows', return_value=rows):
            row, rating = A.emerging_player(self.session, self.tour)
            self.assertEqual(row.name, 'Rookie')
            self.assertEqual(rating, 80)
        A.set_prize(self.tour, 'emerging', 1500, 5)
        self.assertEqual(A.prizes(self.tour)['emerging_player'], {'coins': 1500, 'gems': 5})

    def test_award_image_and_caption_contain_team_and_prize(self):
        from PIL import Image
        from services import tournament_awards as A, tournament_ceremony as C
        A.set_prize(self.tour, 'champion', 5000, 50)
        self.finish_tournament()
        honour = A.honours_for(self.session, self.tour.id)[0]
        self.assertIn(honour.team_name, C.award_caption(self.session, self.tour, honour))
        self.assertIn('5,000 coins + 50 gems', C.award_caption(self.session, self.tour, honour))
        photo = C.award_photo(self.session, self.tour, honour)
        self.assertEqual(Image.open(io.BytesIO(photo)).size, (1000, 1120))

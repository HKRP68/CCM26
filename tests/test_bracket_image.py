"""The playoff bracket picture, and the watch posting it."""

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _tour_fixture as F  # noqa: E402


def setUpModule():
    F.setup_module_db()


def tearDownModule():
    F.teardown_module_db()


PNG = b"\x89PNG"


class Top4Tests(F.FourTeamCase):
    def test_no_bracket_no_picture(self):
        from services import bracket_image
        self.assertIsNone(bracket_image.render(self.session, self.tour))
        self.assertIsNone(bracket_image.state_key(self.session, self.tour.id))

    def test_tbd_then_teams_then_champion(self):
        from services import bracket_image, knockout_service
        knockout_service.generate_knockout(self.session, self.tour.id)
        # Seeded from an empty table: semis have teams, the final is TBD.
        png = bracket_image.render(self.session, self.tour)
        self.assertTrue(png.startswith(PNG))
        k1 = bracket_image.state_key(self.session, self.tour.id)
        for fx in self.fixtures(stage="semifinal"):
            self.ts.simulate_fixture(self.session, fx.id, "1")
        k2 = bracket_image.state_key(self.session, self.tour.id)
        self.assertNotEqual(k1, k2)
        final = self.fixtures(stage="final")[0]
        self.ts.simulate_fixture(self.session, final.id, "2")
        self.assertTrue(bracket_image.render(self.session, self.tour).startswith(PNG))

    def test_the_watch_posts_it_on_seeding_and_each_result(self):
        from services import tournament_watch as tw
        now = datetime(2026, 10, 5)
        tw.tick(self.session, self.tour, now=now)
        self.finish_league()
        kinds = [p.kind for p in tw.tick(self.session, self.tour,
                                         now=now + timedelta(minutes=2))]
        self.assertIn("bracket", kinds)
        self.assertEqual([p for p in tw.tick(self.session, self.tour,
                                             now=now + timedelta(minutes=4))
                          if p.kind == "bracket"], [])
        semi = self.fixtures(stage="semifinal")[0]
        self.ts.simulate_fixture(self.session, semi.id, "1")
        posts = [p for p in tw.tick(self.session, self.tour,
                                    now=now + timedelta(minutes=6))
                 if p.kind == "bracket"]
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0].photo.startswith(PNG))


class IplTests(F.FourTeamCase):
    KNOCKOUT = "ipl_playoffs"

    def test_ipl_playoffs_draw(self):
        from services import bracket_image
        self.finish_league()
        self.assertTrue(bracket_image.render(self.session, self.tour).startswith(PNG))


class PureKnockoutTests(F.FourTeamCase):
    KNOCKOUT = None
    NAMES = ("Alpha", "Bravo", "Charlie", "Delta", "Echo")

    def setUp(self):
        super().setUp()
        from services import knockout_service
        from models import TournamentMatch
        self.tour.knockout_type = "pure_knockout"
        self.session.query(TournamentMatch).filter_by(tournament_id=self.tour.id).delete()
        knockout_service.generate_knockout(self.session, self.tour.id)

    def test_byes_and_tbd_slots_draw(self):
        from services import bracket_image
        self.assertTrue(bracket_image.render(self.session, self.tour).startswith(PNG))

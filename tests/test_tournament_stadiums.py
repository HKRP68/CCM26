"""Tournament stadiums come from Stadium Data — home grounds, lists, kickoff."""

import os
import random
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _tour_fixture as F  # noqa: E402


def setUpModule():
    F.setup_module_db()


def tearDownModule():
    F.teardown_module_db()


class StadiumTests(F.FourTeamCase):
    def setUp(self):
        super().setUp()
        from services import tournament_stadiums
        self.TS = tournament_stadiums

    def test_names_resolve_against_stadium_data(self):
        self.assertEqual(self.TS.resolve("Wankhede"), "Wankhede Stadium")
        self.assertEqual(self.TS.resolve("MCG"), "Melbourne Cricket Ground")
        self.assertIsNone(self.TS.resolve("Nowhere Park"))

    def test_a_stadium_added_to_stadium_data_is_usable(self):
        from engine.sim import stadium
        extra = {"name": "Brand New Oval", "aliases": ["BNO"], "city": "Testville",
                 "country": "India", "altitudeMeters": 10,
                 "boundaryM": {"squareLeg": 65, "fineLeg": 62, "longOn": 70, "longOff": 70},
                 "outfieldSpeed": 70, "dewFactor": 40, "typicalPitch": "Flat",
                 "avgFirstInningsScore": 170, "slope": False,
                 "climate": {"cloudCover": 30, "humidity": 60, "rainChance": 10,
                             "temperatureC": 28, "windKPH": 10}}
        rows = stadium.file_rows() + [extra]
        with mock.patch.object(stadium, "live_rows", return_value=rows):
            self.assertEqual(self.TS.resolve("BNO"), "Brand New Oval")
            self.assertEqual(self.TS.add_tour_stadium(self.tour, "Brand New Oval"),
                             "Brand New Oval")

    def test_unknown_names_are_refused(self):
        with self.assertRaises(ValueError):
            self.TS.set_tour_stadiums(self.tour, ["Nowhere Park"])
        with self.assertRaises(ValueError):
            self.TS.set_home_stadium(self.session, self.teams["Alpha"], "Nowhere Park")

    def test_league_matches_are_played_at_the_home_ground(self):
        alpha = self.teams["Alpha"]
        self.TS.set_home_stadium(self.session, alpha, "Eden Gardens")
        for fx in self.fixtures():
            if fx.home_team_id == alpha.id:
                self.assertEqual(fx.venue, "Eden Gardens")
            else:
                self.assertIsNone(fx.venue)        # no list, no home → random as before

    def test_the_tournament_list_fills_the_rest(self):
        self.TS.set_tour_stadiums(self.tour, ["Lord's", "The Oval"])
        self.TS.set_home_stadium(self.session, self.teams["Alpha"], "Eden Gardens")
        self.TS.assign_venues(self.session, self.tour.id, overwrite=True,
                              rng=random.Random(1))
        for fx in self.fixtures():
            if fx.home_team_id == self.teams["Alpha"].id:
                self.assertEqual(fx.venue, "Eden Gardens")
            else:
                self.assertIn(fx.venue, ("Lord's Cricket Ground", "The Oval"))

    def test_knockouts_are_neutral(self):
        self.TS.set_tour_stadiums(self.tour, ["Lord's"])
        self.TS.set_home_stadium(self.session, self.teams["Alpha"], "Eden Gardens")
        self.finish_league()
        semis = self.fixtures(stage="semifinal")
        self.assertTrue(semis)
        self.assertTrue(all(fx.venue == "Lord's Cricket Ground" for fx in semis))

    def test_played_fixtures_keep_their_ground(self):
        self.TS.set_home_stadium(self.session, self.teams["Alpha"], "Eden Gardens")
        played = [fx for fx in self.fixtures(1)
                  if fx.home_team_id == self.teams["Alpha"].id][0]
        self.ts.simulate_fixture(self.session, played.id, "1")
        self.TS.set_home_stadium(self.session, self.teams["Alpha"], "Wankhede")
        self.assertEqual(played.venue, "Eden Gardens")

    def test_kickoff_falls_back_when_a_ground_left_stadium_data(self):
        fx = self.fixtures(1)[0]
        fx.venue = "Demolished Park"
        self.TS.set_tour_stadiums(self.tour, ["Sabina Park"])
        self.assertEqual(self.TS.kickoff_stadium(self.session, fx), "Sabina Park")
        self.tour.stadiums_json = None
        self.assertIsNone(self.TS.kickoff_stadium(self.session, fx))
        fx.venue = "Wankhede"
        self.assertEqual(self.TS.kickoff_stadium(self.session, fx), "Wankhede Stadium")

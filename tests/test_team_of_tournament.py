"""Team of the Tournament — role balance, fallback, captain."""

import unittest
from types import SimpleNamespace
from unittest import mock

from services import team_of_tournament as TOT


def _row(i, name, points, bat=0.0, bowl=0.0, matches=5):
    return SimpleNamespace(name=name, team_name="T", player_id=i, roster_id=None,
                           points=points, bat_points=bat, bowl_points=bowl,
                           awards=0, wins=0, matches=matches, bat_runs=10,
                           bat_balls=10, bowl_wickets=1, bowl_balls=6)


CATS = {}


def _player(session, row):
    cat = CATS.get(row.player_id)
    return SimpleNamespace(id=row.player_id, category=cat, rating=80) if cat else None


class PickTests(unittest.TestCase):
    def pick(self, rows, cats):
        CATS.clear()
        CATS.update(cats)
        with mock.patch("services.tournament_mvp.mvp_rows", return_value=rows), \
             mock.patch.object(TOT, "_player_for", side_effect=_player):
            return TOT.pick_xi(None, 1)

    def test_a_balanced_xi(self):
        rows, cats = [], {}
        for i in range(20):
            role = ("Wicket Keeper", "Batsman", "All-rounder", "Bowler")[i % 4]
            rows.append(_row(i, f"P{i}", 100 - i))
            cats[i] = role
        xi = self.pick(rows, cats)
        self.assertEqual(len(xi), 11)
        roles = [c.role for c in xi]
        self.assertEqual((roles.count("wk"), roles.count("bat"), roles.count("ar"),
                          roles.count("bowl")), (1, 4, 2, 4))
        self.assertEqual([c for c in xi if c.captain][0].row.name, "P0")

    def test_short_roles_are_filled_by_the_best_of_the_rest(self):
        rows = [_row(i, f"B{i}", 100 - i) for i in range(12)]
        xi = self.pick(rows, {i: "Batsman" for i in range(12)})
        self.assertEqual(len(xi), 11)

    def test_roles_are_inferred_without_a_card(self):
        rows = [_row(1, "Basher", 50, bat=50), _row(2, "Spinner", 40, bowl=40),
                _row(3, "Both", 60, bat=30, bowl=30)]
        xi = self.pick(rows, {})
        roles = {c.row.name: c.role for c in xi}
        self.assertEqual(roles, {"Basher": "bat", "Spinner": "bowl", "Both": "ar"})

    def test_bit_part_players_need_enough_matches(self):
        rows = [_row(1, "Regular", 50, bat=50, matches=10),
                _row(2, "Cameo", 90, bat=90, matches=1)]
        xi = self.pick(rows, {})
        self.assertEqual([c.row.name for c in xi], ["Regular"])

    def test_no_image_without_every_card(self):
        tour = SimpleNamespace(id=1, name="Cup")
        xi = [SimpleNamespace(row=_row(i, "x", 1), player=None, role="bat",
                              captain=False) for i in range(11)]
        self.assertIsNone(TOT.render_image(tour, xi))

    def test_the_image_is_titled(self):
        tour = SimpleNamespace(id=1, name="Cup")
        xi = [SimpleNamespace(row=_row(i, "x", 1), player=SimpleNamespace(id=i),
                              role="bat", captain=(i == 0)) for i in range(11)]
        with mock.patch("services.xi_image.build_xi_image", return_value=b"png") as b:
            self.assertEqual(TOT.render_image(tour, xi), b"png")
        self.assertEqual(b.call_args.kwargs["title"], "TEAM OF THE TOURNAMENT")
        self.assertEqual(b.call_args.kwargs["captain_roster_id"], 0)

    def test_text_fallback(self):
        tour = SimpleNamespace(id=1, name="Cup")
        self.assertIn("Not enough scorecards", TOT.render_text(tour, []))

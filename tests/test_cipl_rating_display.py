"""Challenge League balancing must not change the ratings players SEE.

The anti-blowout pass (``handlers.cipl_play._compress_team_gap``) pulls the two
XIs' effective ratings toward each other before the first ball. It used to do
that by overwriting ``rating``/``bat_rating``/``bowl_rating`` on the player
dicts — the same keys the dugout and the bowler picker read — so an 87 turned up
as 86 for one captain and 88 for the other, which reads as a bug even though the
simulation was doing exactly what it was asked to.

The fix keeps a never-touched ``card_*`` copy of every rating and shows that
instead, and skips the pass entirely when the two squads are already an even
match. These tests pin both halves.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _player(name, rating, bat=None, bowl=None):
    """A player dict shaped like services.cipl_match.cp_to_player_dict's."""
    bat = rating if bat is None else bat
    bowl = rating if bowl is None else bowl
    return {
        "roster_id": abs(hash(name)) % 10000,
        "name": name,
        "rating": rating, "bat_rating": bat, "bowl_rating": bowl,
        "card_rating": rating, "card_bat_rating": bat, "card_bowl_rating": bowl,
        "category": "Batsman", "bowl_style": "Fast",
        "bowl_hand": "Right", "bat_hand": "Right",
    }


def _xi(prefix, ratings):
    return [_player(f"{prefix}{i}", r) for i, r in enumerate(ratings)]


class DisplayRatingTest(unittest.TestCase):
    def setUp(self):
        from services import cipl_match
        self.cipl_match = cipl_match

    def test_the_card_rating_wins_over_an_adjusted_engine_rating(self):
        p = _player("Kohli", 87)
        p["rating"] = 86          # what a balancing pass would leave behind
        p["bowl_rating"] = 61
        self.assertEqual(self.cipl_match.display_rating(p), 87)
        self.assertEqual(self.cipl_match.display_rating(p, "bowl_rating"), 87)

    def test_a_state_without_card_ratings_falls_back(self):
        # Matches already in flight when this shipped have no card_* keys.
        legacy = {"name": "Old", "rating": 84, "bowl_rating": 70}
        self.assertEqual(self.cipl_match.display_rating(legacy), 84)
        self.assertEqual(self.cipl_match.display_rating(legacy, "bowl_rating"), 70)

    def test_missing_player_is_zero_not_a_crash(self):
        self.assertEqual(self.cipl_match.display_rating(None), 0)


class CompressTeamGapTest(unittest.TestCase):
    def setUp(self):
        from handlers import cipl_play
        self.cipl_play = cipl_play

    def test_card_ratings_survive_a_lopsided_tie(self):
        strong = _xi("S", [92] * 11)
        weak = _xi("W", [70] * 11)
        self.cipl_play._compress_team_gap(strong, weak, factor=0.4)

        # The engine numbers moved…
        self.assertLess(strong[0]["rating"], 92)
        self.assertGreater(weak[0]["rating"], 70)
        # …and every printed rating is untouched, on both sides.
        for p in strong:
            self.assertEqual(p["card_rating"], 92)
            self.assertEqual(p["card_bat_rating"], 92)
            self.assertEqual(p["card_bowl_rating"], 92)
        for p in weak:
            self.assertEqual(p["card_rating"], 70)

    def test_an_even_tie_is_left_completely_alone(self):
        # This is the reported bug: two near-identical squads, and every card
        # shifted by one point in opposite directions for no benefit.
        a = _xi("A", [87, 86, 85, 84, 83, 82, 81, 80, 79, 78, 77])
        b = _xi("B", [88, 86, 85, 84, 83, 82, 81, 80, 79, 78, 76])
        self.cipl_play._compress_team_gap(a, b, factor=0.4)
        for team, source in ((a, [87, 86, 85, 84, 83, 82, 81, 80, 79, 78, 77]),
                             (b, [88, 86, 85, 84, 83, 82, 81, 80, 79, 78, 76])):
            for p, expected in zip(team, source):
                self.assertEqual(p["rating"], expected)
                self.assertEqual(p["bat_rating"], expected)
                self.assertEqual(p["bowl_rating"], expected)

    def test_a_gap_wider_than_the_dead_zone_still_compresses(self):
        a = _xi("A", [90] * 11)
        b = _xi("B", [80] * 11)
        self.cipl_play._compress_team_gap(a, b, factor=0.4, deadzone=5.0)
        self.assertEqual(a[0]["rating"], 88)   # 90 - 0.4 * 5
        self.assertEqual(b[0]["rating"], 82)   # 80 + 0.4 * 5

    def test_running_it_twice_is_the_same_as_running_it_once(self):
        # Both innings share the stored XIs; a second pass must not re-nudge.
        a = _xi("A", [92] * 11)
        b = _xi("B", [70] * 11)
        self.cipl_play._compress_team_gap(a, b, factor=0.4)
        once = [p["rating"] for p in a], [p["rating"] for p in b]
        self.cipl_play._compress_team_gap(a, b, factor=0.4)
        twice = [p["rating"] for p in a], [p["rating"] for p in b]
        self.assertEqual(once, twice)

    def test_relative_order_within_a_squad_is_preserved(self):
        a = _xi("A", [95, 90, 85, 80, 75, 70, 65, 60, 55, 50, 45])
        b = _xi("B", [60] * 11)
        before = [p["rating"] for p in a]
        self.cipl_play._compress_team_gap(a, b, factor=0.4)
        after = [p["rating"] for p in a]
        self.assertEqual(sorted(after, reverse=True), after)
        self.assertTrue(all(x > y for x, y in zip(before, after)))

    def test_ratings_stay_inside_1_to_100(self):
        a = _xi("A", [99] * 11)
        b = _xi("B", [2] * 11)
        self.cipl_play._compress_team_gap(a, b, factor=1.0)
        for p in a + b:
            self.assertGreaterEqual(p["rating"], 1)
            self.assertLessEqual(p["rating"], 100)

    def test_factor_zero_and_empty_squads_are_no_ops(self):
        a = _xi("A", [90] * 11)
        b = _xi("B", [70] * 11)
        self.assertEqual(self.cipl_play._compress_team_gap(a, b, factor=0.0), 0.0)
        self.assertEqual(a[0]["rating"], 90)
        self.assertEqual(self.cipl_play._compress_team_gap([], b, factor=0.4), 0.0)


if __name__ == "__main__":
    unittest.main()

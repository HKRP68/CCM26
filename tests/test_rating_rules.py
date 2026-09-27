"""The rating rule helper: "at least N players rated X or lower".

Pure functions, shared by the Franchise Auction's squad rule and the Challenge
League tournament's XI rule, so they are pinned once here.
"""

import unittest

from services import rating_rules as RR


class NormalizeTests(unittest.TestCase):

    def test_bad_rows_are_dropped_and_duplicates_keep_the_larger_count(self):
        rules = RR.normalize([
            {"max_rating": 83, "min_players": 2},
            {"max_rating": "83", "min_players": "4"},
            {"max_rating": 0, "min_players": 3},      # cap out of range
            {"max_rating": 80, "min_players": 0},     # 0 is "no rule"
            {"max_rating": "x", "min_players": 1},
            "junk",
        ])
        self.assertEqual([{"max_rating": 83, "min_players": 4}], rules)

    def test_counts_are_clamped_to_the_players_there_are(self):
        rules = RR.normalize([{"max_rating": 83, "min_players": 15}],
                             cap_players=11)
        self.assertEqual(11, rules[0]["min_players"])

    def test_round_trip(self):
        rules = [{"max_rating": 80, "min_players": 2},
                 {"max_rating": 83, "min_players": 4}]
        self.assertEqual(RR.normalize(rules), RR.parse(RR.dump(rules)))
        self.assertIsNone(RR.dump([]))
        self.assertEqual([], RR.parse(None))
        self.assertEqual([], RR.parse("{not json"))


class CheckTests(unittest.TestCase):
    rules = [{"max_rating": 83, "min_players": 3}]

    def test_unknown_ratings_count_for_nothing(self):
        self.assertEqual(1, RR.count_at_or_below([83, 84, None], 83))

    def test_shortfall_only_when_the_slots_left_cannot_cover_it(self):
        # One player at or under 83, two owed.
        self.assertEqual([], RR.shortfalls(self.rules, [90, 80], slots_left=2))
        missed = RR.shortfalls(self.rules, [90, 80], slots_left=1)
        self.assertEqual([(self.rules[0], 2)], missed)

    def test_xi_error_names_the_rule_and_the_count(self):
        self.assertEqual("", RR.xi_error(self.rules, [80, 81, 83, 95]))
        error = RR.xi_error(self.rules, [80, 95, 96])
        self.assertIn("≤83", error)
        self.assertIn("you have 1", error)


class CommandTests(unittest.TestCase):

    def test_parse(self):
        self.assertEqual(("list", None, None), RR.parse_command([]))
        self.assertEqual(("set", 83, 4), RR.parse_command(["83", "4"]))
        self.assertEqual(("set", 83, 4), RR.parse_command(["≤83", "4"]))
        self.assertEqual(("off", 83, None), RR.parse_command(["off", "83"]))
        self.assertEqual(("off", 83, None), RR.parse_command(["83", "0"]))
        self.assertEqual(("clear", None, None), RR.parse_command(["clear"]))
        for bad in (["83"], ["abc", "2"], ["120", "2"], ["off"]):
            with self.assertRaises(ValueError):
                RR.parse_command(bad)

    def test_apply(self):
        rules = RR.apply_command([], "set", 83, 4)
        rules = RR.apply_command(rules, "set", 80, 2)
        rules = RR.apply_command(rules, "set", 83, 3)
        self.assertEqual([{"max_rating": 83, "min_players": 3},
                          {"max_rating": 80, "min_players": 2}], rules)
        self.assertEqual([{"max_rating": 80, "min_players": 2}],
                         RR.apply_command(rules, "off", 83, None))
        self.assertEqual([], RR.apply_command(rules, "clear", None, None))


if __name__ == "__main__":
    unittest.main()

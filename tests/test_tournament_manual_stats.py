"""Unit tests for the manual-result overs parser and the 50s/100s counters.

Both exercise pure helpers in ``tournament_service`` (no DB), covering the two
tournament additions: manually recording a fixture's scoreline (overs → balls,
which drives net run-rate) and the Most 50s / Most 100s leaderboards.
"""

import unittest

from services.tournament_service import _overs_to_balls, _milestone_rows


class OversToBallsTests(unittest.TestCase):
    def test_whole_overs(self):
        self.assertEqual(_overs_to_balls(20), 120)
        self.assertEqual(_overs_to_balls("20"), 120)

    def test_partial_over_is_balls_not_decimal(self):
        # 19.3 = 19 overs and 3 balls = 117 legal balls, not 19.3 * 6.
        self.assertEqual(_overs_to_balls("19.3"), 117)
        self.assertEqual(_overs_to_balls(0.5), 5)

    def test_blank_and_none_are_zero(self):
        self.assertEqual(_overs_to_balls(None), 0)
        self.assertEqual(_overs_to_balls(""), 0)
        self.assertEqual(_overs_to_balls("   "), 0)

    def test_balls_digit_clamped(self):
        # A typo like 19.9 can't create a 9-ball over.
        self.assertEqual(_overs_to_balls("19.9"), 19 * 6 + 5)

    def test_garbage_is_zero(self):
        self.assertEqual(_overs_to_balls("abc"), 0)


class MilestoneRowsTests(unittest.TestCase):
    def _line(self, rid, runs, name="P", team="T", batted=True):
        return {"roster_id": rid, "name": name, "team_name": team,
                "batted": batted, "bat_runs": runs}

    def test_fifty_range_and_hundred_range(self):
        lines = [
            self._line(1, 49),   # not a fifty
            self._line(1, 50),   # fifty
            self._line(1, 99),   # fifty
            self._line(1, 100),  # hundred (not also a fifty)
            self._line(1, 150),  # hundred
        ]
        agg = _milestone_rows(lines)
        (entry,) = agg.values()
        self.assertEqual(entry["fifties"], 2)
        self.assertEqual(entry["hundreds"], 2)

    def test_century_not_double_counted_as_fifty(self):
        agg = _milestone_rows([self._line(1, 120)])
        (entry,) = agg.values()
        self.assertEqual(entry["fifties"], 0)
        self.assertEqual(entry["hundreds"], 1)

    def test_separate_players_kept_apart(self):
        agg = _milestone_rows([self._line(1, 60), self._line(2, 60)])
        self.assertEqual(len(agg), 2)

    def test_non_batted_and_malformed_lines_ignored(self):
        lines = [self._line(1, 75, batted=False), "junk", None, self._line(1, 75)]
        agg = _milestone_rows(lines)
        (entry,) = agg.values()
        self.assertEqual(entry["fifties"], 1)

    def test_display_snapshot_kept(self):
        agg = _milestone_rows([self._line(1, 55, name="Kohli", team="RCB")])
        (entry,) = agg.values()
        self.assertEqual(entry["name"], "Kohli")
        self.assertEqual(entry["team_name"], "RCB")


if __name__ == "__main__":
    unittest.main()

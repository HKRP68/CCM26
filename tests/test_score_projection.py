"""Unit tests for services.score_projection.

Covers the first-innings projection (historical par-curve estimate, likely
range from the calibrated pitch band, the approximate fallback, and the edge
cases the brief calls out) and the second-innings score comparison (cumulative
reads from recorded over history, wicket snapshots, the last-N window, and the
ahead/behind/level verdict) — including The Hundred's 5-ball accounting and
shortened / all-out innings.

The service is pure, so these tests build plain state dicts rather than running
the simulation; a companion rendering test exercises it on a real match.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import score_projection as sp  # noqa: E402


def _inn1(runs, wkts, over, *, overs=20, ball=0, pitch="Even",
          ball_format="T20", wicket_limit=10):
    """A minimal first-innings state at the start of ``over`` (over 1-indexed)."""
    return {
        "innings": 1, "total_runs": runs, "total_wickets": wkts,
        "overs": overs, "ball_format": ball_format,
        "current_over": over, "current_ball": ball,
        "pitch_type": pitch, "wicket_limit": wicket_limit,
    }


class FirstInningsProjectionTests(unittest.TestCase):

    def test_not_first_innings_returns_none(self):
        s = _inn1(108, 3, 13)
        s["innings"] = 2
        self.assertIsNone(sp.project_first_innings(s))

    def test_none_state(self):
        self.assertIsNone(sp.project_first_innings(None))

    def test_no_ball_bowled_returns_none(self):
        self.assertIsNone(sp.project_first_innings(_inn1(0, 0, 1)))

    def test_expected_in_sensible_band_for_par_start(self):
        # 108/3 after 12 overs on an Even deck: a par-paced innings heading for
        # roughly the Even par total, never the naive run-rate only.
        proj = sp.project_first_innings(_inn1(108, 3, 13))
        self.assertFalse(proj["approximate"])
        self.assertFalse(proj["complete"])
        self.assertTrue(170 <= proj["expected"] <= 205, proj)

    def test_projection_never_below_current_score(self):
        # A huge score late: the projection cannot dip under what is on the board.
        proj = sp.project_first_innings(_inn1(240, 2, 19))
        self.assertGreaterEqual(proj["expected"], 240)
        if proj["low"] is not None:
            self.assertGreaterEqual(proj["low"], 240)

    def test_likely_range_brackets_expected(self):
        proj = sp.project_first_innings(_inn1(108, 3, 13))
        self.assertIsNotNone(proj["low"])
        self.assertIsNotNone(proj["high"])
        self.assertLessEqual(proj["low"], proj["expected"])
        self.assertGreaterEqual(proj["high"], proj["expected"])
        self.assertLess(proj["low"], proj["high"])

    def test_more_wickets_down_lowers_projection(self):
        few = sp.project_first_innings(_inn1(108, 2, 13))
        many = sp.project_first_innings(_inn1(108, 8, 13))
        self.assertLess(many["expected"], few["expected"])

    def test_higher_scoring_pitch_lifts_projection(self):
        dusty = sp.project_first_innings(_inn1(108, 3, 13, pitch="Dusty"))
        flat = sp.project_first_innings(_inn1(108, 3, 13, pitch="Flat"))
        self.assertLess(dusty["expected"], flat["expected"])

    def test_all_out_is_complete_and_frozen(self):
        proj = sp.project_first_innings(_inn1(95, 10, 15, ball=2))
        self.assertTrue(proj["complete"])
        self.assertEqual(proj["expected"], 95)
        self.assertEqual(proj["low"], 95)
        self.assertEqual(proj["high"], 95)

    def test_full_distance_is_complete(self):
        # Every ball bowled (over 21 start == 20 overs done) → nothing to project.
        proj = sp.project_first_innings(_inn1(180, 6, 21))
        self.assertTrue(proj["complete"])
        self.assertEqual(proj["expected"], 180)

    def test_unknown_pitch_has_expected_but_no_invented_range(self):
        # Par curve still applies (neutral factor); no calibrated band → no range.
        proj = sp.project_first_innings(_inn1(108, 3, 13, pitch="Lunar"))
        self.assertIsNotNone(proj["expected"])
        self.assertIsNone(proj["low"])
        self.assertIsNone(proj["high"])

    def test_approximate_fallback_when_curve_missing(self):
        # Force the calibrated curve to be unavailable.
        import unittest.mock as mock
        with mock.patch.object(sp, "_format_config", return_value=None):
            proj = sp.project_first_innings(_inn1(108, 3, 13))
        self.assertTrue(proj["approximate"])
        self.assertIsNone(proj["low"])  # no invented confidence interval
        self.assertIsNone(proj["high"])
        # Rate extrapolation: 108 over 72 balls → 9 rpo → ~180 over 120.
        self.assertTrue(170 <= proj["expected"] <= 190, proj)

    def test_short_formats_supported(self):
        for overs, runs, over in ((5, 24, 3), (10, 55, 6), (20, 108, 13)):
            proj = sp.project_first_innings(_inn1(runs, 2, over, overs=overs))
            self.assertIsNotNone(proj)
            self.assertGreaterEqual(proj["expected"], runs)
            self.assertFalse(proj["complete"])

    def test_the_hundred_five_ball_accounting(self):
        # 60 of 100 balls → current_over 13 (12 sets) + 0 balls, 5 balls/set.
        s = _inn1(72, 2, 13, overs=20, ball_format="The100")
        proj = sp.project_first_innings(s)
        self.assertIsNotNone(proj)
        self.assertFalse(proj["complete"])
        self.assertGreaterEqual(proj["expected"], 72)
        # A Hundred projects lower than a 120-ball T20 at the same stage.
        t20 = sp.project_first_innings(_inn1(72, 2, 9, overs=20))
        self.assertLess(proj["expected"], t20["expected"])


# ── second-innings comparison ────────────────────────────────────────────────

def _inn2(inn1_over_runs, inn2_over_runs, *, over, overs=20, ball=0,
          ball_format="T20", inn1_over_wkts=None, over_wkts=None,
          inn1_balls=None, target=None, defending="Vindhya",
          chasing="Tamil Dragon"):
    bpu = 5 if ball_format == "The100" else 6
    state = {
        "innings": 2, "overs": overs, "ball_format": ball_format,
        "current_over": over, "current_ball": ball,
        "inn1_over_runs": list(inn1_over_runs),
        "over_runs": list(inn2_over_runs),
        "inn1_team": defending, "bat_team_name": chasing,
        "bowl_team_name": defending,
        "inn1_balls": (inn1_balls if inn1_balls is not None
                       else len(inn1_over_runs) * bpu),
        "target": target,
    }
    if inn1_over_wkts is not None:
        state["inn1_over_wkts"] = list(inn1_over_wkts)
    if over_wkts is not None:
        state["over_wkts"] = list(over_wkts)
    return state


class SecondInningsComparisonTests(unittest.TestCase):

    def test_not_second_innings_returns_none(self):
        s = _inn2([8, 10], [9, 8], over=3)
        s["innings"] = 1
        self.assertIsNone(sp.compare_innings(s))

    def test_no_first_innings_history_returns_none(self):
        s = _inn2([], [9, 8], over=3)
        self.assertIsNone(sp.compare_innings(s))

    def test_compares_at_same_completed_overs(self):
        inn1 = [10, 8, 12, 9, 11, 14, 7, 9, 10, 8, 12, 11, 9, 13]
        inn2 = [9, 11, 8, 10, 7, 13, 12, 8, 9, 11, 10, 12]  # 12 overs batted
        comp = sp.compare_innings(_inn2(inn1, inn2, over=13))
        self.assertEqual(comp["stage"], 12)
        self.assertEqual(comp["inn1"]["runs"], sum(inn1[:12]))
        self.assertEqual(comp["inn2"]["runs"], sum(inn2[:12]))
        self.assertEqual(comp["diff"], sum(inn2[:12]) - sum(inn1[:12]))

    def test_stage_is_mutual_minimum(self):
        # First innings only lasted 8 overs; the chase is deeper.
        inn1 = [10, 8, 12, 9, 11, 14, 7, 9]
        inn2 = [9, 11, 8, 10, 7, 13, 12, 8, 9, 11]
        comp = sp.compare_innings(_inn2(inn1, inn2, over=11, inn1_balls=8 * 6))
        self.assertEqual(comp["stage"], 8)

    def test_partial_over_excluded(self):
        # 12 completed overs + 4 balls into the 13th: only 12 compared.
        inn1 = list(range(1, 15))
        inn2 = [5] * 13  # 13th entry is the in-progress over's partial tally
        comp = sp.compare_innings(_inn2(inn1, inn2, over=13, ball=4))
        self.assertEqual(comp["stage"], 12)
        self.assertEqual(comp["inn2"]["runs"], 60)

    def test_wicket_snapshots_from_tracking(self):
        inn1 = [10] * 12
        inn2 = [9] * 12
        comp = sp.compare_innings(_inn2(
            inn1, inn2, over=13,
            inn1_over_wkts=[0, 1, 0, 0, 1, 0, 0, 1, 0, 0, 0, 1],
            over_wkts=[1, 0, 0, 1, 0, 1, 0, 0, 1, 0, 1, 0]))
        self.assertEqual(comp["inn1"]["wkts"], 4)
        self.assertEqual(comp["inn2"]["wkts"], 5)

    def test_wicket_fallback_from_fow(self):
        # No per-over wicket lists: derive from the fall-of-wickets list.
        s = _inn2([10] * 12, [9] * 12, over=13)
        s["inn1_fow"] = [[30, 1, "A", "3.2"], [80, 2, "B", "7.0"],
                         [150, 3, "C", "14.1"]]  # 3rd falls after over 12
        s["fow"] = [[20, 1, "X", "2.4"]]
        comp = sp.compare_innings(s)
        self.assertEqual(comp["inn1"]["wkts"], 2)   # two fell within 12 overs
        self.assertEqual(comp["inn2"]["wkts"], 1)

    def test_wickets_none_when_unavailable(self):
        comp = sp.compare_innings(_inn2([10] * 12, [9] * 12, over=13))
        self.assertIsNone(comp["inn1"]["wkts"])
        self.assertIsNone(comp["inn2"]["wkts"])

    def test_ahead_behind_level(self):
        behind = sp.compare_innings(_inn2([10, 10, 10], [9, 9, 9], over=4))
        self.assertEqual(behind["diff"], -3)
        ahead = sp.compare_innings(_inn2([9, 9, 9], [10, 10, 10], over=4))
        self.assertEqual(ahead["diff"], 3)
        level = sp.compare_innings(_inn2([10, 10, 10], [10, 10, 10], over=4))
        self.assertEqual(level["diff"], 0)

    def test_last_window_only_when_enough_overs(self):
        two = sp.compare_innings(_inn2([10, 12], [9, 11], over=3))
        self.assertIsNone(two["last"])
        three = sp.compare_innings(_inn2([10, 12, 8], [9, 11, 14], over=4))
        self.assertIsNotNone(three["last"])
        self.assertEqual(three["last"]["n"], 3)
        self.assertEqual(three["last"]["inn1"], 30)
        self.assertEqual(three["last"]["inn2"], 34)

    def test_does_not_invent_beyond_first_innings_duration(self):
        # Chase far longer than inn1 lasted: never read past inn1's real overs.
        inn1 = [6, 6, 6]            # all out in 3 overs
        inn2 = [8] * 10
        comp = sp.compare_innings(_inn2(inn1, inn2, over=11, inn1_balls=3 * 6))
        self.assertEqual(comp["stage"], 3)
        self.assertEqual(comp["inn1"]["runs"], 18)

    def test_the_hundred_comparison(self):
        # 5-ball sets: 12 sets == 60 balls; current_over 13 start.
        inn1 = [6, 7, 5, 8, 6, 9, 4, 7, 6, 8, 5, 7, 9, 6]
        inn2 = [5, 8, 6, 7, 5, 9, 6, 7, 8, 6, 7, 9]
        comp = sp.compare_innings(_inn2(inn1, inn2, over=13, ball_format="The100"))
        self.assertEqual(comp["stage"], 12)
        self.assertEqual(comp["unit_word"], "set")


if __name__ == "__main__":
    unittest.main()

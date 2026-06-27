"""Tests for rare Rain & DLS in CIPL (engine.dls + cipl_match._maybe_rain_interrupt)."""

import os
import unittest

from engine import dls
from services import cipl_match


class DlsModelTests(unittest.TestCase):
    def test_no_overs_lost_keeps_target(self):
        self.assertEqual(dls.revised_target(160, 20, 8, 8, 3), 161)

    def test_losing_overs_lowers_target(self):
        full = dls.revised_target(160, 20, 8, 8, 3)
        cut = dls.revised_target(160, 20, 8, 6, 3)
        self.assertLess(cut, full)

    def test_more_wickets_in_hand_loses_more_resource(self):
        # With more wickets standing (fewer lost), the lost overs were worth
        # more, so the target is cut harder than when many wickets are down.
        few_down = dls.revised_target(160, 20, 8, 6, 3)
        many_down = dls.revised_target(160, 20, 8, 6, 7)
        self.assertLessEqual(few_down, many_down)

    def test_resource_monotonic(self):
        self.assertEqual(round(dls.resource_pct(20, 0, 20)), 100)
        self.assertGreater(dls.resource_pct(20, 0, 20), dls.resource_pct(10, 0, 20))
        self.assertGreater(dls.resource_pct(10, 0, 20), dls.resource_pct(10, 5, 20))


class RainInterruptTests(unittest.TestCase):
    def _state(self):
        return {
            "innings": 2, "overs": 20, "target": 161,
            "total_runs": 80, "total_wickets": 3,
            "current_over": 9, "current_ball": 0,
            "bat_team_name": "Chasers", "commentary_log": [],
            "ball_format": "T20",
        }

    def setUp(self):
        self._prev = os.environ.get("CIPL_RAIN_PROB")

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("CIPL_RAIN_PROB", None)
        else:
            os.environ["CIPL_RAIN_PROB"] = self._prev

    def test_forced_rain_shortens_innings_and_revises_target(self):
        os.environ["CIPL_RAIN_PROB"] = "1"
        s = self._state()
        new_target = cipl_match._maybe_rain_interrupt(s, over_idx=8, overs_total=20,
                                                      innings=2, target=161)
        self.assertLess(s["overs"], 20)
        self.assertEqual(s["target"], new_target)
        self.assertLessEqual(new_target, 161)
        self.assertTrue(s["rain_done"])
        self.assertEqual(len(s["rain_interruptions"]), 1)
        self.assertTrue(any(c.get("type") == "rain" for c in s["commentary_log"]))

    def test_fires_at_most_once(self):
        os.environ["CIPL_RAIN_PROB"] = "1"
        s = self._state()
        cipl_match._maybe_rain_interrupt(s, 8, 20, 2, s["target"])
        overs_after_first = s["overs"]
        # A second eligible over must not interrupt again.
        cipl_match._maybe_rain_interrupt(s, 9, 20, 2, s["target"])
        self.assertEqual(s["overs"], overs_after_first)
        self.assertEqual(len(s["rain_interruptions"]), 1)

    def test_never_in_first_innings(self):
        os.environ["CIPL_RAIN_PROB"] = "1"
        s = self._state()
        s["innings"] = 1
        s["target"] = None
        out = cipl_match._maybe_rain_interrupt(s, 8, 20, 1, None)
        self.assertIsNone(out)
        self.assertEqual(s["overs"], 20)
        self.assertNotIn("rain_done", s)

    def test_not_in_final_overs(self):
        os.environ["CIPL_RAIN_PROB"] = "1"
        s = self._state()
        # over_idx 18 of 20 -> only 2 overs left, below the threshold.
        cipl_match._maybe_rain_interrupt(s, 18, 20, 2, s["target"])
        self.assertEqual(s["overs"], 20)
        self.assertNotIn("rain_done", s)

    def test_disabled_when_probability_zero(self):
        os.environ["CIPL_RAIN_PROB"] = "0"
        s = self._state()
        cipl_match._maybe_rain_interrupt(s, 8, 20, 2, s["target"])
        self.assertEqual(s["overs"], 20)
        self.assertNotIn("rain_done", s)


class ComputeResultWithDlsTests(unittest.TestCase):
    """compute_result must judge ties/run-margins against the (possibly revised)
    target, not the raw first-innings score."""

    def _state(self, *, inn1, target, inn2, wickets=4):
        return {
            "inn1_runs": inn1, "target": target, "total_runs": inn2,
            "total_wickets": wickets, "wicket_limit": 10,
            "bat_team_name": "Chasers", "bowl_team_name": "Defenders",
            "inn1_bat_team": "Defenders",
        }

    def test_normal_match_runs_margin_unchanged(self):
        # No revision: target == inn1 + 1, so par == inn1.
        r = cipl_match.compute_result(self._state(inn1=180, target=181, inn2=170))
        self.assertEqual(r["margin_type"], "runs")
        self.assertEqual(r["margin"], 10)
        self.assertEqual(r["winner"], "Defenders")

    def test_dls_revised_loss_margin_uses_par(self):
        # 180 revised to a target of 150 (par 149); chase ends 140 -> lose by 9.
        r = cipl_match.compute_result(self._state(inn1=180, target=150, inn2=140))
        self.assertEqual(r["margin_type"], "runs")
        self.assertEqual(r["margin"], 9)
        self.assertEqual(r["winner"], "Defenders")

    def test_dls_revised_tie_at_par(self):
        # Chase ends exactly on par (149) under a revised target of 150 -> tie.
        r = cipl_match.compute_result(self._state(inn1=180, target=150, inn2=149))
        self.assertTrue(r["tie"])
        self.assertIsNone(r["winner"])

    def test_dls_revised_win_by_wickets(self):
        r = cipl_match.compute_result(self._state(inn1=180, target=150, inn2=150, wickets=3))
        self.assertEqual(r["margin_type"], "wickets")
        self.assertEqual(r["winner"], "Chasers")


if __name__ == "__main__":
    unittest.main()

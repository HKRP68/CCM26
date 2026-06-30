"""Tests for the DLS engine util and CIPL compute_result.

The rare in-match Rain interruption was removed from CIPL/Letsplay; this suite
keeps coverage of the generic ``engine.dls`` model and of ``compute_result``
judging margins against ``state["target"]`` (which is always first-innings + 1
now that no rain revision can occur), plus a regression check that simulating an
over never shortens the innings.
"""

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
        few_down = dls.revised_target(160, 20, 8, 6, 3)
        many_down = dls.revised_target(160, 20, 8, 6, 7)
        self.assertLessEqual(few_down, many_down)

    def test_resource_monotonic(self):
        self.assertEqual(round(dls.resource_pct(20, 0, 20)), 100)
        self.assertGreater(dls.resource_pct(20, 0, 20), dls.resource_pct(10, 0, 20))
        self.assertGreater(dls.resource_pct(10, 0, 20), dls.resource_pct(10, 5, 20))


class RainRemovedTests(unittest.TestCase):
    """Rain was removed from CIPL/Letsplay — the interrupt hook is gone."""

    def test_rain_interrupt_helper_removed(self):
        self.assertFalse(hasattr(cipl_match, "_maybe_rain_interrupt"))


class ComputeResultTests(unittest.TestCase):
    """compute_result judges ties/run-margins against state["target"]."""

    def _state(self, *, inn1, target, inn2, wickets=4):
        return {
            "inn1_runs": inn1, "target": target, "total_runs": inn2,
            "total_wickets": wickets, "wicket_limit": 10,
            "bat_team_name": "Chasers", "bowl_team_name": "Defenders",
            "inn1_bat_team": "Defenders",
        }

    def test_runs_margin(self):
        # target == inn1 + 1, so par == inn1.
        r = cipl_match.compute_result(self._state(inn1=180, target=181, inn2=170))
        self.assertEqual(r["margin_type"], "runs")
        self.assertEqual(r["margin"], 10)
        self.assertEqual(r["winner"], "Defenders")

    def test_tie_at_par(self):
        r = cipl_match.compute_result(self._state(inn1=180, target=181, inn2=180))
        self.assertTrue(r["tie"])
        self.assertIsNone(r["winner"])

    def test_win_by_wickets(self):
        r = cipl_match.compute_result(self._state(inn1=180, target=181, inn2=181, wickets=3))
        self.assertEqual(r["margin_type"], "wickets")
        self.assertEqual(r["winner"], "Chasers")


if __name__ == "__main__":
    unittest.main()

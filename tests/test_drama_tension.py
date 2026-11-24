"""Tension Index (Codex Prompt 05, §7) — presentation-only, 0-100, bounded."""

import unittest

from engine.drama import tension as T
from engine.drama.context import DramaContext


class TensionTests(unittest.TestCase):

    def test_values_stay_within_0_100(self):
        for p in (0.0, 0.1, 0.5, 0.9, 1.0):
            for br in (0, 1, 30, 60, 119, 120):
                for ko in (False, True):
                    for sw in (0.0, 0.5, 1.0):
                        t = T.chase_tension(p, br, 120, is_knockout=ko, recent_swing=sw)
                        self.assertGreaterEqual(t, 0)
                        self.assertLessEqual(t, 100)

    def test_peaks_when_close_late_and_swinging(self):
        hot = T.chase_tension(0.5, 1, 120, is_knockout=True, recent_swing=1.0)
        cold = T.chase_tension(0.97, 115, 120, is_knockout=False, recent_swing=0.0)
        self.assertGreater(hot, cold)
        self.assertGreaterEqual(hot, 90)        # a true last-ball knockout
        self.assertLessEqual(cold, 24)          # a one-sided early chase

    def test_decided_game_has_no_tension(self):
        self.assertEqual(T.chase_tension(1.0, 20, 120), 0)
        self.assertEqual(T.chase_tension(0.0, 20, 120), 0)

    def test_bands(self):
        self.assertEqual(T.band(10), "low")
        self.assertEqual(T.band(30), "building")
        self.assertEqual(T.band(60), "high")
        self.assertEqual(T.band(80), "thriller")
        self.assertEqual(T.band(95), "extreme")

    def test_first_innings_intensity_is_bounded_and_not_a_win_prob(self):
        calm = T.first_innings_intensity(8, 6, 0, 9)
        collapse = T.first_innings_intensity(1, 12, 3, 4)
        self.assertTrue(0 <= calm <= 100 and 0 <= collapse <= 100)
        self.assertGreater(collapse, calm)      # a collapse reads as intense

    def test_context_win_prob_is_bounded(self):
        ctx = DramaContext(innings=2, runs=150, wickets=4, target=180,
                           innings_balls=120, balls_bowled=96)
        p = ctx.batting_win_prob()
        self.assertTrue(0.0 <= p <= 1.0)
        # Already chased / no balls left are terminal.
        done = DramaContext(innings=2, runs=181, wickets=4, target=180,
                            innings_balls=120, balls_bowled=96)
        self.assertTrue(done.decided)


if __name__ == "__main__":
    unittest.main()

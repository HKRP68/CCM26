"""The shared drama budget (Codex Prompt 05, §11) — clamp, caps, safety."""

import math
import unittest

from engine.drama import modifiers as mod
from engine.drama.config import preset_config

_W = {"Dot": 0.35, "Single": 0.33, "Double": 0.07, "Three": 0.005,
      "Four": 0.11, "Six": 0.05, "Wicket": 0.05, "Extras": 0.035}


class BudgetTests(unittest.TestCase):

    def setUp(self):
        self.cfg = preset_config("BALANCED")

    def test_combine_sums_deltas(self):
        c = mod.combine_log_deltas({"Four": 0.1}, {"Four": 0.2, "Dot": -0.1})
        self.assertAlmostEqual(c["Four"], 0.3)
        self.assertAlmostEqual(c["Dot"], -0.1)

    def test_output_is_normalised(self):
        p, _ = mod.apply(_W, {"Six": 0.3, "Dot": -0.2}, self.cfg)
        self.assertAlmostEqual(sum(p.values()), 1.0, places=9)

    def test_safety_limits_bind(self):
        # A deliberately huge push must be scaled back under both caps.
        big = {"Four": 0.5, "Six": 0.6, "Wicket": 0.5, "Dot": -0.5}
        p, info = mod.apply(_W, big, self.cfg)
        self.assertLessEqual(abs(info["wicket_delta"]), 0.012 + 1e-9)
        self.assertLessEqual(abs(info["runs_delta"]), 0.08 + 1e-9)
        self.assertLess(info["scale"], 1.0)             # it had to be reduced

    def test_zero_outcome_never_revived(self):
        w = dict(_W, Six=0.0)
        p, _ = mod.apply(w, {"Six": 1.0}, self.cfg)
        self.assertEqual(p["Six"], 0.0)

    def test_free_hit_adds_no_wicket(self):
        p, info = mod.apply(_W, {"Wicket": 0.5}, self.cfg, free_hit=True)
        self.assertLessEqual(info["wicket_delta"], 1e-9)

    def test_per_outcome_log_clamp(self):
        # Even a single-outcome push is clamped to the +/-ln(1.15) window before
        # the safety search, so no outcome can move more than that per ball.
        p, info = mod.apply(_W, {"Single": 5.0}, self.cfg)
        # Single's realised multiplier stays within exp(+/-ln(1.15)) * scale.
        ratio = p["Single"] / mod._normalized(_W)["Single"]
        self.assertLess(ratio, 1.15 + 0.02)


if __name__ == "__main__":
    unittest.main()

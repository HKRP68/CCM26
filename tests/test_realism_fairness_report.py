"""Legacy-vs-realism fairness invariants (Prompt 04, requirement 7).

Fast, wide-band smoke checks on the realism engine's fairness, driven by the
same harness as ``tools/fairness_report.py``. The bands are deliberately loose
so they never flake on a small sample — they are there to catch a GROSS
regression (e.g. a reintroduced "home side always wins" bias would push
symmetry toward 100, and a broken rating model would drop the stronger-side win
rate toward 50), not to pin an exact rate. The precise figures come from
``tools/fairness_report.py --n 300``.
"""

import logging
import os
import unittest

logging.disable(logging.CRITICAL)

from tools.fairness_report import _run_scenario


class RealismFairnessTests(unittest.TestCase):

    N = 24
    SEED = 2024

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("SIM_REALISM_ENABLED", None)
        os.environ.pop("SIM_REALISM_SHADOW_MODE", None)

    def test_equal_sides_are_roughly_symmetric(self):
        r = _run_scenario(True, "Even", self.N, 20, self.SEED)
        self.assertGreaterEqual(r["decided"], self.N // 2)
        self.assertTrue(25.0 <= r["symmetry_win_pct"] <= 75.0,
                        "equal sides should be near-symmetric, got %.1f"
                        % r["symmetry_win_pct"])

    def test_toss_is_not_decisive(self):
        r = _run_scenario(True, "Even", self.N, 20, self.SEED)
        self.assertTrue(25.0 <= r["toss_winner_win_pct"] <= 75.0,
                        "toss winner should not dominate, got %.1f"
                        % r["toss_winner_win_pct"])

    def test_stronger_side_wins_clearly(self):
        r = _run_scenario(True, "Even", self.N, 20, self.SEED + 1,
                          strong_bias=14)
        self.assertGreater(r["stronger_win_pct"], 75.0,
                           "a much stronger XI should win well above half, got %.1f"
                           % r["stronger_win_pct"])


if __name__ == "__main__":
    unittest.main()

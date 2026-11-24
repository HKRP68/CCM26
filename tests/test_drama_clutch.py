"""Clutch Focus (Codex Prompt 05, §9) — bounded, symmetric, frequency-limited."""

import math
import unittest

from engine.drama import clutch
from engine.drama.config import preset_config
from engine.drama.context import DramaContext
from engine.drama.engine import DramaEngine
from engine.sim.rng import SimRng


class ClutchUnitTests(unittest.TestCase):

    def test_eligibility(self):
        self.assertTrue(clutch.eligible(18))            # final 3 overs
        self.assertTrue(clutch.eligible(60, high_pressure=True))
        self.assertFalse(clutch.eligible(60))
        self.assertFalse(clutch.eligible(6, decided=True))

    def test_activation_probability_formula_and_cap(self):
        # closeness peaks at p=0.5: 0.03 + 0.06*1 = 0.09, under the 0.12 cap.
        self.assertAlmostEqual(clutch.activation_probability(0.5), 0.09, places=6)
        # one-sided: closeness ~0 => ~0.03.
        self.assertAlmostEqual(clutch.activation_probability(1.0), 0.03, places=6)
        # never exceeds the cap.
        self.assertLessEqual(clutch.activation_probability(0.5, cap=0.05), 0.05)
        # first innings is lower-pressure.
        self.assertLess(clutch.activation_probability(0.5, first_innings=True),
                        clutch.activation_probability(0.5))

    def test_type_choice_is_symmetric(self):
        rng = SimRng(1)
        n = 4000
        bat = sum(clutch.choose_type(rng) == clutch.BATTING for _ in range(n))
        self.assertTrue(0.45 < bat / n < 0.55)          # ~50/50, no bias

    def test_modifiers_are_a_tradeoff(self):
        bat = clutch.log_deltas(clutch.BATTING)
        self.assertGreater(bat["Six"], 0.0)             # more reward
        self.assertGreater(bat["Wicket"], 0.0)          # and more risk
        bowl = clutch.log_deltas(clutch.BOWLING)
        self.assertGreater(bowl["Wicket"], 0.0)
        self.assertLess(bowl["Six"], 0.0)


class ClutchFrequencyTests(unittest.TestCase):
    """Driven through the engine: at most one activation per side per innings,
    lasting at most the configured number of legal balls."""

    def _run_innings(self, seed):
        cfg = preset_config("BALANCED")
        cfg["enabled"] = True
        eng = DramaEngine(cfg, rng=SimRng(seed), match_id="c")
        eng.new_innings(2)
        activations = 0
        max_active_run = 0
        active_run = 0
        base = {"Dot": 0.35, "Single": 0.33, "Double": 0.07, "Three": 0.005,
                "Four": 0.11, "Six": 0.05, "Wicket": 0.05, "Extras": 0.035}
        for over in range(20):
            ctx = DramaContext(innings=2, runs=120, wickets=3, target=180,
                               innings_balls=120, balls_bowled=over * 6)
            was = eng.state["clutch"]["active"]
            eng.on_over_start(ctx)
            if eng.state["clutch"]["active"] and not was:
                activations += 1
            for _ in range(6):
                eng.before_delivery(ctx, base)
                if eng.state["clutch"]["active"]:
                    active_run += 1
                    max_active_run = max(max_active_run, active_run)
                else:
                    active_run = 0
                eng.after_delivery(ctx, {"key": "Single", "runs": 1,
                                         "is_wicket": False, "legal": True,
                                         "legal_ball_index": 0}, base)
        return (eng.state["clutch"]["used_batting"]
                + eng.state["clutch"]["used_bowling"]), max_active_run

    def test_at_most_one_per_side_and_bounded_duration(self):
        for seed in range(12):
            total_used, max_run = self._run_innings(seed)
            self.assertLessEqual(total_used, 2)         # <=1 batting + <=1 bowling
            self.assertLessEqual(max_run, 6)            # duration cap


if __name__ == "__main__":
    unittest.main()

"""Drama fairness (Codex Prompt 05, §6, §19, §23).

Balanced drama never predetermines a competitive result, treats both sides
symmetrically, and its tension value never feeds back into an outcome.
"""

import unittest

from engine.drama import clutch, events
from engine.drama.config import preset_config
from engine.drama.context import DramaContext
from engine.drama.engine import DramaEngine
from engine.sim.rng import SimRng

_BASE = {"Dot": 0.35, "Single": 0.33, "Double": 0.07, "Three": 0.005,
         "Four": 0.11, "Six": 0.05, "Wicket": 0.05, "Extras": 0.035}


class FairnessTests(unittest.TestCase):

    def _engine(self, seed=1):
        cfg = preset_config("BALANCED")
        cfg["enabled"] = True
        e = DramaEngine(cfg, rng=SimRng(seed), match_id="m")
        e.new_innings(2)
        return e

    def test_tension_does_not_feed_back_into_outcome(self):
        # before_delivery must produce identical weights regardless of the
        # stored tension value — tension is presentation-only (§7).
        e = self._engine()
        ctx = DramaContext(innings=2, runs=120, wickets=3, target=180,
                           innings_balls=120, balls_bowled=60)
        e.state["last_tension"] = 0
        w_low = e.before_delivery(ctx, _BASE)
        e.state["last_tension"] = 100
        w_high = e.before_delivery(ctx, _BASE)
        self.assertEqual([round(w_low[k], 12) for k in sorted(w_low)],
                         [round(w_high[k], 12) for k in sorted(w_high)])

    def test_clutch_type_has_no_side_bias(self):
        rng = SimRng(7)
        n = 5000
        bat = sum(clutch.choose_type(rng) == clutch.BATTING for _ in range(n))
        self.assertTrue(0.46 < bat / n < 0.54)

    def test_overlay_is_bounded_every_ball(self):
        # Over a whole innings the realised per-ball wicket / run change never
        # exceeds the configured caps (never an unbounded positive feedback).
        e = self._engine(3)
        for over in range(20):
            ctx = DramaContext(innings=2, runs=120, wickets=3, target=185,
                               innings_balls=120, balls_bowled=over * 6)
            e.on_over_start(ctx)
            for _ in range(6):
                e.before_delivery(ctx, _BASE)
                diag = e.get_diagnostics()
                if diag:
                    self.assertLessEqual(abs(diag.get("wicket_delta", 0.0)), 0.0121)
                    self.assertLessEqual(abs(diag.get("runs_delta", 0.0)), 0.0801)
                e.after_delivery(ctx, {"key": "Single", "runs": 1,
                                       "is_wicket": False, "legal": True,
                                       "legal_ball_index": 0}, _BASE)

    def test_inactive_preset_is_a_passthrough(self):
        cfg = preset_config("REALISTIC")             # overlay disabled
        e = DramaEngine(cfg, rng=SimRng(1))
        ctx = DramaContext(innings=2, runs=120, wickets=3, target=180,
                           innings_balls=120, balls_bowled=60)
        self.assertIs(e.before_delivery(ctx, _BASE), _BASE)


if __name__ == "__main__":
    unittest.main()

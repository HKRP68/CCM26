"""Tactical-AI probability-model parity (Prompt 04, fix 6).

The bot captain must reason over the SAME distribution the live engine samples,
not a separate heuristic fitted to the old engine. In realism mode:

  * ``bot_tactics.ball_probabilities`` returns the realism engine's own
    baseline-plus-conditions distribution for the live context; and
  * ``bot_tactics.approach_probabilities`` shapes it with
    ``engine.realism.approaches.multipliers`` — the same priced trade-off the
    engine applies.

So the solver ranks the 25 match-ups over exactly the numbers the over will be
played on. With realism off the legacy estimator runs unchanged.
"""

import logging
import os
import unittest

logging.disable(logging.CRITICAL)

from engine.realism.config import clear_cache
from services import cipl_match, bot_tactics
from tests.test_sim_match import _make_xi


class _Realism:
    def __init__(self, on=True):
        self.on = on

    def __enter__(self):
        self._prev = os.environ.get("SIM_REALISM_ENABLED")
        os.environ["SIM_REALISM_ENABLED"] = "1" if self.on else "0"
        clear_cache()
        return self

    def __exit__(self, *exc):
        if self._prev is None:
            os.environ.pop("SIM_REALISM_ENABLED", None)
        else:
            os.environ["SIM_REALISM_ENABLED"] = self._prev
        clear_cache()
        return False


def _live_state():
    bat = [dict(p, roster_id=f"b{i}") for i, p in enumerate(_make_xi("B", 80))]
    bowl = [dict(p, roster_id=f"w{i}", will_bowl=True)
            for i, p in enumerate(_make_xi("W", 78))]
    st = cipl_match.build_cipl_state(
        "m", 20, 1, 2, "u1", "u2", bat, bowl, "Bats", "Bowls", 10,
        pitch_type="Even")
    st["current_bowler"] = bowl[10]
    st["total_runs"], st["total_wickets"] = 45, 2
    st["current_over"], st["current_ball"] = 7, 0
    return st, bowl[10]


class BallModelParityTests(unittest.TestCase):

    def test_bot_uses_engine_distribution_in_realism_mode(self):
        with _Realism(on=True):
            st, bowler = _live_state()
            rctx = bot_tactics._realism_context(st, bowler)
            self.assertIsNotNone(rctx, "realism context must engage when live")

            from engine.realism.outcome_distribution import build as rbuild
            engine_dist = bot_tactics._normalized(rbuild(rctx, apply_tactics=False))
            bot_dist = bot_tactics.ball_probabilities(st, bowler)

            self.assertEqual(set(bot_dist), set(engine_dist))
            for k in engine_dist:
                self.assertAlmostEqual(bot_dist[k], engine_dist[k], places=12)

    def test_realism_model_differs_from_legacy_heuristic(self):
        # Proves the parity branch actually swaps the model — the realism
        # distribution is not merely the legacy estimate by coincidence.
        with _Realism(on=True):
            st, bowler = _live_state()
            realism_dist = bot_tactics.ball_probabilities(st, bowler)
        with _Realism(on=False):
            legacy_dist = bot_tactics.ball_probabilities(st, bowler)
        self.assertNotEqual(
            [round(realism_dist[k], 4) for k in sorted(realism_dist)],
            [round(legacy_dist.get(k, 0.0), 4) for k in sorted(realism_dist)])


class ApproachParityTests(unittest.TestCase):

    def test_approach_shaping_matches_engine_approach_model(self):
        with _Realism(on=True):
            st, bowler = _live_state()
            base = bot_tactics.ball_probabilities(st, bowler)
            rating = 78.0

            shaped = bot_tactics.approach_probabilities(
                base, "aggressive", "defensive", rating,
                realism_baseline=base)

            from engine.realism import approaches as ra
            mult = ra.multipliers("aggressive", "defensive", base,
                                  bowler_rating=rating)
            expected = bot_tactics._normalized(
                {k: max(0.0, base.get(k, 0.0) * mult.get(k, 1.0)) for k in base})

            for k in expected:
                self.assertAlmostEqual(shaped[k], expected[k], places=12)

    def test_aggressive_bat_hits_harder_than_defensive(self):
        # The approach axis must bite the same way the engine's does: against
        # the same bowling approach, aggressive batting carries a far higher
        # boundary multiplier and fewer dots than defensive. (Net dismissal
        # risk is a frontier that also depends on the bowling approach, so the
        # trade-off is asserted on the engine-exact parity above, not here.)
        from engine.realism import approaches as ra
        with _Realism(on=True):
            st, bowler = _live_state()
            base = bot_tactics.ball_probabilities(st, bowler)
            agg = ra.multipliers("aggressive", "balanced", base, bowler_rating=78.0)
            dfn = ra.multipliers("defensive", "balanced", base, bowler_rating=78.0)
        self.assertGreater(agg["Four"] + agg["Six"], dfn["Four"] + dfn["Six"])
        self.assertLess(agg["Dot"], dfn["Dot"])


if __name__ == "__main__":
    unittest.main()

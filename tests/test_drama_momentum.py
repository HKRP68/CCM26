"""Bounded momentum (Codex Prompt 05, §8) — continuity without runaway."""

import unittest

from engine.drama import momentum as M

_CFG = {"ema_previous_weight": 0.80, "max_absolute_state": 0.75,
        "window_legal_balls": 12}
_PROBS = {"Dot": 0.35, "Single": 0.33, "Double": 0.07, "Three": 0.005,
          "Four": 0.11, "Six": 0.05, "Wicket": 0.05, "Extras": 0.035}


class MomentumTests(unittest.TestCase):

    def test_stays_within_cap(self):
        hot = [M.ball_record("Six", 6, False, _PROBS) for _ in range(12)]
        cold = [M.ball_record("Wicket", 0, True, _PROBS) for _ in range(12)]
        mh = mc = 0.0
        for _ in range(30):
            mh = M.recompute(mh, hot, _CFG)
            mc = M.recompute(mc, cold, _CFG)
        self.assertLessEqual(mh, 0.75 + 1e-9)
        self.assertGreaterEqual(mc, -0.75 - 1e-9)
        self.assertGreater(mh, 0.0)             # sustained scoring => positive
        self.assertLess(mc, 0.0)                # collapse => negative

    def test_does_not_run_away_after_many_boundaries(self):
        hot = [M.ball_record("Six", 6, False, _PROBS) for _ in range(12)]
        m = 0.0
        seen = []
        for _ in range(50):
            m = M.recompute(m, hot, _CFG)
            seen.append(m)
        self.assertTrue(all(abs(x) <= 0.75 + 1e-9 for x in seen))

    def test_mean_reverts_toward_zero_on_a_neutral_window(self):
        neutral = [M.ball_record("Single", 1, False, _PROBS) for _ in range(12)]
        m = 0.6
        for _ in range(8):
            m = M.recompute(m, neutral, _CFG)
        self.assertLess(abs(m), 0.6)            # decays back toward neutral

    def test_log_deltas_sign_and_zero(self):
        self.assertEqual(M.log_deltas(0.0), {})
        pos = M.log_deltas(0.5)
        self.assertGreater(pos["Six"], 0.0)     # positive momentum lifts scoring
        self.assertLess(pos["Dot"], 0.0)
        self.assertLess(pos["Wicket"], 0.0)
        neg = M.log_deltas(-0.5)
        self.assertLess(neg["Six"], 0.0)        # negative momentum favours bowling
        self.assertGreater(neg["Dot"], 0.0)

    def test_expected_from_probs_is_sane(self):
        er, ew, vr, vw = M.expected_from_probs(_PROBS)
        self.assertGreater(er, 0.0)
        self.assertAlmostEqual(ew, _PROBS["Wicket"], places=6)
        self.assertGreaterEqual(vr, 0.0)
        self.assertGreaterEqual(vw, 0.0)


if __name__ == "__main__":
    unittest.main()

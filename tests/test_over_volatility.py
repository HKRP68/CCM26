"""Over-to-over variation (Historical Cricket Intelligence, Feature 2).

The effect must widen the per-over spread while leaving the mean exactly where
the calibrated per-ball model put it, stay bounded, be reproducible from a seed,
and never revive a zero-probability outcome.
"""

import statistics as S
import unittest

from engine.realism import tilt
from engine.realism.over_volatility import OverVolatility
from engine.sim.rng import SimRng

_BASE = {"Dot": 0.34, "Single": 0.33, "Double": 0.07, "Three": 0.005,
         "Four": 0.12, "Six": 0.06, "Wicket": 0.05, "Extras": 0.035}


def _over_mean_runs(probs):
    """Expected runs per ball from a full outcome distribution (Extras ~1)."""
    rv = dict(tilt.RUN_VALUE, Extras=1, Wicket=0)
    tot = sum(probs.values())
    return sum(probs.get(k, 0.0) * rv.get(k, 0) for k in probs) / (tot or 1.0)


class OverVolatilityTests(unittest.TestCase):

    def _cfg(self, **kw):
        c = {"enabled": True, "sigma": 0.34, "bound": 2.5, "wicket_coupling": 0.06}
        c.update(kw)
        return c

    def test_disabled_is_passthrough(self):
        ov = OverVolatility({"enabled": False}, SimRng(1))
        ov.new_over()
        self.assertIs(ov.apply(_BASE), _BASE)

    def test_zero_z_is_passthrough(self):
        ov = OverVolatility(self._cfg(), rng=None)   # no rng => z stays 0
        ov.new_over()
        self.assertIs(ov.apply(_BASE), _BASE)

    def test_positive_z_lifts_scoring_negative_cuts_it(self):
        ov = OverVolatility(self._cfg())
        ov._z = 2.0
        hi = _over_mean_runs(ov.apply(_BASE))
        ov._z = -2.0
        lo = _over_mean_runs(ov.apply(_BASE))
        base = _over_mean_runs(_BASE)
        self.assertGreater(hi, base)
        self.assertLess(lo, base)

    def test_is_zero_mean_over_many_overs(self):
        # Averaged over the symmetric z draws, the per-over expected runs must
        # return to the base mean — the innings total is not shifted.
        ov = OverVolatility(self._cfg(), SimRng(7))
        base = _over_mean_runs(_BASE)
        means = []
        for _ in range(4000):
            ov.new_over()
            means.append(_over_mean_runs(ov.apply(_BASE)))
        self.assertAlmostEqual(S.mean(means), base, delta=0.03)

    def test_adds_over_variance(self):
        ov = OverVolatility(self._cfg(), SimRng(9))
        means = []
        for _ in range(3000):
            ov.new_over()
            means.append(_over_mean_runs(ov.apply(_BASE)))
        # The spread of per-over expected runs must grow well beyond zero.
        self.assertGreater(S.pstdev(means), 0.15)

    def test_bounded_and_normalised(self):
        ov = OverVolatility(self._cfg(bound=2.5), SimRng(3))
        for _ in range(500):
            z = ov.new_over()
            self.assertLessEqual(abs(z), 2.5 + 1e-9)
            p = ov.apply(_BASE)
            self.assertAlmostEqual(sum(p.values()), 1.0, places=9)
            self.assertTrue(all(v >= 0.0 for v in p.values()))

    def test_zero_outcome_not_revived(self):
        ov = OverVolatility(self._cfg())
        ov._z = 2.0
        w = dict(_BASE, Six=0.0)
        self.assertEqual(ov.apply(w)["Six"], 0.0)

    def test_reproducible_from_seed(self):
        a = OverVolatility(self._cfg(), SimRng(42))
        b = OverVolatility(self._cfg(), SimRng(42))
        self.assertEqual([a.new_over() for _ in range(20)],
                         [b.new_over() for _ in range(20)])


if __name__ == "__main__":
    unittest.main()

"""Realistic over-to-over variation (Historical Cricket Intelligence, Feature 2).

Real T20 cricket is lumpy over to over: a tight over is followed by a flurry,
maidens happen, overs go for twenty. A pure per-ball model with independent
deliveries under-produces that lumpiness — the spread of runs-per-over comes out
a little too narrow and genuine low-scoring overs become rarer than history.

This module adds a ZERO-MEAN, temporary, bounded per-over random effect that
widens the runs-per-over spread to match history WITHOUT moving the average over
score and WITHOUT predetermining any over's result.

Construction (why it is provably mean-neutral):
  * each over draws one standard normal ``z`` on the seeded match RNG;
  * the over's *target* conditional run-mean is ``base_mean + sigma * z`` —
    LINEAR in z — and the weights are tilted to hit it exactly with the existing
    max-entropy solver (``engine.realism.tilt.solve_mean``);
  * because the target is linear and ``E[z] = 0``, the expectation over overs is
    exactly ``base_mean``: the innings total is unchanged, only its variance (and
    the over-to-over variance) grows.
A small symmetric wicket coupling lets a tight over cluster the odd extra wicket
and a loose over slightly fewer, which reproduces the maiden+wicket clustering
without shifting the mean materially.

Introduces no player attribute; a realism-layer effect kept distinct from the
Balanced Drama momentum overlay so the two are not counted twice.
"""

import math

from engine.realism import tilt as _tilt

_RUN_OUTCOMES = _tilt.RUN_OUTCOMES
_MIN_MEAN = 0.05          # feasible run-mean floor for the tilt solver
_MAX_MEAN = 3.60          # feasible run-mean ceiling (all-boundary is 6.0)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


class OverVolatility:
    """One instance per innings. ``new_over`` draws the over's tendency;
    ``apply`` shapes every ball of that over to it (constant within the over)."""

    def __init__(self, cfg=None, rng=None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", False))
        # SD of the per-over conditional run-mean shift (runs per legal ball).
        self.sigma = float(cfg.get("sigma", 0.0))
        self.bound = float(cfg.get("bound", 2.5))          # clamp z, in SD units
        self.wicket_coupling = float(cfg.get("wicket_coupling", 0.06))
        self.rng = rng
        self._z = 0.0

    def new_over(self):
        """Draw this over's zero-mean scoring tendency and return it."""
        if not self.enabled or self.sigma <= 0.0 or self.rng is None:
            self._z = 0.0
            return 0.0
        self._z = _clamp(self.rng.gauss(0.0, 1.0), -self.bound, self.bound)
        return self._z

    @property
    def z(self):
        return self._z

    def apply(self, weights):
        """Return ``weights`` shaped by this over's tendency, renormalised.

        The run outcomes (Dot..Six) are tilted so their conditional mean becomes
        ``base_mean + sigma*z`` exactly; Wicket is nudged by a small symmetric
        factor; Extras and any zero-weight outcome are left untouched. A pure
        pass-through when the effect is off or z is zero.
        """
        if not self.enabled or self._z == 0.0 or self.sigma <= 0.0:
            return weights

        run_part = {k: max(0.0, float(weights.get(k, 0.0))) for k in _RUN_OUTCOMES}
        run_mass = sum(run_part.values())
        if run_mass <= 0.0:
            return weights

        cond = {k: v / run_mass for k, v in run_part.items()}
        base_mean = _tilt.mean_runs(cond)
        target = _clamp(base_mean + self.sigma * self._z, _MIN_MEAN, _MAX_MEAN)
        theta = _tilt.solve_mean(cond, target)
        tilted = _tilt.tilted(cond, theta, 0.0)

        out = dict(weights)
        for k in _RUN_OUTCOMES:
            out[k] = tilted[k] * run_mass          # preserve the run mass
        # A tight over (negative z) brings slightly more wicket chance.
        if "Wicket" in out and out["Wicket"] > 0.0:
            out["Wicket"] *= math.exp(-self.wicket_coupling * self._z)

        total = sum(max(0.0, v) for v in out.values())
        if total <= 0.0:
            return weights
        return {k: max(0.0, v) / total for k, v in out.items()}

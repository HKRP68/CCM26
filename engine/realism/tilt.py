"""Exponential tilting — the one way this engine reshapes a distribution.

A tilt answers: *given a baseline distribution over run outcomes, what is the
distribution with the prior's shape but a different mean (and, optionally, a
different boundary share)?* The maximum-entropy answer is an exponential
family — multiply each outcome by ``exp(theta * r)`` and renormalise — and
bisection solves for ``theta`` because the mean is strictly increasing in it.

Two parameters, because one is not enough:

* ``theta`` moves mass along the run axis. It sets the **expected runs**.
* ``phi`` moves mass onto the boundaries specifically. It sets the
  **boundary share** independently of the mean.

One parameter alone cannot express "rotate the strike": more singles, fewer
boundaries, roughly the same run rate. It also cannot express a powerplay,
which is simultaneously high-dot and high-boundary. Both of those are real
cricket, so the family needs both knobs.

``tools/build_realism_reference.py`` uses the single-parameter form to fit a
shape prior onto a measured scoring level; ``engine/realism/approaches.py``
uses both to turn a batting or bowling intent into outcome multipliers.

Pure functions, no RNG, no config.
"""

import math

RUN_OUTCOMES = ("Dot", "Single", "Double", "Three", "Four", "Six")
RUN_VALUE = {"Dot": 0, "Single": 1, "Double": 2, "Three": 3, "Four": 4, "Six": 6}
BOUNDARY = frozenset(("Four", "Six"))

_TOL = 1e-12
_ITERS = 200
_BOUND = 8.0


def _normalised(prior):
    base = {k: max(0.0, float(prior.get(k, 0.0))) for k in RUN_OUTCOMES}
    total = sum(base.values())
    if total <= 0:
        return {k: 1.0 / len(RUN_OUTCOMES) for k in RUN_OUTCOMES}
    return {k: v / total for k, v in base.items()}


def tilted(prior, theta=0.0, phi=0.0):
    """``p(r) ∝ prior(r) * exp(theta*r + phi*[r is a boundary])``."""
    base = _normalised(prior)
    w = {}
    for k in RUN_OUTCOMES:
        z = theta * RUN_VALUE[k] + (phi if k in BOUNDARY else 0.0)
        w[k] = base[k] * math.exp(max(-_BOUND * 12.0, min(_BOUND * 12.0, z)))
    total = sum(w.values())
    if total <= 0:
        return base
    return {k: v / total for k, v in w.items()}


def mean_runs(dist):
    return sum(RUN_VALUE[k] * dist.get(k, 0.0) for k in RUN_OUTCOMES)


def boundary_share(dist):
    return sum(dist.get(k, 0.0) for k in BOUNDARY)


def _bisect(fn, target, lo=-_BOUND, hi=_BOUND):
    """Solve ``fn(x) == target`` for a monotone increasing ``fn``."""
    if fn(lo) >= target:
        return lo
    if fn(hi) <= target:
        return hi
    for _ in range(_ITERS):
        mid = 0.5 * (lo + hi)
        if fn(mid) < target:
            lo = mid
        else:
            hi = mid
        if hi - lo < _TOL:
            break
    return 0.5 * (lo + hi)


def solve_mean(prior, target_mean):
    """``theta`` such that the tilted mean equals *target_mean*."""
    return _bisect(lambda t: mean_runs(tilted(prior, t, 0.0)), float(target_mean))


def solve_mean_and_boundary(prior, target_mean, target_boundary,
                            outer_iters=60):
    """``(theta, phi)`` hitting both an expected-runs and a boundary-share target.

    The two knobs are coupled — pushing mass onto the boundaries also raises
    the mean — so this alternates: solve ``phi`` for the boundary share at the
    current ``theta``, then re-solve ``theta`` for the mean at the new ``phi``.
    Each step is a monotone 1-D bisection, and the pair converges in a handful
    of rounds for any target the family can reach.

    Targets the family cannot reach (a boundary share above what putting all
    the mass on sixes would give) simply converge to the closest point the
    bounds allow, which is the right failure: a bounded, valid distribution
    rather than an exception on a delivery.
    """
    prior = _normalised(prior)
    theta = solve_mean(prior, target_mean)
    phi = 0.0
    for _ in range(outer_iters):
        phi_new = _bisect(
            lambda p: boundary_share(tilted(prior, theta, p)),
            float(target_boundary))
        theta_new = _bisect(
            lambda t: mean_runs(tilted(prior, t, phi_new)), float(target_mean))
        if abs(phi_new - phi) < 1e-9 and abs(theta_new - theta) < 1e-9:
            theta, phi = theta_new, phi_new
            break
        theta, phi = theta_new, phi_new
    return theta, phi


def multipliers_for(prior, target_mean, target_boundary):
    """Per-outcome multipliers that move *prior* onto both targets.

    Returned as multipliers rather than a distribution so the caller can
    compose them with everything else in the pipeline and let the normaliser
    at the end do its job.
    """
    prior = _normalised(prior)
    theta, phi = solve_mean_and_boundary(prior, target_mean, target_boundary)
    out = tilted(prior, theta, phi)
    return ({k: (out[k] / prior[k] if prior[k] > 0 else 1.0)
             for k in RUN_OUTCOMES},
            {"theta": theta, "phi": phi,
             "mean": mean_runs(out), "boundary": boundary_share(out)})

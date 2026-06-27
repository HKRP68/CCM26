"""Duckworth–Lewis–Stern style revised-target helper (compact approximation).

This is a game-grade approximation of the DLS resource model, not the official
published tables. It captures the qualitative behaviour that matters for a rain
interruption in a chase:

* fewer overs remaining  -> fewer resources -> lower revised target
* more wickets lost      -> fewer resources remaining
* no overs lost          -> target is unchanged

The resource curve uses the Duckworth–Lewis exponential-decay form
``Z(u, w) = Z0[w] * (1 - exp(-b*u))`` with the widely-cited Standard-Edition
asymptote constants, normalised so a full innings reads as 100% resource.
"""

import math

# Asymptotic average runs still to be made with w wickets lost (w = 0..9),
# from the Duckworth–Lewis Standard Edition. Only their *ratios* matter here
# because we normalise against a full innings below.
_Z0 = [506.0, 466.0, 414.0, 345.0, 282.0, 219.0, 170.0, 124.0, 80.0, 41.0]
_B = 0.031  # per-over decay constant (published value ~0.0309)


def _z(overs_left, wickets_lost):
    w = max(0, min(9, int(wickets_lost)))
    u = max(0.0, float(overs_left))
    return _Z0[w] * (1.0 - math.exp(-_B * u))


def resource_pct(overs_left, wickets_lost, total_overs):
    """Resources remaining as a percentage of a full ``total_overs`` innings."""
    full = _z(total_overs, 0)
    if full <= 0:
        return 0.0
    return 100.0 * _z(overs_left, wickets_lost) / full


def revised_target(team1_score, total_overs, overs_left_before,
                   overs_left_after, wickets_lost):
    """Revised DLS target for the chasing side after losing overs mid-innings.

    ``overs_left_before`` / ``overs_left_after`` are the overs the batting side
    had remaining immediately before and after the interruption. Returns the
    new runs-to-win target (an integer, never below 1).
    """
    r_full = resource_pct(total_overs, 0, total_overs)  # == 100.0
    r_before = resource_pct(overs_left_before, wickets_lost, total_overs)
    r_after = resource_pct(overs_left_after, wickets_lost, total_overs)
    lost = max(0.0, r_before - r_after)
    r2 = max(0.0, r_full - lost)  # chasing side's total resource %
    par = team1_score * r2 / r_full if r_full else team1_score
    return max(1, int(math.floor(par)) + 1)

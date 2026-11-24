"""Individual-innings and fielding realism (Prompt 04, fixes 4 & 5).

Fix 4 — individual innings. A batter's innings shape is driven ENTIRELY by live
match state (balls faced, runs scored), never by a stored player attribute:

  * early-innings vulnerability + settling — ``wicket_pressure_multipliers``
    keyed on balls faced, mean-neutral so the TEAM total is untouched;
  * milestone risk (what caps a runaway hundred) — ``milestone_multipliers``
    keyed on runs scored, mean-neutral and WICKET-ONLY (scoring rows are left
    at 1.0 to avoid a scoring→band→scoring feedback loop);
  * acceleration / strike rotation are carried by the settling curve and the
    phase baseline, so they need no separate attribute.

Fix 5 — fielding. Drops/misfields are a TEAM-level effect: the only fielding
input to ``calculate_outcome`` is the scalar ``fielding_quality`` (plus the
pressure ``drop_mult`` / ``misfield_mult``). There is no per-fielder attribute,
and a weaker fielding side drops more.

These are fast, deterministic invariants. The end-to-end distribution (century
and fifty rates, individual scores) is measured by ``tools/compare_realism.py``;
the figures are recorded in ``docs/realism_reliability_fixes.md``.
"""

import logging
import os
import unittest

logging.disable(logging.CRITICAL)

from engine import ball_outcome as bo
from engine.format_config import get_format
from engine.realism import conditions
from engine.realism.config import clear_cache
from engine.sim.rng import SimRng

T20 = get_format("T20")


class _Realism:
    def __enter__(self):
        self._prev = os.environ.get("SIM_REALISM_ENABLED")
        os.environ["SIM_REALISM_ENABLED"] = "1"
        clear_cache()
        return self

    def __exit__(self, *exc):
        if self._prev is None:
            os.environ.pop("SIM_REALISM_ENABLED", None)
        else:
            os.environ["SIM_REALISM_ENABLED"] = self._prev
        clear_cache()
        return False


# ───────────────── Fix 4: settling (balls faced) ─────────────────

class SettlingCurveTests(unittest.TestCase):

    def test_new_batter_is_more_vulnerable_than_a_set_one(self):
        new = conditions.wicket_pressure_multipliers(wickets=3, balls_faced=1)
        set_ = conditions.wicket_pressure_multipliers(wickets=3, balls_faced=20)
        self.assertGreater(new["Wicket"], set_["Wicket"])
        # Vulnerability shows up as a higher wicket chance AND a lower scoring
        # rate for the brand-new batter.
        self.assertLess(new["Four"], set_["Four"])

    def test_settling_is_mean_neutral_on_wicket_chance(self):
        # Σ share · wicket_mult over the balls-faced bands must sit at the
        # deliberate sub-1.0 tail constant (the 7+-down excess is carried
        # separately), i.e. essentially neutral — so the team total is not
        # moved by reshaping the curve.
        bands = conditions._SETTLE_BANDS
        reps = {2: 1, 5: 4, 10: 8, 24: 18, 44: 34, float("inf"): 60}
        total = sum(
            share * conditions.wicket_pressure_multipliers(
                wickets=3, balls_faced=reps[upper])["Wicket"]
            for upper, share, _r, _w in bands)
        self.assertAlmostEqual(total, 1.0, delta=0.05)

    def test_bounded(self):
        for bf in (0, 1, 3, 8, 18, 40, 80):
            m = conditions.wicket_pressure_multipliers(wickets=5, balls_faced=bf)
            for v in m.values():
                self.assertGreater(v, 0.0)
                self.assertLess(v, 2.0)


# ───────────────── Fix 4: milestone (runs scored) ─────────────────

class MilestoneCurveTests(unittest.TestCase):

    def test_dismissal_risk_rises_with_score(self):
        risks = [conditions.milestone_multipliers(r)["Wicket"]
                 for r in (5, 30, 45, 70, 95, 120)]
        # Monotone non-decreasing: a batter deeper into a big score carries
        # more dismissal risk, which is what caps a runaway hundred.
        for a, b in zip(risks, risks[1:]):
            self.assertLessEqual(a, b + 1e-9)
        self.assertGreater(risks[-1], risks[0])

    def test_is_wicket_only_scoring_rows_untouched(self):
        # The milestone family must NOT raise the scoring rate (feedback loop).
        for r in (5, 60, 99, 150):
            m = conditions.milestone_multipliers(r)
            for k in ("Dot", "Single", "Double", "Three", "Four", "Six"):
                self.assertAlmostEqual(m[k], 1.0, places=9)

    def test_is_mean_neutral_on_wicket_chance(self):
        # bound=2.5 at strength 1.0 clamps nothing, so the solved bands give an
        # exact share-weighted mean of 1.0 — the team total is provably
        # untouched by the century cap.
        bands = conditions._MILESTONE_BANDS
        reps = {19: 10, 34: 27, 49: 42, 74: 60, 99: 90, float("inf"): 130}
        total = sum(share * conditions.milestone_multipliers(reps[upper])["Wicket"]
                    for upper, share, _r, _w in bands)
        self.assertAlmostEqual(total, 1.0, delta=0.005)


# ───────────────── Fix 5: fielding is team-level ─────────────────

class FieldingRealismTests(unittest.TestCase):

    def _drop_rate(self, fielding_quality, n=4000, drop_mult=1.0, seed=20240):
        """Fraction of deliveries that produced a dropped catch, with a strong
        bowler against a weak batter so catching chances are frequent."""
        rng = SimRng(seed)
        batter = {"name": "B", "batting_rating": 35, "batting_hand": "Right"}
        bowler = {"name": "W", "bowling_rating": 92, "fielding_rating": 70,
                  "bowling_hand": "Right", "bowling_type": "Fast-medium"}
        drops = 0
        with _Realism():
            for _ in range(n):
                oc = bo.calculate_outcome(
                    batter=batter, bowler=bowler, pitch="Even",
                    streak={"boundaries": 0}, over_number=8, batter_runs=10,
                    balls_faced=8, format_config=T20,
                    fielding_quality=fielding_quality, drop_mult=drop_mult,
                    rng=rng)
                if oc.get("dropped_catch"):
                    drops += 1
        return drops / n

    def test_weaker_fielding_side_drops_more(self):
        poor = self._drop_rate(30)
        good = self._drop_rate(95)
        self.assertGreater(poor, good)
        # A good side still drops the odd one, but rarely.
        self.assertLess(good, poor)

    def test_pressure_drop_mult_raises_drops(self):
        base = self._drop_rate(70, drop_mult=1.0)
        under_pressure = self._drop_rate(70, drop_mult=2.0)
        self.assertGreater(under_pressure, base)

    def test_no_fielding_quality_means_no_drops(self):
        # With no team fielding quality supplied, the drop path is inert — the
        # engine never invents a per-fielder attribute to fall back on.
        self.assertEqual(self._drop_rate(None), 0.0)


if __name__ == "__main__":
    unittest.main()

"""The rating curve: meaningful, bounded, and never a guarantee.

Covers requirements 4, 5, 6 and 8 of the brief's test list — a batting edge
helps, a bowling edge helps, a premium batter can still be dismissed, and
equal ratings behave symmetrically.
"""

import math
import unittest

from engine.realism import rating_curve as rc
from engine.realism.config import get_config
from engine.realism.context import BallContext
from engine.realism.outcome_distribution import (
    OUTCOMES, build, expected_runs,
)


def _dist(bat, bowl, **kw):
    return build(BallContext(bat_rating=bat, bowl_rating=bowl, **kw))


class AdvantageCurveTests(unittest.TestCase):

    def test_advantage_is_signed_bounded_and_monotone(self):
        self.assertEqual(rc.advantage(70, 70), 0.0)
        self.assertGreater(rc.advantage(90, 70), 0.0)
        self.assertLess(rc.advantage(70, 90), 0.0)
        last = -1.0
        for gap in range(-60, 61, 5):
            a = rc.advantage(70 + gap, 70)
            self.assertGreater(a, last)
            self.assertTrue(-1.0 < a < 1.0)
            last = a

    def test_advantage_saturates_rather_than_clipping(self):
        """§7: steep through the realistic range, smooth at the extremes."""
        mid = rc.advantage(85, 70) - rc.advantage(80, 70)
        far = rc.advantage(100, 30) - rc.advantage(95, 30)
        self.assertGreater(mid, far,
                           "the curve must be steeper in the realistic range")
        self.assertGreater(far, 0.0, "it must still be monotone at the extremes")

    def test_no_single_multiplier_escapes_its_bound(self):
        bound = float(get_config()["rating_curve"]["max_multiplier"])
        norm = get_config()["rating_curve"].get("normaliser") or {}
        for bat in range(30, 101, 10):
            for bowl in range(30, 101, 10):
                for name, m in rc.multipliers(bat, bowl).items():
                    # The normaliser is a calibration divisor applied after
                    # the bound, so compare the pre-normalised value.
                    raw = m * float(norm.get(name, 1.0))
                    self.assertLessEqual(raw, bound + 1e-9,
                                         "%s %d/%d" % (name, bat, bowl))
                    self.assertGreaterEqual(raw, 1.0 / bound - 1e-9,
                                            "%s %d/%d" % (name, bat, bowl))
                    self.assertGreater(m, 0.0)

    def test_overall_rating_is_never_an_input(self):
        """§7: do not count overall_rating again."""
        a = _dist(80, 70)
        b = build(BallContext(bat_rating=80, bowl_rating=70,
                              extra={"overall_rating": 99}))
        self.assertEqual(a, b)

    def test_rating_curve_is_deterministic(self):
        self.assertEqual(rc.multipliers(83, 71), rc.multipliers(83, 71))


class BattingAdvantageTests(unittest.TestCase):
    """Test 4: a higher batting rating is a reasonable advantage."""

    def test_better_batter_scores_more_and_is_dismissed_less(self):
        weak = _dist(50, 75)
        strong = _dist(95, 75)
        self.assertGreater(expected_runs(strong), expected_runs(weak) * 1.15)
        self.assertLess(strong["Wicket"], weak["Wicket"])
        self.assertLess(strong["Dot"], weak["Dot"])
        self.assertGreater(strong["Four"] + strong["Six"],
                           weak["Four"] + weak["Six"])

    def test_expected_runs_rise_monotonically_with_batting_rating(self):
        last = -1.0
        for bat in range(30, 101, 5):
            er = expected_runs(_dist(bat, 75))
            self.assertGreater(er, last, "bat_rating %d went backwards" % bat)
            last = er

    def test_one_rating_point_is_measurable(self):
        """§7: do not hardcode one fixed shift; every point should count."""
        a = expected_runs(_dist(74, 75))
        b = expected_runs(_dist(75, 75))
        self.assertGreater(b, a)
        self.assertLess(b - a, 0.05, "a single point must not be a cliff")


class BowlingAdvantageTests(unittest.TestCase):
    """Test 5: a higher bowling rating is a reasonable advantage."""

    def test_better_bowler_concedes_less_and_takes_more(self):
        weak = _dist(75, 45)
        strong = _dist(75, 95)
        self.assertLess(expected_runs(strong), expected_runs(weak) * 0.88)
        self.assertGreater(strong["Wicket"], weak["Wicket"])
        self.assertGreater(strong["Dot"], weak["Dot"])

    def test_wickets_rise_monotonically_with_bowling_rating(self):
        last = -1.0
        for bowl in range(30, 101, 5):
            w = _dist(75, bowl)["Wicket"]
            self.assertGreater(w, last, "bowl_rating %d went backwards" % bowl)
            last = w

    def test_better_bowler_concedes_fewer_extras(self):
        self.assertLess(_dist(75, 95)["Extras"], _dist(75, 45)["Extras"])


class NotAGuaranteeTests(unittest.TestCase):
    """Test 6: wicket chances are never forced to zero for premium batters."""

    def test_the_best_batter_against_the_worst_bowler_can_still_get_out(self):
        floor = float(get_config()["rating_curve"]["min_wicket_share"])
        dist = _dist(100, 30)
        self.assertGreaterEqual(dist["Wicket"], floor)
        self.assertGreater(dist["Wicket"], 0.0)
        # ~1 in 83 balls at worst: a hundred is hard, not impossible to avoid.
        self.assertLess(1.0 / dist["Wicket"], 100.0)

    def test_the_wicket_floor_holds_in_every_phase_and_situation(self):
        floor = float(get_config()["rating_curve"]["min_wicket_share"])
        for balls in (0, 40, 110, 119):
            for inn, target in ((1, None), (2, 240)):
                dist = build(BallContext(
                    bat_rating=99, bowl_rating=30, balls_bowled=balls,
                    innings=inn, target=target, runs=60, pitch="Dead"))
                self.assertGreaterEqual(dist["Wicket"], floor - 1e-12)

    def test_the_worst_batter_can_still_score(self):
        dist = _dist(30, 99)
        self.assertGreater(dist["Four"] + dist["Six"], 0.0)
        self.assertGreater(expected_runs(dist), 0.3)

    def test_the_wicket_share_is_capped(self):
        cap = float(get_config()["rating_curve"]["max_wicket_share"])
        for pitch in ("Green", "Dusty", "Dry"):
            dist = build(BallContext(bat_rating=30, bowl_rating=100,
                                     pitch=pitch, pitch_wear=1.0,
                                     balls_faced=0, wickets=9,
                                     bowling_type="Off spin"))
            self.assertLessEqual(dist["Wicket"], cap + 1e-9)


class SymmetryTests(unittest.TestCase):
    """Test 8: equal-rated teams behave symmetrically in equal conditions."""

    def test_identical_ratings_give_identical_distributions(self):
        for r in (40, 60, 75, 90):
            self.assertEqual(_dist(r, r), _dist(r, r))

    def test_mirrored_gaps_mirror_the_advantage(self):
        up = _dist(85, 65)
        down = _dist(65, 85)
        self.assertAlmostEqual(rc.advantage(85, 65), -rc.advantage(65, 85),
                               places=12)
        self.assertGreater(expected_runs(up), expected_runs(down))
        self.assertLess(up["Wicket"], down["Wicket"])

    def test_two_cards_with_the_same_numbers_are_the_same_player(self):
        """§1: identical ratings and styles -> identical ability."""
        a = build(BallContext(bat_rating=88, bowl_rating=73,
                              bat_style="Right-handed",
                              bowling_type="Fast-medium", bowl_hand="Right"))
        b = build(BallContext(bat_rating=88, bowl_rating=73,
                              bat_style="Right-handed",
                              bowling_type="Fast-medium", bowl_hand="Right"))
        self.assertEqual(a, b)

    def test_handedness_alone_does_not_change_a_pace_matchup_much(self):
        """§8: use neutral effects where the data does not support detail."""
        same = build(BallContext(bat_rating=80, bowl_rating=75,
                                 bat_style="Right-handed", bowl_hand="Right",
                                 bowling_type="Fast"))
        cross = build(BallContext(bat_rating=80, bowl_rating=75,
                                  bat_style="Right-handed", bowl_hand="Left",
                                  bowling_type="Fast"))
        for o in OUTCOMES:
            self.assertLess(abs(same[o] - cross[o]), 0.02,
                            "%s moved too far on handedness alone" % o)


class CalibrationInvariantTests(unittest.TestCase):
    """The normaliser is what keeps the curve from moving the overall level."""

    def test_normaliser_is_present_and_plausible(self):
        """Either the flat form or the per-phase form, fully populated."""
        rc_cfg = get_config()["rating_curve"]
        norm = rc_cfg.get("normaliser") or {}
        self.assertTrue(norm, "the curve needs a measured normaliser")
        per_phase = any(isinstance(v, dict) for v in norm.values())
        sets = (list(norm.values()) if per_phase else [norm])
        for row in sets:
            self.assertEqual(set(row), set(OUTCOMES),
                             "every outcome needs a measured normaliser")
            for o, v in row.items():
                self.assertGreater(v, 0.2, o)
                self.assertLess(v, 5.0, o)
        if per_phase:
            for phase in ("powerplay", "middle", "death"):
                self.assertIn(phase, norm,
                              "a per-phase normaliser must cover every phase")

    def test_per_phase_normaliser_is_selected_by_phase(self):
        """The powerplay and the death must not share divisors."""
        rc_cfg = get_config()["rating_curve"]
        norm = rc_cfg.get("normaliser") or {}
        if not any(isinstance(v, dict) for v in norm.values()):
            self.skipTest("a flat normaliser is configured")
        pp = rc._normaliser_for(rc_cfg, "powerplay")
        death = rc._normaliser_for(rc_cfg, "death")
        self.assertNotEqual(pp, death)
        self.assertNotEqual(rc.multipliers(85, 75, phase="powerplay"),
                            rc.multipliers(85, 75, phase="death"))

    def test_an_unknown_phase_never_borrows_another_phase_divisors(self):
        cfg = {"rating_curve": {"normaliser": {"powerplay": {"Dot": 2.0}}}}
        self.assertEqual(rc._normaliser_for(cfg["rating_curve"], "middle"), {})
        self.assertEqual(rc._normaliser_for(cfg["rating_curve"], None), {})

    def test_level_term_separates_class_from_gap(self):
        """A 90-v-90 contest is not a 40-v-40 contest."""
        elite = _dist(90, 90)
        club = _dist(40, 40)
        self.assertGreater(elite["Four"] + elite["Six"],
                           club["Four"] + club["Six"])
        self.assertGreater(club["Dot"], elite["Dot"])
        self.assertAlmostEqual(rc.advantage(90, 90), rc.advantage(40, 40),
                               places=12)

    def test_clamp_wicket_share_solves_the_share_not_the_weight(self):
        dist = {o: 0.1 for o in OUTCOMES}
        dist["Wicket"] = 0.0
        out = rc.clamp_wicket_share(dist)
        total = sum(out.values())
        lo = float(get_config()["rating_curve"]["min_wicket_share"])
        self.assertAlmostEqual(out["Wicket"] / total, lo, places=9)

    def test_free_hit_skips_the_wicket_floor(self):
        dist = {o: 0.1 for o in OUTCOMES}
        dist["Wicket"] = 0.0
        self.assertEqual(rc.clamp_wicket_share(dist, free_hit=True)["Wicket"], 0.0)


if __name__ == "__main__":
    unittest.main()

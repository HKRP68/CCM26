"""Tests for the chase/defence chance model (engine/chase_chance.py).

Pure Python — no Telegram / DB — so it runs under plain unittest.
"""
import unittest

from engine import chase_chance as cc


class MatrixTests(unittest.TestCase):
    def test_run_range_boundaries(self):
        self.assertEqual(cc.get_run_range(15), "1-15")
        self.assertEqual(cc.get_run_range(16), "16-20")
        self.assertEqual(cc.get_run_range(30), "26-30")
        self.assertEqual(cc.get_run_range(50), "46-50")
        self.assertEqual(cc.get_run_range(51), "51+")
        self.assertEqual(cc.get_run_range(200), "51+")

    def test_wicket_index_boundaries(self):
        self.assertEqual(cc.get_wicket_index(0), 0)
        self.assertEqual(cc.get_wicket_index(3), 0)
        self.assertEqual(cc.get_wicket_index(4), 1)
        self.assertEqual(cc.get_wicket_index(5), 1)
        self.assertEqual(cc.get_wicket_index(6), 2)
        self.assertEqual(cc.get_wicket_index(8), 3)

    def test_base_chase_chance_matches_spec(self):
        # The headline example from the spec.
        self.assertEqual(cc.base_chase_chance(30, 5), 56)
        self.assertEqual(cc.base_chase_chance(15, 0), 90)
        self.assertEqual(cc.base_chase_chance(45, 7), 14)
        self.assertEqual(cc.base_chase_chance(200, 9), 1)

    def test_monotonic_in_runs_and_wickets(self):
        # More runs needed → never easier; more wickets lost → never easier.
        for wl in (0, 4, 6, 8):
            vals = [cc.base_chase_chance(r, wl) for r in (10, 20, 30, 40, 50, 60)]
            self.assertEqual(vals, sorted(vals, reverse=True))
        for rn in (15, 30, 45):
            vals = [cc.base_chase_chance(rn, w) for w in (0, 4, 6, 8)]
            self.assertEqual(vals, sorted(vals, reverse=True))


class FinalChanceTests(unittest.TestCase):
    def test_modified_example_from_spec(self):
        # Base 56 + set batter (+5) + elite bowler (-5) + green (-3) + momentum(+2).
        out = cc.final_chase_chance(30, 5, batter_mod=5, bowler_mod=-5,
                                    pitch_mod=-3, momentum_mod=2)
        self.assertEqual(out["base_chasing_chance"], 56)
        self.assertEqual(out["total_modifier"], -1)
        self.assertEqual(out["chasing_chance"], 55)
        self.assertEqual(out["defending_chance"], 45)

    def test_total_modifier_clamped(self):
        out = cc.final_chase_chance(30, 5, batter_mod=20, bowler_mod=20)
        self.assertEqual(out["total_modifier"], 15)   # clamped to +15
        self.assertEqual(out["chasing_chance"], 71)    # 56 + 15

    def test_final_chance_clamped_1_99(self):
        lo = cc.final_chase_chance(200, 9, bowler_mod=-15)
        self.assertGreaterEqual(lo["chasing_chance"], 1)
        hi = cc.final_chase_chance(10, 0, batter_mod=15)
        self.assertLessEqual(hi["chasing_chance"], 99)
        self.assertEqual(lo["chasing_chance"] + lo["defending_chance"], 100)


class ModifierTests(unittest.TestCase):
    def test_batter_modifier(self):
        elite = {"bat_rating": 92}
        good = {"bat_rating": 75}
        weak = {"bat_rating": 50}
        # Set elite batter on strike (10+ runs) → +5; two recognised bats → +4.
        self.assertEqual(cc.batter_modifier(elite, good, striker_runs=20), 9)
        # Both genuine non-batsmen → -6.
        self.assertEqual(cc.batter_modifier(weak, {"bat_rating": 55}), -6)

    def test_bowler_modifier(self):
        self.assertEqual(cc.bowler_modifier({"bowl_rating": 95}), -5)   # elite death
        self.assertEqual(cc.bowler_modifier({"bowl_rating": 60}), 5)    # weak
        self.assertEqual(cc.bowler_modifier({"bowl_rating": 60}, is_emergency=True), 8)

    def test_pitch_modifier(self):
        self.assertEqual(cc.pitch_modifier("Green"), -3)
        self.assertEqual(cc.pitch_modifier("Flat"), 3)
        self.assertEqual(cc.pitch_modifier("Hard"), 0)


class SteerTests(unittest.TestCase):
    def test_neutral_chance_is_no_op(self):
        eff = cc.chase_steer_effects(50)
        self.assertAlmostEqual(eff["boundary_modifier"], 1.0)
        self.assertAlmostEqual(eff["wicket_modifier"], 1.0)
        self.assertAlmostEqual(eff["dot_bonus"], 0.0)

    def test_chasing_favoured_helps_bat(self):
        eff = cc.chase_steer_effects(90)
        self.assertGreater(eff["boundary_modifier"], 1.0)   # more boundaries
        self.assertLess(eff["wicket_modifier"], 1.0)        # fewer wickets
        self.assertLess(eff["dot_bonus"], 0.0)              # fewer dots

    def test_defending_favoured_helps_bowl(self):
        eff = cc.chase_steer_effects(10)
        self.assertLess(eff["boundary_modifier"], 1.0)
        self.assertGreater(eff["wicket_modifier"], 1.0)
        self.assertGreater(eff["dot_bonus"], 0.0)


if __name__ == "__main__":
    unittest.main()

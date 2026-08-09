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
        self.assertEqual(out["total_modifier"], cc.MODIFIER_CLAMP)
        self.assertEqual(out["chasing_chance"], 56 + cc.MODIFIER_CLAMP)

    def test_situation_mod_counts_toward_the_same_clamp(self):
        """The Step-2 match factors are not a way around the modifier ceiling."""
        out = cc.final_chase_chance(30, 5, batter_mod=20, situation_mod=20)
        self.assertEqual(out["total_modifier"], cc.MODIFIER_CLAMP)

    def test_final_chance_clamped_to_the_spec_caps(self):
        """No chase is guaranteed, and none is dead while it is still possible."""
        lo = cc.final_chase_chance(200, 9, bowler_mod=-15)
        self.assertGreaterEqual(lo["chasing_chance"], cc.LOWER_CAP)
        hi = cc.final_chase_chance(10, 0, batter_mod=25)
        self.assertLessEqual(hi["chasing_chance"], cc.UPPER_CAP)
        self.assertEqual(lo["chasing_chance"] + lo["defending_chance"], 100)


class Step2ModifierTests(unittest.TestCase):
    """Section 9 Step 2 — the factors the runs x wickets matrix cannot see."""

    def test_powerplay_bonus_needs_sixty(self):
        self.assertEqual(cc.powerplay_modifier(59), 0)
        self.assertEqual(cc.powerplay_modifier(60), cc.POWERPLAY_BONUS)

    def test_deterioration_only_bites_when_batting_last(self):
        self.assertEqual(cc.deterioration_modifier("Dusty", innings=2),
                         cc.DETERIORATION_PENALTY)
        self.assertEqual(cc.deterioration_modifier("Dusty", innings=1), 0)
        self.assertEqual(cc.deterioration_modifier("Flat", innings=2), 0)

    def test_nothing_to_lose_needs_both_a_road_and_a_monster(self):
        self.assertEqual(cc.nothing_to_lose_modifier(265, "Flat"), cc.NTL_BONUS)
        self.assertEqual(cc.nothing_to_lose_modifier(265, "Dusty"), 0)
        self.assertEqual(cc.nothing_to_lose_modifier(200, "Flat"), 0)

    def test_death_bowler_penalty_needs_the_phase_the_trait_and_the_rating(self):
        elite = {"bowl_rating": 90}
        self.assertEqual(cc.bowler_modifier(elite), -5)
        self.assertEqual(
            cc.bowler_modifier(elite, is_death_phase=True, has_death_trait=True),
            -5 + cc.DEATH_BOWLER_PENALTY)
        # The trait alone, outside the death overs, changes nothing.
        self.assertEqual(cc.bowler_modifier(elite, has_death_trait=True), -5)
        # And an 80-rated death bowler is not the bowler this rule is about.
        self.assertEqual(
            cc.bowler_modifier({"bowl_rating": 80}, is_death_phase=True,
                               has_death_trait=True), 0)

    def test_emergency_bowler_still_short_circuits_everything(self):
        self.assertEqual(
            cc.bowler_modifier({"bowl_rating": 95}, is_emergency=True,
                               is_death_phase=True, has_death_trait=True), 8)

    def test_mpi_modifier_nets_momentum_against_pressure(self):
        self.assertEqual(cc.mpi_modifier(1.0, 0.0), 5.0)
        self.assertEqual(cc.mpi_modifier(0.0, 1.0), -6.0)
        self.assertEqual(cc.mpi_modifier(1.0, 1.0), -1.0)
        # Each side is capped separately.
        self.assertEqual(cc.mpi_modifier(10.0, 0.0), cc.MPI_MOMENTUM_CAP)
        self.assertEqual(cc.mpi_modifier(0.0, 10.0), -cc.MPI_PRESSURE_CAP)

    def test_an_impossible_ask_is_zero_not_the_floor(self):
        """The lower cap is for chases that are merely hopeless. 40 off 1 is
        over, and the model has to be able to say so."""
        info = cc.final_chase_chance(40, 5)
        out = cc.apply_feasibility(dict(info), 40, 1)
        self.assertEqual(out["chasing_chance"], 0)
        # Still alive, however unlikely → never below the floor.
        alive = cc.apply_feasibility(dict(info), 40, 24)
        self.assertGreaterEqual(alive["chasing_chance"], cc.LOWER_CAP)


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

    def test_no_single_boost_emitted(self):
        # single_boost would override the pressure engine's strike_rotation_penalty.
        for c in (10, 50, 90):
            self.assertNotIn("single_boost", cc.chase_steer_effects(c))

    def test_strength_above_one_keeps_multipliers_nonnegative(self):
        eff = cc.chase_steer_effects(0, strength=5.0)
        self.assertGreaterEqual(eff["boundary_modifier"], 0.0)
        self.assertGreaterEqual(eff["wicket_modifier"], 0.0)

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


class FeasibilityTests(unittest.TestCase):
    def test_comfortable_rate_unscaled(self):
        self.assertEqual(cc.feasibility_factor(30, 30), 1.0)   # 6 rpo
        self.assertEqual(cc.feasibility_factor(12, 6), 1.0)    # 12 rpo (2/ball)

    def test_impossible_ask_crashes_chance(self):
        # 16 off 1 must NOT read as batting-favoured.
        info = cc.final_chase_chance(16, 2)        # matrix alone is high
        self.assertGreater(info["chasing_chance"], 50)
        scaled = cc.apply_feasibility(dict(info), 16, 1)
        self.assertLessEqual(scaled["chasing_chance"], 5)
        self.assertEqual(scaled["chasing_chance"] + scaled["defending_chance"], 100)

    def test_factor_monotonic_in_rate(self):
        f2 = cc.feasibility_factor(12, 6)   # 2/ball
        f3 = cc.feasibility_factor(18, 6)   # 3/ball
        f5 = cc.feasibility_factor(30, 6)   # 5/ball
        self.assertGreaterEqual(f2, f3)
        self.assertGreaterEqual(f3, f5)
        self.assertEqual(cc.feasibility_factor(36, 6), 0.0)  # 6/ball → ~hopeless


if __name__ == "__main__":
    unittest.main()


class DeathTraitLookupTests(unittest.TestCase):
    """The Step-2 death-bowler penalty is keyed on the trait engine's own
    ``effect_key``, not on the display name — display names are admin-editable,
    so matching on them would leave the rule one rename from never firing."""

    def setUp(self):
        import services.cipl_match as cm
        self.cm = cm

    def test_the_two_death_gated_effects_are_recognised(self):
        from services import trait_engine
        for key in self.cm._DEATH_TRAIT_KEYS:
            self.assertIn(key, trait_engine.TRAIT_HANDLERS,
                          f"{key} is not a real trait effect")
            self.assertTrue(self.cm._has_death_trait(
                {"traits": [{"effect_key": key, "level": 2}]}), key)

    def test_other_traits_and_missing_traits_do_not_count(self):
        self.assertFalse(self.cm._has_death_trait({}))
        self.assertFalse(self.cm._has_death_trait({"traits": []}))
        self.assertFalse(self.cm._has_death_trait(
            {"traits": [{"effect_key": "bowl_economy"}]}))
        # A /cipl player carries no traits at all.
        self.assertFalse(self.cm._has_death_trait({"traits": None}))

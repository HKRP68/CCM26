"""Batting and bowling have equal, bounded influence on outcome weights."""

import unittest
from unittest.mock import patch

from services import cipl_match
from services.probability_engine import BASE
from services.trait_engine import TRAIT_HANDLERS, TRAIT_ROLES, apply_traits, trait_allowed


def _trait(key, level=1):
    return {"effect_key": key, "level": level, "display_name": key}


class TraitBalanceTests(unittest.TestCase):
    def test_rare_wickets_and_boundaries_get_the_same_relative_boost(self):
        batting, bowling = dict(BASE), dict(BASE)
        def single_bucket(ctx, strength, role):
            return [("6" if role == "batter" else "W", strength)]

        with (patch.dict(TRAIT_HANDLERS, {"_single": single_bucket}),
              patch.dict(TRAIT_ROLES, {"_single": ("batter", "bowler")})):
            apply_traits(batting, [_trait("_single")], [], {})
            apply_traits(bowling, [], [_trait("_single")], {})
        self.assertAlmostEqual(batting["6"] / BASE["6"], bowling["W"] / BASE["W"])
        self.assertAlmostEqual(bowling["W"], 4.083333333333333)

    def test_anchor_counters_wicket_hunter_at_every_level(self):
        for level in range(1, 6):
            with self.subTest(level=level):
                probs = dict(BASE)
                apply_traits(probs, [_trait("bat_anchor", level)],
                             [_trait("bowl_wicket_hunter", level)], {})
                hunter_only = dict(BASE)
                apply_traits(hunter_only, [], [_trait("bowl_wicket_hunter", level)], {})
                self.assertLess(probs["W"], hunter_only["W"])
                self.assertLess(probs["W"], BASE["W"] * 1.01)
                self.assertLess(probs["6"], BASE["6"])

    def test_equal_opposing_stacks_cancel_at_every_level(self):
        def mirror(ctx, strength, role):
            sign = -1 if role == "batter" else 1
            return [("W", sign * strength), ("6", -sign * strength / 2)]

        with (patch.dict(TRAIT_HANDLERS, {"_mirror": mirror}),
              patch.dict(TRAIT_ROLES, {"_mirror": ("batter", "bowler")})):
            for level in range(1, 6):
                with self.subTest(level=level):
                    traits = [_trait("_mirror", level)] * 3
                    probs = dict(BASE)
                    apply_traits(probs, traits, traits, {})
                    self.assertEqual(probs, BASE)

    def test_missing_or_inactive_opponent_does_not_give_away_its_share(self):
        alone, inactive = dict(BASE), dict(BASE)
        apply_traits(alone, [], [_trait("bowl_wicket_hunter")], {})
        apply_traits(inactive, [_trait("bat_finisher")],
                     [_trait("bowl_wicket_hunter")],
                     {"over": 10, "total_overs": 20})
        self.assertEqual(alone, inactive)
        self.assertAlmostEqual(alone["W"], 4.083333333333333)

    def test_boosts_and_reductions_share_the_same_cap_for_both_roles(self):
        def changes(ctx, strength, role):
            sign = ctx["sign"]
            return [("W", sign * strength), ("6", sign * strength),
                    ("4", sign * strength)]

        magnitudes = []
        with (patch.dict(TRAIT_HANDLERS, {"_changes": changes}),
              patch.dict(TRAIT_ROLES, {"_changes": ("batter", "bowler")})):
            for role in ("batter", "bowler"):
                for sign in (-1, 1):
                    with self.subTest(role=role, sign=sign):
                        traits = [_trait("_changes", 5)] * 3
                        probs = dict(BASE)
                        apply_traits(probs, traits if role == "batter" else [],
                                     traits if role == "bowler" else [],
                                     {"sign": sign})
                        relative_change = sum(
                            abs(probs[key] / BASE[key] - 1.0)
                            for key in BASE)
                        self.assertLess(relative_change, 0.125)
                        self.assertGreater(relative_change, 0.10)
                        magnitudes.append(relative_change)
        for magnitude in magnitudes[1:]:
            self.assertAlmostEqual(magnitude, magnitudes[0])

    def test_mixed_sign_traits_cannot_bypass_the_cap(self):
        probs = dict(BASE)
        apply_traits(probs, [], [_trait("bowl_economy", 5),
                                _trait("elite_unplayable", 5),
                                _trait("bowl_wicket_hunter", 5)],
                     {"boundary_streak": 2})
        relative_change = sum(abs(probs[key] / BASE[key] - 1.0)
                              for key in BASE)
        self.assertLess(relative_change, 0.125)
        self.assertGreater(relative_change, 0.0)
        self.assertLess(probs["6"], BASE["6"])
        self.assertGreater(probs["W"], BASE["W"])

    def test_max_level_wicket_hunter_stays_close_to_the_base_wicket_chance(self):
        probs = dict(BASE)
        apply_traits(probs, [], [_trait("bowl_wicket_hunter", 5)], {})
        self.assertAlmostEqual(probs["W"], 4.259615384615385)
        normalized_wicket_pct = 100.0 * probs["W"] / sum(probs.values())
        self.assertLess(normalized_wicket_pct, 5.0)

    def test_levels_still_increase_the_effect(self):
        wickets = []
        for level in range(1, 6):
            probs = dict(BASE)
            apply_traits(probs, [], [_trait("bowl_wicket_hunter", level)], {})
            wickets.append(probs["W"])
        self.assertEqual(wickets, sorted(set(wickets)))

    def test_zero_weight_outcomes_stay_disabled(self):
        probs = dict(BASE, W=0.0, **{"6": 0.0})
        apply_traits(probs, [_trait("bat_finisher", 5)],
                     [_trait("bowl_wicket_hunter", 5)],
                     {"over": 20, "total_overs": 20})
        self.assertEqual(probs["W"], 0.0)
        self.assertEqual(probs["6"], 0.0)

    def test_empty_unknown_and_inactive_traits_keep_the_original_weights(self):
        for traits in ([], [_trait("unknown")], [_trait("bat_finisher")]):
            with self.subTest(traits=traits):
                probs = dict(BASE)
                activated = apply_traits(probs, traits, [], {})
                self.assertEqual(probs, BASE)
                self.assertEqual(activated, [])


class TraitRoleAndUpgradeTests(unittest.TestCase):
    @staticmethod
    def context(key, role):
        return {
            "over": 2 if key in ("bat_pinch_hitter", "bowl_powerplay") else
                    10 if key == "bowl_middle_squeeze" else 20,
            "total_overs": 20, "rrr": 12, "balls_left": 6, "wickets": 8,
            "bat_balls_faced": 5 if key == "bat_fast_starter" else
                               12 if key == "aware_pitch" else 30,
            "bat_runs": 10, "bat_boundaries": 5, "boundary_streak": 3,
            "bowler_balls": 0 if key == "elite_golden_arm" else
                            12 if key == "aware_pitch" else 18,
            "bowler_runs": 45, "partnership_runs": 70,
            "bat_position": 3 if key == "elite_big_fish" else 9,
            "is_spin": key != "bat_pace_destroyer",
            "bat_rating": 60 if role == "batter" else 80,
            "bowl_rating": 80 if role == "batter" else 60,
            "bat_hand": "Left", "bowl_hand": "Right",
        }

    def test_every_catalogue_handler_has_explicit_roles(self):
        self.assertEqual(set(TRAIT_HANDLERS), set(TRAIT_ROLES))
        for roles in TRAIT_ROLES.values():
            self.assertTrue(roles)
            self.assertTrue(set(roles) <= {"batter", "bowler"})

    def test_wrong_role_traits_never_activate_or_change_probabilities(self):
        for key, roles in TRAIT_ROLES.items():
            for role in {"batter", "bowler"} - set(roles):
                with self.subTest(key=key, role=role):
                    probs = dict(BASE)
                    traits = [_trait(key, 5)]
                    activated = apply_traits(
                        probs, traits if role == "batter" else [],
                        traits if role == "bowler" else [], self.context(key, role))
                    self.assertEqual(activated, [])
                    self.assertEqual(probs, BASE)

    def test_every_active_trait_improves_from_level_four_to_five(self):
        with (patch("services.trait_engine._over_roll", return_value=0.0),
              patch("services.trait_engine.random.random", return_value=0.0)):
            for key, roles in TRAIT_ROLES.items():
                for role in roles:
                    changes = []
                    for level in (4, 5):
                        with self.subTest(key=key, role=role, level=level):
                            probs = dict(BASE)
                            traits = [_trait(key, level)]
                            active = apply_traits(
                                probs, traits if role == "batter" else [],
                                traits if role == "bowler" else [], self.context(key, role))
                            self.assertTrue(active, f"missing activation fixture for {key}")
                            changes.append(sum(abs(probs[k] / BASE[k] - 1) for k in BASE))
                    self.assertGreater(changes[1], changes[0], f"upgrade flattened: {key}/{role}")

    def test_upgrade_still_helps_when_two_other_traits_are_already_maxed(self):
        cases = (
            ("batter", "bat_finisher", ("mental_consistency", "elite_master_blaster"), "6"),
            ("bowler", "bowl_wicket_hunter", ("field_safe_hands", "elite_bowling_wizard"), "W"),
        )
        for role, key, others, outcome in cases:
            with self.subTest(role=role):
                weights = []
                for level in (4, 5):
                    probs = dict(BASE)
                    traits = [_trait(key, level)] + [_trait(other, 5) for other in others]
                    apply_traits(probs, traits if role == "batter" else [],
                                 traits if role == "bowler" else [], self.context(key, role))
                    weights.append(probs[outcome])
                self.assertGreater(weights[1], weights[0])

    def test_an_all_rounder_uses_only_the_trait_for_the_current_role(self):
        both = [_trait("bat_anchor", 3), _trait("bowl_wicket_hunter", 5)]
        for role, valid in (("batter", both[:1]), ("bowler", both[1:])):
            with self.subTest(role=role):
                actual, expected = dict(BASE), dict(BASE)
                apply_traits(actual, both if role == "batter" else [],
                             both if role == "bowler" else [], {})
                apply_traits(expected, valid if role == "batter" else [],
                             valid if role == "bowler" else [], {})
                self.assertEqual(actual, expected)

    def test_an_inactive_high_level_trait_does_not_take_the_first_stack_slot(self):
        expected, actual = dict(BASE), dict(BASE)
        anchor = _trait("bat_anchor", 3)
        apply_traits(expected, [anchor], [], {"over": 10, "total_overs": 20})
        apply_traits(actual, [_trait("bat_finisher", 5), anchor], [],
                     {"over": 10, "total_overs": 20})
        self.assertEqual(actual, expected)

    def test_a_broken_or_unknown_trait_does_not_take_a_stack_slot(self):
        expected, actual = dict(BASE), dict(BASE)
        hunter = _trait("bowl_wicket_hunter", 3)
        apply_traits(expected, [], [hunter], {})
        apply_traits(actual, [], [_trait("unknown", 5), hunter], {})
        self.assertEqual(actual, expected)

    def test_shared_traits_activate_in_both_roles(self):
        for key in ("mental_comeback", "aware_pitch", "special_giantkiller", "elite_goat"):
            for role in ("batter", "bowler"):
                with self.subTest(key=key, role=role), patch(
                        "services.trait_engine.random.random", return_value=0.0):
                    probs = dict(BASE)
                    traits = [_trait(key, 3)]
                    activated = apply_traits(
                        probs, traits if role == "batter" else [],
                        traits if role == "bowler" else [], self.context(key, role))
                    self.assertTrue(activated)
                    self.assertNotEqual(probs, BASE)


class ChaseTraitUpgradeTests(unittest.TestCase):
    def test_chase_controller_respects_batting_and_bowling_upgrades(self):
        from engine.chase_model import situational_shift
        baseline = situational_shift(striker_balls=10)
        for role, key in (("batter", "bat_finisher"), ("bowler", "bowl_death")):
            effects = []
            for level in range(1, 6):
                kwargs = {"striker_traits" if role == "batter" else "bowler_traits":
                          [_trait(key, level)]}
                effects.append(abs(situational_shift(striker_balls=10, **kwargs) - baseline))
            self.assertEqual(effects, sorted(set(effects)))

    def test_chase_controller_cancels_matching_trait_investments(self):
        from engine.chase_model import situational_shift
        baseline = situational_shift(striker_balls=10)
        for level in range(1, 6):
            shifted = situational_shift(
                striker_balls=10, striker_traits=[_trait("bat_finisher", level)],
                bowler_traits=[_trait("bowl_death", level)])
            self.assertAlmostEqual(shifted, baseline)


class MatchTraitBalanceTests(unittest.TestCase):
    RAW = {"Dot": 30.0, "Single": 25.0, "Double": 8.0, "Three": 1.0,
           "Four": 12.0, "Six": 8.0, "Wicket": 6.0, "Extras": 2.0}

    def test_match_adapter_preserves_relative_effect_at_any_weight_scale(self):
        for scale in (0.01, 1.0, 100.0):
            with self.subTest(scale=scale):
                raw = {key: value * scale for key, value in self.RAW.items()}
                original = dict(raw)
                changed = cipl_match._apply_trait_weights(
                    raw, [], [_trait("bowl_wicket_hunter", 3)], {})
                self.assertEqual(raw, original)
                self.assertAlmostEqual(changed["Wicket"], raw["Wicket"] * 1.046875)
                for key in raw.keys() - {"Wicket"}:
                    self.assertAlmostEqual(changed[key], raw[key])

    def test_match_adapter_preserves_the_anchor_counter(self):
        changed = cipl_match._apply_trait_weights(
            self.RAW, [_trait("bat_anchor", 3)],
            [_trait("bowl_wicket_hunter", 3)], {})
        self.assertLess(changed["Wicket"], self.RAW["Wicket"] * 1.01)

    def test_activated_traits_are_still_collected_for_each_side(self):
        collector = {"bat": set(), "bowl": set()}
        cipl_match._apply_trait_weights(
            self.RAW, [_trait("bat_anchor")], [_trait("bowl_wicket_hunter")],
            {}, collector)
        self.assertEqual(collector["bat"], {"✨ bat_anchor Lv.1"})
        self.assertEqual(collector["bowl"], {"✨ bowl_wicket_hunter Lv.1"})

    def test_an_identical_wrong_role_label_is_not_attributed_to_the_batter(self):
        collector = {"bat": set(), "bowl": set()}
        hunter = [_trait("bowl_wicket_hunter")]
        cipl_match._apply_trait_weights(self.RAW, hunter, hunter, {}, collector)
        self.assertEqual(collector["bat"], set())
        self.assertEqual(collector["bowl"], {"✨ bowl_wicket_hunter Lv.1"})


if __name__ == "__main__":
    unittest.main()

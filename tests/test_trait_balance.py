"""Batting and bowling have equal, bounded influence on outcome weights."""

import unittest
from unittest.mock import patch

from services import cipl_match
from services.probability_engine import BASE
from services.trait_engine import TRAIT_HANDLERS, apply_traits


def _trait(key, level=1):
    return {"effect_key": key, "level": level, "display_name": key}


class TraitBalanceTests(unittest.TestCase):
    def test_rare_wickets_and_boundaries_get_the_same_relative_boost(self):
        batting, bowling = dict(BASE), dict(BASE)
        apply_traits(batting, [_trait("bat_finisher")], [],
                     {"over": 20, "total_overs": 20})
        apply_traits(bowling, [], [_trait("bowl_wicket_hunter")], {})
        # Lv.1 is 5%, and each role owns half of that influence. Adding five
        # percentage points to these differently sized buckets was the bias.
        self.assertAlmostEqual(batting["6"], BASE["6"] * 1.025)
        self.assertAlmostEqual(bowling["W"], BASE["W"] * 1.025)

    def test_anchor_and_wicket_hunter_cancel_before_probability_updates(self):
        for level in (1, 2, 3, 4):
            with self.subTest(level=level):
                probs = dict(BASE)
                apply_traits(probs, [_trait("bat_anchor", level)],
                             [_trait("bowl_wicket_hunter", level)], {})
                self.assertAlmostEqual(probs["W"], BASE["W"])
                self.assertLess(probs["6"], BASE["6"])

    def test_equal_opposing_stacks_cancel_at_every_level(self):
        def mirror(ctx, strength, role):
            sign = -1 if role == "batter" else 1
            return [("W", sign * strength), ("6", -sign * strength / 2)]

        with patch.dict(TRAIT_HANDLERS, {"_mirror": mirror}):
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
        self.assertAlmostEqual(alone["W"], BASE["W"] * 1.025)

    def test_boosts_and_reductions_share_the_same_cap_for_both_roles(self):
        def changes(ctx, strength, role):
            sign = ctx["sign"]
            return [("W", sign * strength), ("6", sign * strength),
                    ("4", sign * strength)]

        with patch.dict(TRAIT_HANDLERS, {"_changes": changes}):
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
                        self.assertAlmostEqual(relative_change, 0.125)

    def test_mixed_sign_traits_cannot_bypass_the_cap(self):
        probs = dict(BASE)
        apply_traits(probs, [], [_trait("bowl_economy", 5),
                                _trait("elite_unplayable", 5),
                                _trait("bowl_wicket_hunter", 5)],
                     {"boundary_streak": 2})
        relative_change = sum(abs(probs[key] / BASE[key] - 1.0)
                              for key in BASE)
        self.assertAlmostEqual(relative_change, 0.125)
        self.assertLess(probs["6"], BASE["6"])
        self.assertGreater(probs["W"], BASE["W"])

    def test_max_level_wicket_hunter_stays_close_to_the_base_wicket_chance(self):
        probs = dict(BASE)
        apply_traits(probs, [], [_trait("bowl_wicket_hunter", 5)], {})
        self.assertAlmostEqual(probs["W"], BASE["W"] * 1.125)
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
                self.assertAlmostEqual(changed["Wicket"], raw["Wicket"] * 1.075)
                for key in raw.keys() - {"Wicket"}:
                    self.assertAlmostEqual(changed[key], raw[key])

    def test_match_adapter_cancels_opposing_wicket_effects(self):
        changed = cipl_match._apply_trait_weights(
            self.RAW, [_trait("bat_anchor", 3)],
            [_trait("bowl_wicket_hunter", 3)], {})
        self.assertAlmostEqual(changed["Wicket"], self.RAW["Wicket"])

    def test_activated_traits_are_still_collected_for_each_side(self):
        collector = {"bat": set(), "bowl": set()}
        cipl_match._apply_trait_weights(
            self.RAW, [_trait("bat_anchor")], [_trait("bowl_wicket_hunter")],
            {}, collector)
        self.assertEqual(collector["bat"], {"✨ bat_anchor Lv.1"})
        self.assertEqual(collector["bowl"], {"✨ bowl_wicket_hunter Lv.1"})


if __name__ == "__main__":
    unittest.main()

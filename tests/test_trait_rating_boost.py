"""Trait Rating Boost — what equipped traits add to a team's Overall Rating.

What is pinned here, and why each one is a way the feature could quietly go
wrong:

  1. The ladder doubles (0.2 / 0.4 / 0.8 / 1.6 / 3.2). Flatten it and levelling
     a trait stops competing with buying another one, which is the whole
     decision the trait economy exists to create.
  2. Stacking on one card diminishes and then caps, using the ball engine's own
     weights — otherwise three Lv.5 traits on one card outrun the gap between a
     good XI and a great one.
  3. The team boost is the AVERAGE of eleven cards, not their sum. Sum it and
     one Lv.5 trait moves a Team Overall by 3.2 instead of 0.3.
  4. Ordering is by level, not by row order: two captains with identical squads
     must read identical Team Overalls whatever order the database hands the
     traits back in.
  5. The boost does NOT enter the anti stat-farming gap. Traits are bought with
     gems; a captain who levelled theirs must never lose career stats for it.
  6. Nothing raises on junk. A card screen is the worst place to take a match
     setup down, so a missing, null or nonsense level counts as Lv.1.
"""

import unittest

from config import (
    TRAIT_RATING_BONUS, TRAIT_RATING_BONUS_MAX_PER_PLAYER,
    TRAIT_RATING_BONUS_MAX_PER_TEAM, TRAIT_STACK_WEIGHTS,
)
from services import trait_rating_service as trs
from services.player_stats_service import (
    STATS_FAIRNESS_OVR_GAP, is_stat_farming_mismatch, team_overall,
    team_overall_card, team_overall_effective,
)


def _xi(ratings, traits_by_index=None):
    """An XI of engine-shaped dicts; ``traits_by_index`` maps slot → levels."""
    traits_by_index = traits_by_index or {}
    return [{"name": f"P{i}", "rating": r,
             "traits": [{"effect_key": f"t{n}", "level": lv, "display_name": "T"}
                        for n, lv in enumerate(traits_by_index.get(i, []))]}
            for i, r in enumerate(ratings)]


class LadderTests(unittest.TestCase):
    def test_the_ladder_is_the_published_one(self):
        self.assertEqual(TRAIT_RATING_BONUS,
                         {1: 0.2, 2: 0.4, 3: 0.8, 4: 1.6, 5: 3.2})

    def test_each_level_is_worth_double_the_one_below(self):
        for level in range(2, 6):
            self.assertAlmostEqual(trs.level_bonus(level),
                                   trs.level_bonus(level - 1) * 2, places=6)

    def test_one_max_level_trait_beats_four_of_the_cheapest(self):
        """The reason the ladder doubles: levelling must beat hoarding."""
        self.assertGreater(trs.level_bonus(5), trs.level_bonus(1) * 4)

    def test_an_unknown_level_reads_as_the_nearest_real_one(self):
        self.assertEqual(trs.level_bonus(0), trs.level_bonus(1))
        self.assertEqual(trs.level_bonus(99), trs.level_bonus(5))

    def test_junk_never_raises(self):
        for junk in (None, "", "x", object(), float("nan")):
            self.assertIsInstance(trs.level_bonus(junk), float)


class PlayerStackTests(unittest.TestCase):
    def test_no_traits_is_no_boost(self):
        self.assertEqual(trs.player_bonus([]), 0.0)
        self.assertEqual(trs.player_bonus(None), 0.0)

    def test_a_single_trait_is_its_full_ladder_value(self):
        self.assertAlmostEqual(trs.player_bonus([{"level": 4}]), 1.6, places=2)

    def test_stacking_uses_the_ball_engine_weights(self):
        want = round(3.2 * TRAIT_STACK_WEIGHTS[0] + 0.4 * TRAIT_STACK_WEIGHTS[1],
                     2)
        self.assertAlmostEqual(
            trs.player_bonus([{"level": 5}, {"level": 2}]), want, places=2)

    def test_the_highest_level_always_takes_the_full_slot(self):
        """Row order must not decide what a squad is worth."""
        low_first = trs.player_bonus([{"level": 1}, {"level": 5}])
        high_first = trs.player_bonus([{"level": 5}, {"level": 1}])
        self.assertEqual(low_first, high_first)
        self.assertGreater(low_first, trs.player_bonus([{"level": 5}]))

    def test_one_card_is_capped(self):
        maxed = trs.player_bonus([{"level": 5}] * 3)
        self.assertEqual(maxed, TRAIT_RATING_BONUS_MAX_PER_PLAYER)

    def test_bare_levels_are_accepted_too(self):
        self.assertEqual(trs.player_bonus([5]), trs.player_bonus([{"level": 5}]))


class TeamBoostTests(unittest.TestCase):
    def test_an_untraited_xi_is_unchanged(self):
        xi = _xi([80] * 11)
        self.assertEqual(trs.team_bonus(xi), 0.0)
        self.assertEqual(trs.team_effective_overall(xi), 80.0)

    def test_the_team_boost_is_an_average_not_a_sum(self):
        """One Lv.5 trait lifts eleven cards by 3.2/11, not by 3.2."""
        xi = _xi([80] * 11, {0: [5]})
        self.assertAlmostEqual(trs.team_bonus(xi), round(3.2 / 11, 2), places=2)

    def test_kitting_the_whole_xi_is_what_moves_the_number(self):
        one = trs.team_bonus(_xi([80] * 11, {0: [5]}))
        all_eleven = trs.team_bonus(_xi([80] * 11, {i: [5] for i in range(11)}))
        self.assertAlmostEqual(all_eleven, 3.2, places=2)
        self.assertGreater(all_eleven, one * 10)

    def test_the_team_boost_is_capped(self):
        xi = _xi([80] * 11, {i: [5, 5, 5] for i in range(11)})
        self.assertEqual(trs.team_bonus(xi), TRAIT_RATING_BONUS_MAX_PER_TEAM)

    def test_the_team_cap_stays_under_the_stat_farming_gap(self):
        """No amount of trait investment may on its own look like farming."""
        self.assertLess(TRAIT_RATING_BONUS_MAX_PER_TEAM, STATS_FAIRNESS_OVR_GAP)

    def test_the_card_carries_base_bonus_and_effective(self):
        card = trs.team_rating_card(_xi([80] * 11, {i: [5] for i in range(11)}))
        self.assertEqual(card["base"], 80)
        self.assertAlmostEqual(card["bonus"], 3.2, places=2)
        self.assertAlmostEqual(card["effective"], 83.2, places=2)

    def test_an_empty_xi_is_zero_rather_than_a_crash(self):
        self.assertEqual(trs.team_bonus([]), 0.0)
        self.assertEqual(trs.team_base_overall(None), 0)

    def test_the_printed_card_rating_wins_over_an_adjusted_engine_rating(self):
        """Some modes rebalance ``rating`` before the first ball; the boost is
        always quoted against the number the captain actually sees."""
        xi = [{"name": "P", "rating": 70, "card_rating": 84, "traits": []}]
        self.assertEqual(trs.team_base_overall(xi), 84)


class AnnotationTests(unittest.TestCase):
    def test_every_entry_gets_the_keys_even_with_no_traits(self):
        xi = _xi([80, 80], {0: [3]})
        trs.annotate_xi(xi)
        self.assertAlmostEqual(xi[0]["trait_bonus"], 0.8, places=2)
        self.assertAlmostEqual(xi[0]["effective_rating"], 80.8, places=2)
        self.assertEqual(xi[1]["trait_bonus"], 0.0)
        self.assertEqual(xi[1]["effective_rating"], 80.0)

    def test_top_contributors_names_the_cards_carrying_the_boost(self):
        xi = _xi([80] * 4, {0: [1], 2: [5]})
        self.assertEqual(trs.top_contributors(xi, limit=2),
                         [("P2", 3.2), ("P0", 0.2)])


class FairnessGateTests(unittest.TestCase):
    """The gate that decides whether career stats count stays on card ratings."""

    def test_the_boost_does_not_widen_the_gap(self):
        strong = _xi([84] * 11, {i: [5] for i in range(11)})
        weak = _xi([76] * 11)
        # 8 apart on the card, 11.2 apart once boosted — the gate must read 8.
        self.assertEqual(abs(team_overall(strong) - team_overall(weak)), 8)
        self.assertGreater(
            abs(team_overall_effective(strong) - team_overall_effective(weak)),
            STATS_FAIRNESS_OVR_GAP)
        self.assertFalse(is_stat_farming_mismatch(strong, weak),
                         "traits must never cost a captain their career stats")

    def test_a_real_mismatch_is_still_caught(self):
        self.assertTrue(is_stat_farming_mismatch(_xi([88] * 11), _xi([60] * 11)))

    def test_the_operator_can_opt_the_boost_into_the_gap(self):
        import config
        original = config.TRAIT_RATING_BONUS_COUNTS_FOR_FAIRNESS
        config.TRAIT_RATING_BONUS_COUNTS_FOR_FAIRNESS = True
        # The service reads the constant at import, so patch it there too —
        # which is exactly what the switch has to reach to mean anything.
        trs_original = trs.TRAIT_RATING_BONUS_COUNTS_FOR_FAIRNESS
        trs.TRAIT_RATING_BONUS_COUNTS_FOR_FAIRNESS = True
        try:
            strong = _xi([84] * 11, {i: [5] for i in range(11)})
            weak = _xi([76] * 11)
            self.assertTrue(trs.counts_for_fairness())
            self.assertTrue(is_stat_farming_mismatch(strong, weak))
        finally:
            config.TRAIT_RATING_BONUS_COUNTS_FOR_FAIRNESS = original
            trs.TRAIT_RATING_BONUS_COUNTS_FOR_FAIRNESS = trs_original

    def test_team_overall_card_is_the_shared_shape(self):
        card = team_overall_card(_xi([80] * 11, {0: [5]}))
        self.assertEqual(set(card), {"base", "bonus", "effective"})


class RenderingTests(unittest.TestCase):
    def test_an_unboosted_card_reads_exactly_as_it_always_did(self):
        self.assertEqual(trs.format_player_rating(84, 0.0), "84")

    def test_a_boosted_card_shows_what_the_traits_are_worth(self):
        self.assertEqual(trs.format_player_rating(84, 1.6), "84 ⚡+1.6")

    def test_the_boost_line_is_omitted_when_neither_side_has_traits(self):
        flat = {"base": 80, "bonus": 0.0, "effective": 80.0}
        self.assertIsNone(trs.format_team_line("Host", flat, "Guest", flat))

    def test_the_boost_line_names_both_sides(self):
        host = {"base": 80, "bonus": 1.2, "effective": 81.2}
        guest = {"base": 82, "bonus": 0.0, "effective": 82.0}
        line = trs.format_team_line("Host", host, "Guest", guest)
        self.assertIn("Host 80 → <b>81.2</b> (+1.2)", line)
        self.assertIn("Guest 82 → <b>82.0</b> (+0.0)", line)

    def test_a_bonus_is_always_signed(self):
        self.assertEqual(trs.format_bonus(1.2), "+1.2")
        self.assertEqual(trs.format_bonus(0), "+0.0")


if __name__ == "__main__":
    unittest.main()

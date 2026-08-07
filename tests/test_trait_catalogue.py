"""The trait catalogue, the rarity economy, and every trait's engine handler.

Three things are pinned here, and each of them is a way a trait can silently do
nothing:

  1. Every catalogue entry routes to a real handler, and every handler is in the
     catalogue. A trait whose ``effect_key`` the engine doesn't know is bought,
     equipped, upgraded — and never fires.
  2. Every handler survives an empty context. Several ball loops feed the trait
     engine and they know different amounts about the match; a handler that
     raises on a missing key takes its whole side's traits down with it.
  3. Rarity prices resale below the floor at EVERY tier. The Common tier has had
     that guarantee since the resale rewrite (tests/test_trait_market.py); this
     is the same guarantee re-checked now that a trait can cost eight times as
     much.

Plus one behavioural test per new trait — the condition it claims to key off
actually turns it on, and its absence turns it off. Those are the tests that
would have caught "Spin Basher fires against seamers".
"""

import unittest

from config import (
    TRAIT_RARITIES, TRAIT_RARITY_ORDER, TRAIT_MARKET_BUY_COST,
    TRAIT_MAX_ELITE_PER_PLAYER, trait_buy_value, trait_floor_cost,
    trait_list_price, trait_rarity_key, trait_rarity_mult, trait_sell_value,
    trait_trade_fee,
)
from services.trait_engine import TRAIT_HANDLERS, apply_traits
from services.trait_service import (
    TRAIT_CATEGORY_ORDER, TRAIT_DEFINITIONS, trait_meta, trait_price_of,
    trait_rarity_of,
)
from utils.message_chunks import chunk_blocks

LEVELS = (1, 2, 3, 4, 5)


class CatalogueIntegrityTests(unittest.TestCase):
    def test_every_trait_routes_to_an_engine_handler(self):
        missing = [t["name"] for t in TRAIT_DEFINITIONS
                   if t["effect_key"] not in TRAIT_HANDLERS]
        self.assertEqual(missing, [],
                         "these traits are buyable but do nothing in a match")

    def test_every_handler_is_a_trait_somebody_can_own(self):
        keys = {t["effect_key"] for t in TRAIT_DEFINITIONS}
        orphans = sorted(set(TRAIT_HANDLERS) - keys)
        self.assertEqual(orphans, [],
                         "engine handlers with no catalogue entry to reach them")

    def test_names_and_effect_keys_are_unique(self):
        names = [t["name"] for t in TRAIT_DEFINITIONS]
        keys = [t["effect_key"] for t in TRAIT_DEFINITIONS]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(keys), len(set(keys)))
        # ``Trait.name`` is a UNIQUE column, so a duplicate name doesn't just
        # look untidy — it makes the seed drop a trait.
        for name in names:
            self.assertLessEqual(len(name), 50, name)

    def test_every_trait_is_complete_and_well_formed(self):
        for t in TRAIT_DEFINITIONS:
            with self.subTest(t["name"]):
                self.assertIn(t["category"], TRAIT_CATEGORY_ORDER)
                self.assertIn(t["rarity"], TRAIT_RARITIES)
                self.assertTrue(t["description"].strip())
                self.assertLessEqual(len(t["description"]), 300)
                self.assertTrue(t["emoji"].strip())

    def test_the_requested_traits_are_all_present(self):
        """The catalogue this change was asked for, named trait by trait."""
        wanted = [
            "bat_spin_basher", "bat_pace_destroyer", "bat_late_bloomer",
            "bat_power_surge", "bowl_spell_builder", "bowl_partner_breaker",
            "bowl_tail_hunter", "elite_master_blaster", "elite_run_machine",
            "elite_ice_finisher", "elite_bowling_wizard", "elite_magic_spell",
            "elite_unplayable", "elite_golden_arm", "elite_big_fish",
            "elite_matchup", "elite_goat", "mental_confidence",
            "mental_comeback", "aware_pitch", "special_giantkiller",
            "aware_rotation", "aware_gap",
        ]
        for key in wanted:
            with self.subTest(key):
                self.assertIsNotNone(trait_meta(key))

    def test_every_elite_trait_carries_the_elite_rarity(self):
        for t in TRAIT_DEFINITIONS:
            if t["category"] == "Elite":
                self.assertEqual(t["rarity"], "elite", t["name"])
            if t["rarity"] == "elite":
                self.assertEqual(t["category"], "Elite", t["name"])

    def test_elite_is_the_rarest_tier(self):
        weights = {k: v["weight"] for k, v in TRAIT_RARITIES.items()}
        self.assertEqual(min(weights, key=weights.get), "elite")
        self.assertEqual(max(weights, key=weights.get), "common")


class HandlerRobustnessTests(unittest.TestCase):
    """A handler must never raise, whatever the loop failed to tell it."""

    CONTEXTS = (
        {},
        {"over": 1, "total_overs": 20},
        {"over": None, "total_overs": None, "rrr": None,
         "bat_balls_faced": None, "bat_rating": None, "bowl_rating": None,
         "is_spin": None, "bat_hand": None, "bowl_hand": None},
        {"over": "x", "total_overs": "y", "bat_balls_faced": "z"},
    )

    def test_no_handler_raises_on_a_thin_context(self):
        for key, handler in TRAIT_HANDLERS.items():
            for ctx in self.CONTEXTS:
                for role in ("batter", "bowler"):
                    with self.subTest(trait=key, role=role, ctx=ctx):
                        deltas = handler(dict(ctx), 10.0, role)
                        self.assertIsInstance(deltas, list)
                        for entry in deltas:
                            self.assertEqual(len(entry), 2)

    def test_apply_traits_survives_a_broken_handler(self):
        """One bad trait must not cost the player their other traits."""
        def boom(ctx, x, role):
            raise ValueError("bad trait")

        TRAIT_HANDLERS["_test_boom"] = boom
        try:
            probs = {"6": 10.0, "4": 10.0, "W": 5.0, "dot": 30.0}
            activated = apply_traits(
                probs,
                [{"effect_key": "_test_boom", "level": 3, "display_name": "Boom"},
                 {"effect_key": "bowl_wicket_hunter", "level": 3,
                  "display_name": "Wicket Hunter"}],
                [], {"over": 5, "total_overs": 20})
            self.assertEqual(activated, ["Wicket Hunter Lv.3"])
        finally:
            TRAIT_HANDLERS.pop("_test_boom")

    def test_the_strongest_trait_takes_the_full_strength_stack_slot(self):
        """Stacking is level-ordered, not list-ordered.

        Two identical traits at different levels must produce the same total
        whichever order they arrive in, or a player's effective squad depends on
        the order the database happened to return their rows.
        """
        def totals(traits):
            probs = {"6": 10.0, "4": 10.0, "W": 5.0, "dot": 30.0}
            apply_traits(probs, traits, [], {"over": 5, "total_overs": 20})
            return probs

        low = {"effect_key": "bat_power_hitter", "level": 1, "display_name": "A"}
        high = {"effect_key": "bat_anchor", "level": 5, "display_name": "B"}
        self.assertEqual(totals([low, high]), totals([high, low]))


class RarityPricingTests(unittest.TestCase):
    def test_common_prices_are_exactly_what_they_were(self):
        """Rarity must not have moved the existing economy by a single gem."""
        self.assertEqual([trait_buy_value(l) for l in LEVELS],
                         [150, 350, 750, 1550, 3050])
        self.assertEqual([trait_sell_value(l) for l in LEVELS],
                         [119, 319, 719, 1519, 3019])
        self.assertEqual([trait_trade_fee(l) for l in LEVELS],
                         [15, 16, 18, 22, 29])
        for level in LEVELS:
            self.assertEqual(trait_buy_value(level), trait_buy_value(level, "common"))
            self.assertEqual(trait_sell_value(level), trait_sell_value(level, "common"))
            self.assertEqual(trait_trade_fee(level), trait_trade_fee(level, "common"))

    def test_selling_is_a_loss_at_every_rarity(self):
        """The rule the whole resale design exists to keep: no gem printer."""
        for rarity in TRAIT_RARITIES:
            for level in LEVELS:
                with self.subTest(rarity=rarity, level=level):
                    self.assertLess(trait_sell_value(level, rarity),
                                    trait_floor_cost(level, rarity))
                    self.assertLess(trait_sell_value(level, rarity),
                                    trait_buy_value(level, rarity))

    def test_a_rarer_trait_costs_and_returns_more(self):
        for level in LEVELS:
            prices = [trait_buy_value(level, r) for r in TRAIT_RARITY_ORDER]
            self.assertEqual(prices, sorted(prices))
            self.assertEqual(len(set(prices)), len(prices), level)
            sells = [trait_sell_value(level, r) for r in TRAIT_RARITY_ORDER]
            self.assertEqual(sells, sorted(sells))

    def test_the_lv1_price_is_the_rarity_multiplier(self):
        for rarity, meta in TRAIT_RARITIES.items():
            expected = TRAIT_MARKET_BUY_COST * meta["price_mult"]
            self.assertEqual(trait_list_price(rarity), expected)
            self.assertEqual(trait_buy_value(1, rarity), expected)

    def test_upgrades_cost_the_same_whatever_the_rarity(self):
        """Rarity gates acquisition, not use — the levelling bill is flat."""
        for level in LEVELS[1:]:
            steps = {trait_buy_value(level, r) - trait_buy_value(level - 1, r)
                     for r in TRAIT_RARITIES}
            self.assertEqual(len(steps), 1, level)

    def test_a_rarer_swap_costs_more(self):
        fees = [trait_trade_fee(3, r) for r in TRAIT_RARITY_ORDER]
        self.assertEqual(fees, sorted(fees))
        self.assertEqual(len(set(fees)), len(fees))

    def test_unknown_rarities_read_as_common(self):
        for junk in (None, "", "legendary", "COMMON ", 7):
            self.assertEqual(trait_rarity_key(junk) in TRAIT_RARITIES, True)
        self.assertEqual(trait_rarity_key("nonsense"), "common")
        self.assertEqual(trait_rarity_mult(None), 1)
        self.assertEqual(trait_buy_value(2, "nonsense"), trait_buy_value(2))


class _Row:
    """A stand-in for a ``Trait`` row, including ones missing the new columns."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class TraitRowPricingTests(unittest.TestCase):
    def test_a_row_written_before_the_rarity_column_still_prices_correctly(self):
        """Rarity falls back to the catalogue, keyed on the effect key."""
        row = _Row(effect_key="elite_goat", rarity=None, base_price=None)
        self.assertEqual(trait_rarity_of(row), "elite")
        self.assertEqual(trait_price_of(row), trait_list_price("elite"))

    def test_the_admin_price_override_wins(self):
        row = _Row(effect_key="elite_goat", rarity="elite", base_price=99)
        self.assertEqual(trait_price_of(row), 99)

    def test_a_blank_override_falls_back_to_the_rarity(self):
        for blank in (None, 0):
            row = _Row(effect_key="bat_anchor", rarity="common", base_price=blank)
            self.assertEqual(trait_price_of(row), trait_list_price("common"))

    def test_an_unknown_trait_reads_as_common(self):
        row = _Row(effect_key="not_a_trait", rarity=None, base_price=None)
        self.assertEqual(trait_rarity_of(row), "common")


def _fire(effect_key, ctx, role="batter", level=3):
    """Run one trait's handler and return its deltas as a dict."""
    x = 15.0  # level 3
    return dict(TRAIT_HANDLERS[effect_key](ctx, x, role))


class NewTraitBehaviourTests(unittest.TestCase):
    """Each new trait fires on its condition — and only on its condition."""

    BASE = {"over": 8, "total_overs": 20, "innings": 1}

    def ctx(self, **kw):
        out = dict(self.BASE)
        out.update(kw)
        return out

    def test_spin_basher_only_plays_spin(self):
        self.assertTrue(_fire("bat_spin_basher", self.ctx(is_spin=True)))
        self.assertFalse(_fire("bat_spin_basher", self.ctx(is_spin=False)))
        self.assertFalse(_fire("bat_spin_basher", self.ctx()))

    def test_pace_destroyer_only_plays_pace(self):
        self.assertTrue(_fire("bat_pace_destroyer", self.ctx(is_spin=False)))
        self.assertFalse(_fire("bat_pace_destroyer", self.ctx(is_spin=True)))
        # Unknown bowler type stays silent rather than firing against everyone.
        self.assertFalse(_fire("bat_pace_destroyer", self.ctx()))

    def test_late_bloomer_waits_for_twenty_balls(self):
        self.assertFalse(_fire("bat_late_bloomer", self.ctx(bat_balls_faced=19)))
        self.assertTrue(_fire("bat_late_bloomer", self.ctx(bat_balls_faced=20)))

    def test_power_surge_needs_consecutive_boundaries(self):
        self.assertFalse(_fire("bat_power_surge", self.ctx(boundary_streak=1)))
        two = _fire("bat_power_surge", self.ctx(boundary_streak=2))
        three = _fire("bat_power_surge", self.ctx(boundary_streak=3))
        self.assertTrue(two)
        self.assertGreater(three["6"], two["6"])

    def test_spell_builder_grows_through_the_spell(self):
        self.assertFalse(_fire("bowl_spell_builder", self.ctx(bowler_balls=5),
                               role="bowler"))
        one = _fire("bowl_spell_builder", self.ctx(bowler_balls=6), role="bowler")
        three = _fire("bowl_spell_builder", self.ctx(bowler_balls=18), role="bowler")
        self.assertGreater(three["W"], one["W"])

    def test_partnership_breaker_waits_for_a_fifty_stand(self):
        self.assertFalse(_fire("bowl_partner_breaker",
                               self.ctx(partnership_runs=49), role="bowler"))
        self.assertTrue(_fire("bowl_partner_breaker",
                              self.ctx(partnership_runs=50), role="bowler"))

    def test_tail_end_hunter_only_hunts_the_tail(self):
        self.assertFalse(_fire("bowl_tail_hunter", self.ctx(bat_position=7),
                               role="bowler"))
        self.assertTrue(_fire("bowl_tail_hunter", self.ctx(bat_position=8),
                              role="bowler"))

    def test_big_fish_hunter_only_hunts_the_top_three(self):
        self.assertTrue(_fire("elite_big_fish", self.ctx(bat_position=3),
                              role="bowler"))
        self.assertFalse(_fire("elite_big_fish", self.ctx(bat_position=4),
                               role="bowler"))
        self.assertFalse(_fire("elite_big_fish", self.ctx(), role="bowler"))

    def test_golden_arm_strikes_in_the_first_over_of_the_spell(self):
        self.assertTrue(_fire("elite_golden_arm", self.ctx(bowler_balls=0),
                              role="bowler"))
        self.assertFalse(_fire("elite_golden_arm", self.ctx(bowler_balls=7),
                               role="bowler"))

    def test_unplayable_shuts_down_a_boundary_run(self):
        hit = _fire("elite_unplayable", self.ctx(boundary_streak=1), role="bowler")
        self.assertLess(hit["4"], 0)
        self.assertLess(hit["6"], 0)
        self.assertFalse(_fire("elite_unplayable", self.ctx(boundary_streak=0),
                               role="bowler"))

    def test_nightmare_matchup_needs_opposite_hands(self):
        self.assertTrue(_fire("elite_matchup",
                              self.ctx(bat_hand="Left", bowl_hand="Right"),
                              role="bowler"))
        self.assertFalse(_fire("elite_matchup",
                               self.ctx(bat_hand="Right", bowl_hand="Right"),
                               role="bowler"))
        self.assertFalse(_fire("elite_matchup", self.ctx(), role="bowler"))

    def test_ice_finisher_needs_the_death_overs_of_a_chase(self):
        chase = self.ctx(over=19, total_overs=20, rrr=9.5)
        self.assertTrue(_fire("elite_ice_finisher", chase))
        # Death overs but no chase on: the elite version stays out.
        self.assertFalse(_fire("elite_ice_finisher",
                               self.ctx(over=19, total_overs=20, rrr=0)))
        self.assertFalse(_fire("elite_ice_finisher",
                               self.ctx(over=5, total_overs=20, rrr=9.5)))

    def test_giant_killer_needs_a_stronger_opponent(self):
        self.assertTrue(_fire("special_giantkiller",
                              self.ctx(bat_rating=70, bowl_rating=90)))
        self.assertFalse(_fire("special_giantkiller",
                               self.ctx(bat_rating=90, bowl_rating=70)))
        # Same trait from the bowler's end reads the gap the other way round.
        self.assertTrue(_fire("special_giantkiller",
                              self.ctx(bat_rating=90, bowl_rating=70),
                              role="bowler"))

    def test_comeback_king_answers_an_expensive_spell(self):
        beaten = self.ctx(bowler_balls=12, bowler_runs=20)
        quiet = self.ctx(bowler_balls=12, bowler_runs=8)
        self.assertTrue(_fire("mental_comeback", beaten, role="bowler"))
        self.assertFalse(_fire("mental_comeback", quiet, role="bowler"))

    def test_confidence_grows_with_boundaries_hit(self):
        self.assertFalse(_fire("mental_confidence", self.ctx(bat_boundaries=0)))
        one = _fire("mental_confidence", self.ctx(bat_boundaries=1))
        four = _fire("mental_confidence", self.ctx(bat_boundaries=4))
        self.assertGreater(four["4"], one["4"])

    def test_pitch_reader_helps_early_and_then_stops(self):
        self.assertTrue(_fire("aware_pitch", self.ctx(bat_balls_faced=5)))
        self.assertFalse(_fire("aware_pitch", self.ctx(bat_balls_faced=40)))
        self.assertTrue(_fire("aware_pitch", self.ctx(bowler_balls=6),
                              role="bowler"))

    def test_strike_rotator_turns_dots_into_singles(self):
        deltas = _fire("aware_rotation", self.ctx())
        self.assertGreater(deltas["1"], 0)
        self.assertLess(deltas["dot"], 0)

    def test_gap_finder_finds_twos_and_fours(self):
        deltas = _fire("aware_gap", self.ctx())
        self.assertGreater(deltas["2"], 0)
        self.assertGreater(deltas["4"], 0)

    def test_master_blaster_works_in_all_three_phases(self):
        for over in (1, 10, 19):
            with self.subTest(over=over):
                self.assertTrue(_fire("elite_master_blaster",
                                      self.ctx(over=over, total_overs=20)))

    def test_bowling_wizard_works_in_all_three_phases(self):
        for over in (1, 10, 19):
            with self.subTest(over=over):
                self.assertTrue(_fire("elite_bowling_wizard",
                                      self.ctx(over=over, total_overs=20),
                                      role="bowler"))

    def test_magic_spell_is_the_same_answer_all_over_long(self):
        """An 'exceptional over' has to be an over, not scattered balls."""
        ctx = self.ctx(over=7, innings=1, bowler_id=42)
        first = _fire("elite_magic_spell", ctx, role="bowler")
        for _ in range(5):
            self.assertEqual(_fire("elite_magic_spell", dict(ctx), role="bowler"),
                             first)

    def test_magic_spell_is_rare_across_a_match(self):
        fired = sum(
            1 for over in range(1, 200)
            if _fire("elite_magic_spell",
                     self.ctx(over=over, bowler_id=7), role="bowler"))
        self.assertLess(fired, 199 * 0.3, "an 'occasional' over shouldn't be common")

    def test_goat_instinct_only_fires_under_pressure(self):
        calm = self.ctx(rrr=5, balls_left=60, wickets=1)
        for _ in range(30):
            self.assertFalse(_fire("elite_goat", dict(calm)))
        pressure = self.ctx(rrr=14, balls_left=6, wickets=8)
        self.assertTrue(any(_fire("elite_goat", dict(pressure))
                            for _ in range(60)))

    def test_middle_over_squeeze_is_neither_powerplay_nor_death(self):
        self.assertFalse(_fire("bowl_middle_squeeze",
                               self.ctx(over=2, total_overs=20), role="bowler"))
        self.assertTrue(_fire("bowl_middle_squeeze",
                              self.ctx(over=10, total_overs=20), role="bowler"))
        self.assertFalse(_fire("bowl_middle_squeeze",
                               self.ctx(over=19, total_overs=20), role="bowler"))

    def test_pinch_hitter_only_bats_in_the_powerplay(self):
        self.assertTrue(_fire("bat_pinch_hitter", self.ctx(over=2)))
        self.assertFalse(_fire("bat_pinch_hitter", self.ctx(over=9)))

    def test_ice_veins_calms_a_high_required_rate(self):
        self.assertFalse(_fire("mental_ice", self.ctx(rrr=6)))
        self.assertLess(_fire("mental_ice", self.ctx(rrr=12))["W"], 0)


class EliteStackingRuleTests(unittest.TestCase):
    def test_only_one_elite_trait_per_player(self):
        self.assertEqual(TRAIT_MAX_ELITE_PER_PLAYER, 1)

    def test_the_elite_gate_reads_the_rarity_not_the_name(self):
        from services.trait_service import _elite_block
        elite = _Row(effect_key="elite_goat", rarity="elite", name="GOAT Instinct")
        common = _Row(effect_key="bat_anchor", rarity="common", name="Anchor")
        self.assertIsNone(_elite_block(common, [elite]))
        self.assertIsNone(_elite_block(elite, [common]))
        self.assertIsNotNone(_elite_block(elite, [elite]))


class TraitListMessageTests(unittest.TestCase):
    """/traitlist must fit inside Telegram, and not tear its own HTML doing it.

    Exercises the packing helper directly (``utils.message_chunks``) rather than
    the handler, so this needs no database and no Telegram — the handler's only
    job is to hand it the blocks.
    """

    def _blocks(self):
        """The same blocks the handler builds, rendered without a database."""
        by_category = {}
        for t in TRAIT_DEFINITIONS:
            by_category.setdefault(t["category"], []).append(t)
        blocks = []
        for category in TRAIT_CATEGORY_ORDER:
            lines = [f"<b>{category.upper()}</b>"]
            for t in by_category.get(category, []):
                lines.append(
                    f"{TRAIT_RARITIES[t['rarity']]['emoji']} {t['emoji']} "
                    f"<b>{t['name']}</b> — {trait_buy_value(1, t['rarity']):,} gems\n"
                    f"   <i>{t['description']}</i>")
            blocks.append("<blockquote expandable>" + "\n".join(lines)
                          + "</blockquote>")
        return blocks

    def test_the_whole_catalogue_needs_splitting_and_gets_it(self):
        chunks = chunk_blocks(self._blocks(), "HEADER", "FOOTER")
        self.assertGreater(len("".join(chunks)), 4096,
                           "if the catalogue now fits in one message, this test "
                           "is no longer testing anything")
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 4096)

    def test_every_blockquote_is_closed_in_the_message_that_opens_it(self):
        for chunk in chunk_blocks(self._blocks(), "HEADER", "FOOTER"):
            self.assertEqual(chunk.count("<blockquote expandable>"),
                             chunk.count("</blockquote>"))

    def test_the_header_and_footer_appear_exactly_once(self):
        chunks = chunk_blocks(self._blocks(), "HEADER", "FOOTER")
        joined = "\n".join(chunks)
        self.assertEqual(joined.count("HEADER"), 1)
        self.assertEqual(joined.count("FOOTER"), 1)
        self.assertTrue(chunks[0].startswith("HEADER"))
        self.assertTrue(chunks[-1].endswith("FOOTER"))

    def test_no_trait_is_lost_in_the_split(self):
        joined = "\n".join(chunk_blocks(self._blocks(), "HEADER", "FOOTER"))
        for t in TRAIT_DEFINITIONS:
            self.assertIn(t["name"], joined)

    def test_an_empty_catalogue_still_produces_one_message(self):
        self.assertEqual(chunk_blocks([], "H", "F"), ["H\nF"])
        self.assertEqual(chunk_blocks([]), [""])

    def test_a_single_oversized_block_is_sent_rather_than_dropped(self):
        giant = "x" * 9000
        chunks = chunk_blocks([giant], "H", "F")
        self.assertEqual(len(chunks), 1)
        self.assertIn(giant, chunks[0])


if __name__ == "__main__":
    unittest.main()

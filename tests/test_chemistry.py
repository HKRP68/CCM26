"""Tests for the Team Chemistry system.

Pins the country block curve, the published combination table, the Icon
double-count rule, the variety-over-quantity special-card score, and the
edge cases (short XIs, missing country data, the 7-per-country ceiling).
"""

import unittest
from types import SimpleNamespace

from services import chemistry


def _card(country, version="Base"):
    return SimpleNamespace(country=country, version=version)


def _xi(shape, version="Base"):
    """Build an XI from a block shape like ``(7, 4)``.

    Country names are synthetic (``C0``, ``C1``, …) — only the sizes matter.
    """
    players = []
    for index, size in enumerate(shape):
        players.extend(_card(f"C{index}", version) for _ in range(size))
    return players


class CountryCurveTests(unittest.TestCase):

    def test_block_value_curve_is_concave(self):
        expected = {0: 0, 1: 0, 2: 8, 3: 18, 4: 30, 5: 40, 6: 46, 7: 50}
        for size, value in expected.items():
            self.assertEqual(chemistry.country_block_value(size), value)

    def test_marginal_value_peaks_at_the_fourth_countryman(self):
        steps = [chemistry.country_block_value(n)
                 - chemistry.country_block_value(n - 1) for n in range(2, 8)]
        self.assertEqual(steps, [8, 10, 12, 10, 6, 4])
        self.assertEqual(max(steps), steps[2])          # the 4th player

    def test_block_value_never_read_past_seven(self):
        self.assertEqual(chemistry.country_block_value(9),
                         chemistry.country_block_value(7))

    def test_one_country_cannot_carry_an_xi(self):
        # 50 of 80 — a second real block is always required.
        self.assertLess(chemistry.country_block_value(7),
                        chemistry.COUNTRY_CHEMISTRY_CAP)


class CombinationTableTests(unittest.TestCase):
    """The published table players will see in the app."""

    TABLE = {
        (7, 4): 80,
        (6, 5): 80,
        (5, 5, 1): 80,
        (5, 4, 2): 78,
        (4, 4, 3): 78,
        (5, 3, 3): 76,
        (6, 3, 2): 72,
        (7, 3, 1): 68,
        (7, 2, 2): 66,
        (3, 3, 3, 2): 62,
    }

    def test_published_combination_table(self):
        for shape, expected in self.TABLE.items():
            total, _ = chemistry.country_chemistry(_xi(shape))
            self.assertEqual(total, expected, f"shape {shape}")

    def test_diverse_squads_beat_stacked_ones(self):
        # The headline balance claim: a 3-nation 4-4-3 outscores a 7-stack.
        four_four_three, _ = chemistry.country_chemistry(_xi((4, 4, 3)))
        seven_three_one, _ = chemistry.country_chemistry(_xi((7, 3, 1)))
        self.assertGreater(four_four_three, seven_three_one)

    def test_stack_plus_filler_exploit_is_closed(self):
        # Under naive pair-counting this shape scored a perfect 80.
        total, _ = chemistry.country_chemistry(_xi((7, 1, 1, 1, 1)))
        self.assertEqual(total, 50)

    def test_only_three_shapes_reach_a_full_eighty(self):
        maxed = [shape for shape in _legal_shapes(11)
                 if chemistry.country_chemistry(_xi(shape))[0] >= 80]
        self.assertEqual(sorted(maxed), [(5, 5, 1), (6, 5), (7, 4)])

    def test_many_shapes_stay_competitive(self):
        # Diversity should cost a little, not disqualify you.
        close = [shape for shape in _legal_shapes(11)
                 if chemistry.country_chemistry(_xi(shape))[0] >= 75]
        self.assertGreaterEqual(len(close), 7)


def _legal_shapes(total, most=7):
    """Every block shape for an XI of ``total`` under the 7-per-country rule."""
    def walk(remaining, ceiling):
        if remaining == 0:
            yield ()
            return
        for size in range(min(remaining, ceiling), 0, -1):
            for rest in walk(remaining - size, size):
                yield (size,) + rest
    return list(walk(total, most))


class IconRuleTests(unittest.TestCase):

    def test_lone_icon_is_no_longer_dead_weight(self):
        plain, _ = chemistry.country_chemistry([_card("West Indies")])
        icon, _ = chemistry.country_chemistry(
            [_card("West Indies", "Icon")])
        self.assertEqual(plain, 0)
        self.assertEqual(icon, 8)

    def test_icon_lifts_a_small_nation_core(self):
        # Lara + 2 West Indians: block of 3 sizes as 4.
        squad = [_card("West Indies", "Icon"), _card("West Indies"),
                 _card("West Indies")]
        total, blocks = chemistry.country_chemistry(squad)
        self.assertEqual(blocks[0]["effective"], 4)
        self.assertEqual(total, 30)

    def test_icon_does_nothing_for_an_already_maxed_block(self):
        stacked = _xi((7,))
        with_icon = _xi((6,)) + [_card("C0", "Icon")]
        self.assertEqual(chemistry.country_chemistry(stacked)[0],
                         chemistry.country_chemistry(with_icon)[0])

    def test_icons_do_not_bypass_the_squad_limit(self):
        # Seven real players is legal however many of them are Icons.
        valid, errors = chemistry.validate_country_rule(_xi((7, 4), "Icon"))
        self.assertTrue(valid, errors)
        valid, errors = chemistry.validate_country_rule(_xi((8, 3)))
        self.assertFalse(valid)
        self.assertIn("Max 7", errors[0])


class SpecialChemistryTests(unittest.TestCase):

    def test_version_strings_map_to_canonical_types(self):
        cases = {
            "Base": None, "": None, "  base ": None,
            "Icon": "icon", "Icon Prime": "icon",
            "TOTY": "toty", "Team of the Year 2026": "toty",
            "Prime": "prime", "Peak": "prime",
            "Legend": "legend", "Ultimate Legend": "legend",
            "World Cup 2023": "event", "Gold": "event", "IPL 2026": "event",
        }
        for version, expected in cases.items():
            self.assertEqual(chemistry.special_type(version), expected,
                             f"version {version!r}")

    def test_full_twenty_needs_one_of_every_type(self):
        squad = [_card("India", v) for v in
                 ("Icon", "TOTY", "Prime", "Legend", "World Cup 2023")]
        total, detail = chemistry.special_chemistry(squad)
        self.assertEqual(total, 20)
        self.assertEqual(detail["types"],
                         ["icon", "toty", "prime", "legend", "event"])

    def test_variety_beats_quantity(self):
        # The whole anti-pay-to-win claim: eleven Icons lose to five mixed.
        all_icons = [_card("India", "Icon") for _ in range(11)]
        mixed = [_card("India", v) for v in
                 ("Icon", "TOTY", "Prime", "Legend", "Gold")]
        self.assertEqual(chemistry.special_chemistry(all_icons)[0], 8)
        self.assertEqual(chemistry.special_chemistry(mixed)[0], 20)

    def test_base_only_squad_scores_nothing_special(self):
        self.assertEqual(chemistry.special_chemistry(_xi((7, 4)))[0], 0)

    def test_special_score_is_capped_at_twenty(self):
        squad = [_card("India", v) for v in
                 ("Icon", "TOTY", "Prime", "Legend", "Gold", "Icon", "TOTY")]
        self.assertEqual(chemistry.special_chemistry(squad)[0], 20)


class FullReportTests(unittest.TestCase):

    def test_perfect_hundred(self):
        squad = _xi((7, 4))
        for index, version in enumerate(
                ("Icon", "TOTY", "Prime", "Legend", "World Cup 2023")):
            squad[index] = _card(squad[index].country, version)
        report = chemistry.calculate_chemistry(squad)
        self.assertEqual(report["total"], 100)
        self.assertEqual(report["country"], 80)
        self.assertEqual(report["special"], 20)
        self.assertTrue(report["valid"])

    def test_shape_is_reported_for_the_ui(self):
        report = chemistry.calculate_chemistry(_xi((5, 4, 2)))
        self.assertEqual(report["shape"], "5-4-2")

    def test_short_xi_is_prorated_and_flagged(self):
        # 6 players of one country would score 46; pro-rated to 6/11 of that.
        report = chemistry.calculate_chemistry(_xi((6,)))
        self.assertTrue(report["prorated"])
        self.assertEqual(report["raw_total"], 46)
        self.assertEqual(report["country"], int(46 * 6 / 11))

    def test_short_xi_cannot_outscore_a_full_one(self):
        best_short = max(
            chemistry.calculate_chemistry(_xi(shape))["total"]
            for size in range(1, 11) for shape in _legal_shapes(size))
        full = chemistry.calculate_chemistry(_xi((7, 4)))["total"]
        self.assertLess(best_short, full)

    def test_full_xi_is_never_prorated(self):
        self.assertFalse(chemistry.calculate_chemistry(_xi((7, 4)))["prorated"])

    def test_live_version_strings_are_classified_correctly(self):
        # The three editions actually present in data/players.json. "Base card"
        # must not fall through to the Event catch-all.
        self.assertIsNone(chemistry.special_type("Base card"))
        self.assertEqual(chemistry.special_type("Legend card"), "legend")
        self.assertEqual(chemistry.special_type("Star Card"), "event")

    def test_live_country_spellings_fold_onto_one_block(self):
        # data/players.json carries both spellings of Ireland.
        squad = ([_card("Ireland Republic")] * 3
                 + [_card("Republic of Ireland")])
        _total, blocks = chemistry.country_chemistry(squad)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["country"], "Ireland")
        self.assertEqual(blocks[0]["count"], 4)

    def test_missing_country_data_forms_one_block(self):
        squad = [_card(None), _card(""), _card("   "), _card("India")]
        _total, blocks = chemistry.country_chemistry(squad)
        unknown = [b for b in blocks if b["country"] == "Unknown"]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0]["count"], 3)

    def test_empty_xi_does_not_raise(self):
        report = chemistry.calculate_chemistry([])
        self.assertEqual(report["total"], 0)
        self.assertEqual(report["shape"], "")


class MatchEffectTests(unittest.TestCase):

    def test_bonus_band_endpoints(self):
        self.assertEqual(chemistry.chemistry_bonus_pct(0), 0.0)
        self.assertEqual(chemistry.chemistry_bonus_pct(100), 3.0)
        self.assertEqual(chemistry.chemistry_bonus_pct(80), 2.4)

    def test_bonus_is_never_negative_and_never_runs_away(self):
        self.assertEqual(chemistry.chemistry_bonus_pct(-50), 0.0)
        self.assertEqual(chemistry.chemistry_bonus_pct(1000), 3.0)


if __name__ == "__main__":
    unittest.main()

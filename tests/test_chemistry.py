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


def _rc(country, category, version="Base card"):
    return SimpleNamespace(country=country, category=category, version=version)


class RoleChemistryTests(unittest.TestCase):
    """The /cmuchem breakdown."""

    def _squad(self):
        # 6 Indians (4 bowlers + keeper + all-rounder), 4 lone batsmen,
        # 1 unconnected all-rounder.
        squad = [_rc(f"Solo{i}", "Batsman") for i in range(4)]
        squad += [_rc("India", "Bowler") for _ in range(4)]
        squad += [_rc("India", "Wicket Keeper"), _rc("India", "All-rounder")]
        squad += [_rc("SoloA", "All-rounder")]
        return squad

    def test_role_lines_are_ordered_and_labelled(self):
        lines = chemistry.role_chemistry(self._squad())
        self.assertEqual([line["label"] for line in lines],
                         ["BAT", "BOWL", "WK", "ALR"])

    def test_role_maximums_match_the_card(self):
        lines = {l["label"]: l for l in chemistry.role_chemistry(self._squad())}
        self.assertEqual(lines["BAT"]["max_bonus"], 12)
        self.assertEqual(lines["BOWL"]["max_bonus"], 12)
        self.assertEqual(lines["WK"]["max_bonus"], 12)
        self.assertEqual(lines["ALR"]["max_bonus"], 9)
        # 12/12/12/9 must stay ×3 of the 4/4/4/3 weighting it scaled from.
        self.assertEqual(lines["ALR"]["max_bonus"] * 4,
                         lines["BAT"]["max_bonus"] * 3)

    def test_connected_role_scores_full_country_component(self):
        lines = {l["label"]: l for l in chemistry.role_chemistry(self._squad())}
        # Four bowlers all inside India's block → fully connected.
        self.assertEqual(lines["BOWL"]["country_component"], 20)
        # Four batsmen each alone in their country → nothing.
        self.assertEqual(lines["BAT"]["country_component"], 0)

    def test_only_all_rounders_are_halved(self):
        lines = {l["label"]: l for l in chemistry.role_chemistry(self._squad())}
        self.assertTrue(lines["ALR"]["halved"])
        for label in ("BAT", "BOWL", "WK"):
            self.assertFalse(lines[label]["halved"])

    def test_fully_connected_role_reaches_its_ceiling(self):
        lines = chemistry.role_chemistry(
            [_rc("India", "Bowler")] * 4, xi_bonus=20)
        bowl = [l for l in lines if l["label"] == "BOWL"][0]
        self.assertEqual(bowl["bonus"], 12)

    def test_all_rounder_ceiling_is_reachable(self):
        # Dividing the halved component by the full 40 would cap ALR at 75% of
        # its ceiling, so a perfect ALR line could never appear.
        lines = chemistry.role_chemistry(
            [_rc("India", "All-rounder")] * 3, xi_bonus=20)
        alr = [l for l in lines if l["label"] == "ALR"][0]
        self.assertEqual(alr["bonus"], 9)
        self.assertEqual(alr["max_bonus"], 9)

    def test_a_core_of_three_connects_a_role(self):
        # Role connection is measured on the 3-block so that full diversity
        # (4 countries) and connected roles can co-exist inside 11 players.
        three = chemistry.role_chemistry([_rc("India", "Bowler")] * 3)
        seven = chemistry.role_chemistry([_rc("India", "Bowler")] * 7)
        self.assertEqual(three[1]["country_component"], 20)
        self.assertEqual(three[1]["country_component"],
                         seven[1]["country_component"])

    def test_icon_connects_an_otherwise_short_block(self):
        # A pair plus an Icon sizes as three, which is what makes a perfect
        # 3-3-3-2 card possible at all.
        pair = chemistry.role_chemistry([_rc("India", "Bowler")] * 2)
        with_icon = chemistry.role_chemistry(
            [_rc("India", "Bowler"), _rc("India", "Bowler", "Icon")])
        self.assertLess(pair[1]["country_component"], 20)
        self.assertEqual(with_icon[1]["country_component"], 20)

    def test_empty_role_scores_zero_rather_than_dividing_by_zero(self):
        lines = chemistry.role_chemistry([_rc("India", "Bowler")])
        bat = [l for l in lines if l["label"] == "BAT"][0]
        self.assertEqual(bat["players"], 0)
        self.assertEqual(bat["country_component"], 0)

    def test_unknown_category_falls_back_to_batsman(self):
        self.assertEqual(chemistry.role_of(_rc("India", "Mystery")), "Batsman")
        self.assertEqual(chemistry.role_of(_rc("India", "WK")), "Wicket Keeper")
        self.assertEqual(chemistry.role_of(_rc("India", "allrounder")),
                         "All-rounder")

    def test_diversity_and_variety_target_four(self):
        squad = [_rc(f"C{i}", "Batsman") for i in range(4)]
        self.assertEqual(chemistry.country_diversity(squad), (20, 4))
        squad = [_rc("India", "Batsman") for _ in range(2)]
        self.assertEqual(chemistry.country_diversity(squad), (5, 1))

        varied = [_rc("India", "Batsman", v) for v in
                  ("Icon", "TOTY", "Prime", "Legend")]
        self.assertEqual(chemistry.card_variety(varied), (15, 4))
        self.assertEqual(chemistry.card_variety(
            [_rc("India", "Batsman", "Base card")]), (0, 0))

    def test_diversity_and_variety_are_capped(self):
        squad = [_rc(f"C{i}", "Batsman") for i in range(8)]
        self.assertEqual(chemistry.country_diversity(squad)[0], 20)
        varied = [_rc("India", "Batsman", v) for v in
                  ("Icon", "TOTY", "Prime", "Legend", "Star Card")]
        self.assertEqual(chemistry.card_variety(varied)[0], 15)

    def test_overall_is_the_honest_sum_of_the_parts(self):
        report = chemistry.calculate_role_report(self._squad())
        self.assertEqual(
            report["total"],
            report["role_total"] + report["diversity"]
            + report["variety"] + report["xi_bonus"])
        self.assertEqual(report["total_max"], 100)
        self.assertEqual(report["total_max"],
                         report["role_max"] + report["diversity_max"]
                         + report["variety_max"] + report["xi_bonus_max"])

    def test_total_can_actually_reach_its_maximum(self):
        # A perfect card is genuinely buildable: 3-3-3-2 across four countries,
        # the two-man block sized up to three by an Icon, and one card of each
        # of the five special types. This is the test that would have caught
        # the unreachable-maximum bug.
        squad = ([_rc("India", "Batsman") for _ in range(3)]
                 + [_rc("Australia", "Bowler") for _ in range(3)]
                 + [_rc("England", "All-rounder") for _ in range(2)]
                 + [_rc("England", "Wicket Keeper")]
                 + [_rc("West Indies", "Batsman", "Icon"),
                    _rc("West Indies", "Bowler")])
        for card, version in zip(squad, ("TOTY", "Prime", "Legend",
                                         "Star Card")):
            card.version = version
        report = chemistry.calculate_role_report(squad)
        self.assertEqual(report["role_total"], 45)
        self.assertEqual(report["diversity"], 20)
        self.assertEqual(report["variety"], 15)
        self.assertEqual(report["xi_bonus"], 20)
        self.assertEqual(report["total"], 100)

    def test_every_component_ceiling_is_independently_reachable(self):
        # Guards the whole card against the flaw the 80/20 rework exists to
        # fix: a ceiling nobody can hit reads to players as a broken stat.
        for line in chemistry.role_chemistry([], xi_bonus=0):
            role = line["role"]
            connected = [_rc("India", role)] * 3
            best = chemistry.role_chemistry(connected, xi_bonus=20)
            earned = [l for l in best if l["role"] == role][0]
            self.assertEqual(earned["bonus"], earned["max_bonus"], role)

    def test_report_never_exceeds_its_maximum(self):
        squad = [_rc(f"C{i % 5}", "Batsman", "Icon") for i in range(11)]
        report = chemistry.calculate_role_report(squad)
        self.assertLessEqual(report["total"], report["total_max"])

    def test_the_parts_are_defined_to_sum_to_one_hundred(self):
        # The card's whole contract: it reads /100 because the components
        # genuinely add to 100, not because a smaller total was normalised up.
        self.assertEqual(
            sum(chemistry.ROLE_MAX_BONUS.values())
            + chemistry.DIVERSITY_MAX + chemistry.VARIETY_MAX
            + chemistry.SPECIAL_CHEMISTRY_CAP,
            100)
        self.assertEqual(chemistry.CMUCHEM_TOTAL_MAX, 100)

    def test_rounding_is_half_up_not_bankers(self):
        # round(0.5) is 0 in Python; a role bonus of 0.5 must render as +1.
        self.assertEqual(chemistry._round_half_up(0.5), 1)
        self.assertEqual(chemistry._round_half_up(1.5), 2)
        self.assertEqual(chemistry._round_half_up(2.5), 3)


class MessageRenderTests(unittest.TestCase):
    """The rendered card, independent of Telegram."""

    def _text(self):
        squad = ([_rc("India", "Bowler") for _ in range(4)]
                 + [_rc("Australia", "Batsman") for _ in range(4)]
                 + [_rc("England", "All-rounder") for _ in range(2)]
                 + [_rc("England", "Wicket Keeper")])
        return chemistry.render_chemistry_card(squad)

    def test_card_has_every_line_the_spec_asks_for(self):
        text = self._text()
        for fragment in ("<b>BAT</b>", "<b>BOWL</b>", "<b>WK</b>",
                         "<b>ALR</b>", "Country Diversity", "Card Variety",
                         "Playing XI Bonus", "Overall Chemistry",
                         "ALR boost is halved"):
            self.assertIn(fragment, text)

    def test_all_rounder_line_shows_the_halving(self):
        self.assertIn("÷ 2)", self._text())

    def test_countries_and_types_show_their_targets(self):
        text = self._text()
        self.assertIn("/4 countries", text)
        self.assertIn("/4 special types", text)


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

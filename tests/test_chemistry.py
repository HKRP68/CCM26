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
            "Star Card": "star",
            # League editions are distinct types, not one lumped "event".
            "IPL 2026": "ipl", "WPL": "wpl", "BBL 2025": "bbl",
            "PSL": "psl", "SA20": "sa20", "CPL": "cpl",
            # Only genuinely unclassified drops fall through to the catch-all.
            "World Cup 2023": "event", "Gold": "event",
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
        self.assertEqual(chemistry.special_type("Star Card"), "star")

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
    """Category Chemistry: 20 per role, lost to players outside the majority."""

    def _squad(self):
        # BAT 4 all India, BOWL 4 = 3 Australia + 1 India, WK 1 England,
        # ALR 2 = England + South Africa.
        return ([_rc("India", "Batsman") for _ in range(4)]
                + [_rc("Australia", "Bowler") for _ in range(3)]
                + [_rc("India", "Bowler")]
                + [_rc("England", "Wicket Keeper")]
                + [_rc("England", "All-rounder"),
                   _rc("South Africa", "All-rounder")])

    def test_role_lines_are_ordered_and_labelled(self):
        lines = chemistry.role_chemistry(self._squad())
        self.assertEqual([line["label"] for line in lines],
                         ["BAT", "BOWL", "WK", "ALR"])

    def test_role_maximums_match_the_card(self):
        lines = {l["label"]: l for l in chemistry.role_chemistry(self._squad())}
        self.assertEqual(lines["BAT"]["max_bonus"], 4)
        self.assertEqual(lines["BOWL"]["max_bonus"], 4)
        self.assertEqual(lines["WK"]["max_bonus"], 4)
        self.assertEqual(lines["ALR"]["max_bonus"], 3)

    def test_category_curve_for_four_players(self):
        # The published example table in /chemhelp.
        def score(countries):
            return chemistry.category_chemistry(
                [_rc(c, "Batsman") for c in countries])[0]
        self.assertEqual(score(["IND"] * 4), 20)          # all same
        self.assertEqual(score(["IND"] * 3 + ["AUS"]), 13)
        self.assertEqual(score(["IND"] * 2 + ["AUS"] * 2), 7)
        self.assertEqual(score(["IND", "AUS", "ENG", "SA"]), 0)


    def test_all_same_country_always_scores_full(self):
        for size in range(1, 6):
            members = [_rc("India", "Bowler") for _ in range(size)]
            self.assertEqual(chemistry.category_chemistry(members)[0], 20,
                             f"size {size}")

    def test_all_different_always_scores_zero(self):
        for size in range(2, 6):
            members = [_rc(f"C{i}", "Bowler") for i in range(size)]
            self.assertEqual(chemistry.category_chemistry(members)[0], 0,
                             f"size {size}")

    def test_single_player_role_is_trivially_unified(self):
        score, country, count = chemistry.category_chemistry(
            [_rc("England", "Wicket Keeper")])
        self.assertEqual((score, country, count), (20, "England", 1))

    def test_empty_role_scores_zero_without_dividing_by_zero(self):
        self.assertEqual(chemistry.category_chemistry([]), (0, None, 0))
        lines = chemistry.role_chemistry([_rc("India", "Bowler")])
        bat = [l for l in lines if l["label"] == "BAT"][0]
        self.assertEqual((bat["players"], bat["category"]), (0, 0))

    def test_majority_country_and_outsiders_are_reported(self):
        lines = {l["label"]: l for l in chemistry.role_chemistry(self._squad())}
        self.assertEqual(lines["BOWL"]["majority_country"], "Australia")
        self.assertEqual(lines["BOWL"]["majority_count"], 3)
        self.assertEqual(lines["BOWL"]["outsiders"], 1)
        self.assertEqual(lines["BAT"]["outsiders"], 0)

    def test_tied_majority_is_stable(self):
        # 2-2 split: whichever country is reported must not vary run to run.
        members = [_rc("India", "Bowler"), _rc("Australia", "Bowler"),
                   _rc("Australia", "Bowler"), _rc("India", "Bowler")]
        first = chemistry.category_chemistry(members)[1]
        for _ in range(5):
            self.assertEqual(chemistry.category_chemistry(members)[1], first)

    def test_only_all_rounders_are_halved(self):
        lines = {l["label"]: l for l in chemistry.role_chemistry(self._squad())}
        self.assertTrue(lines["ALR"]["halved"])
        for label in ("BAT", "BOWL", "WK"):
            self.assertFalse(lines[label]["halved"])

    def test_halving_applies_to_the_boost_not_the_hundred(self):
        # ALR contributes its full category to Overall; only its match boost
        # is halved, matching the worked example on the card.
        squad = [_rc("India", "All-rounder") for _ in range(2)]
        report = chemistry.calculate_role_report(squad)
        alr = [l for l in report["roles"] if l["label"] == "ALR"][0]
        self.assertEqual(alr["category"], 20)
        self.assertEqual(report["category_total"], 20)

    def test_every_role_ceiling_is_reachable(self):
        # A ceiling nobody can hit reads to players as a broken stat.
        for role in chemistry.ROLE_ORDER:
            unified = [_rc("India", role) for _ in range(3)]
            line = [l for l in chemistry.role_chemistry(unified, xi_bonus=20)
                    if l["role"] == role][0]
            self.assertEqual(line["bonus"], line["max_bonus"], role)

    def test_worked_example_from_the_spec(self):
        # BAT all different + full XI bonus -> +2/4; ALR at 7 halved -> +2/3.
        bat = chemistry.role_chemistry(
            [_rc(f"C{i}", "Batsman") for i in range(4)], xi_bonus=20)
        self.assertEqual(
            [l for l in bat if l["label"] == "BAT"][0]["bonus"], 2)
        alr = chemistry.role_chemistry(
            [_rc("India", "All-rounder")] * 2
            + [_rc("Australia", "All-rounder"), _rc("England", "All-rounder")],
            xi_bonus=20)
        line = [l for l in alr if l["label"] == "ALR"][0]
        self.assertEqual(line["category"], 7)
        self.assertEqual(line["bonus"], 2)

    def test_colour_tracks_severity(self):
        self.assertEqual(chemistry.chem_colour(20), "🟩")
        self.assertEqual(chemistry.chem_colour(13), "🟨")
        self.assertEqual(chemistry.chem_colour(7), "🟧")
        self.assertEqual(chemistry.chem_colour(0), "🟥")

    def test_colour_scales_to_other_maximums(self):
        self.assertEqual(chemistry.chem_colour(10, 10), "🟩")
        self.assertEqual(chemistry.chem_colour(0, 10), "🟥")
        self.assertEqual(chemistry.chem_colour(100, 100), "🟩")

    def test_diversity_tiers(self):
        def score(n):
            return chemistry.country_diversity(
                [_rc(f"C{i}", "Batsman") for i in range(n)])[0]
        self.assertEqual(score(5), 10)
        self.assertEqual(score(4), 10)
        self.assertEqual(score(3), 7)
        self.assertEqual(score(2), 3)
        self.assertEqual(score(1), 0)

    def test_variety_tiers(self):
        types = ("Icon", "TOTY", "Prime", "Legend card", "IPL 2026")
        def score(n):
            return chemistry.card_variety(
                [_rc("India", "Batsman", v) for v in types[:n]])[0]
        self.assertEqual(score(5), 10)
        self.assertEqual(score(4), 10)
        self.assertEqual(score(3), 7)
        self.assertEqual(score(2), 3)
        self.assertEqual(score(1), 0)

    def test_league_editions_are_distinct_types(self):
        # Card Variety wants four *different* types, so leagues cannot all
        # collapse into one "event" bucket.
        squad = [_rc("India", "Batsman", v) for v in
                 ("IPL 2026", "BBL 2025", "PSL", "SA20")]
        score, types = chemistry.card_variety(squad)
        self.assertEqual(types, 4)
        self.assertEqual(score, 10)

    def test_playing_xi_bonus_is_diversity_plus_variety(self):
        squad = ([_rc("India", "Batsman", "Icon"),
                  _rc("Australia", "Bowler", "TOTY"),
                  _rc("England", "All-rounder", "IPL 2026"),
                  _rc("South Africa", "Wicket Keeper", "BBL")])
        total, detail = chemistry.playing_xi_bonus(squad)
        self.assertEqual(detail["diversity"], 10)
        self.assertEqual(detail["variety"], 10)
        self.assertEqual(total, 20)

    def test_the_parts_are_defined_to_sum_to_one_hundred(self):
        self.assertEqual(
            chemistry.CATEGORY_CHEMISTRY_MAX + chemistry.XI_BONUS_MAX, 100)
        self.assertEqual(chemistry.CMUCHEM_TOTAL_MAX, 100)
        self.assertEqual(chemistry.CATEGORY_CHEMISTRY_MAX, 80)
        self.assertEqual(chemistry.XI_BONUS_MAX, 20)

    def test_overall_is_base_plus_earned(self):
        report = chemistry.calculate_role_report(self._squad())
        self.assertEqual(report["total"],
                         report["base"] + report["earned"])
        self.assertEqual(report["base"], 30)
        self.assertEqual(report["earned_max"], 70)

    def test_raw_points_are_category_plus_xi_bonus(self):
        report = chemistry.calculate_role_report(self._squad())
        self.assertEqual(report["raw_points"],
                         report["category_total"] + report["xi_bonus"])
        self.assertEqual(report["raw_points_max"], 100)

    def test_total_can_actually_reach_its_maximum(self):
        # Every role unified, four countries, four special types.
        squad = ([_rc("India", "Batsman") for _ in range(4)]
                 + [_rc("Australia", "Bowler") for _ in range(4)]
                 + [_rc("England", "All-rounder") for _ in range(2)]
                 + [_rc("South Africa", "Wicket Keeper")])
        for card, version in zip(squad, ("Icon", "TOTY", "IPL 2026", "BBL")):
            card.version = version
        report = chemistry.calculate_role_report(squad)
        self.assertEqual(report["category_total"], 80)
        self.assertEqual(report["xi_bonus"], 20)
        self.assertEqual(report["total"], 100)
        self.assertEqual(report["boost_total"], report["boost_max"])

    def test_report_never_exceeds_its_maximum(self):
        squad = [_rc(f"C{i % 5}", "Batsman", "Icon") for i in range(11)]
        report = chemistry.calculate_role_report(squad)
        self.assertLessEqual(report["total"], report["total_max"])

    def test_rounding_is_half_up_not_bankers(self):
        self.assertEqual(chemistry._round_half_up(0.5), 1)
        self.assertEqual(chemistry._round_half_up(1.5), 2)
        self.assertEqual(chemistry._round_half_up(2.5), 3)


class ImprovementTipTests(unittest.TestCase):
    """The /cmuchem and /chemhelp "how to improve" advice."""

    def test_perfect_squad_has_nothing_to_suggest(self):
        squad = ([_rc("India", "Batsman") for _ in range(4)]
                 + [_rc("Australia", "Bowler") for _ in range(4)]
                 + [_rc("England", "All-rounder") for _ in range(2)]
                 + [_rc("South Africa", "Wicket Keeper")])
        for card, version in zip(squad, ("Icon", "TOTY", "IPL 2026", "BBL")):
            card.version = version
        self.assertEqual(chemistry.improvement_tips(squad), [])

    def test_tips_are_ranked_by_points_available(self):
        squad = ([_rc(f"C{i}", "Batsman") for i in range(4)]
                 + [_rc("India", "Bowler") for _ in range(3)]
                 + [_rc("Australia", "Bowler")]
                 + [_rc("India", "Wicket Keeper")]
                 + [_rc("India", "All-rounder") for _ in range(2)])
        tips = chemistry.improvement_tips(squad)
        points = [p for p, _text in tips]
        self.assertEqual(points, sorted(points, reverse=True))

    def test_broken_role_is_the_top_tip(self):
        squad = ([_rc(f"C{i}", "Batsman") for i in range(4)]
                 + [_rc("India", "Bowler") for _ in range(4)]
                 + [_rc("India", "All-rounder") for _ in range(2)]
                 + [_rc("India", "Wicket Keeper")])
        top = chemistry.improvement_tips(squad)[0]
        self.assertEqual(top[0], 20)
        self.assertIn("BAT", top[1])

    def test_tips_are_capped(self):
        squad = [_rc(f"C{i}", ["Batsman", "Bowler", "Wicket Keeper",
                               "All-rounder"][i % 4]) for i in range(11)]
        self.assertLessEqual(len(chemistry.improvement_tips(squad, limit=3)), 3)

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


class XiSummaryTests(unittest.TestCase):
    """The compact summary /pxi puts in its header and footer."""

    def _full_xi(self):
        return ([_rc("India", "Batsman") for _ in range(6)]
                + [_rc("Australia", "Bowler") for _ in range(3)]
                + [_rc("England", "All-rounder"),
                   _rc("Afghanistan", "Wicket Keeper")])

    def test_part_built_side_shows_nothing(self):
        self.assertIsNone(chemistry.xi_summary(self._full_xi()[:7]))
        self.assertIsNone(chemistry.xi_summary([]))

    def test_full_xi_reports_total_and_shape(self):
        total, shape = chemistry.xi_summary(self._full_xi())
        self.assertEqual(shape, "6-3-1-1")
        self.assertTrue(0 <= total <= chemistry.CMUCHEM_TOTAL_MAX)

    def test_shape_is_ordered_largest_block_first(self):
        squad = ([_rc("India", "Batsman") for _ in range(4)]
                 + [_rc("Australia", "Bowler") for _ in range(4)]
                 + [_rc("England", "All-rounder") for _ in range(3)])
        self.assertEqual(chemistry.xi_summary(squad)[1], "4-4-3")

    def test_summary_total_agrees_with_the_full_card(self):
        squad = self._full_xi()
        total, _shape = chemistry.xi_summary(squad)
        self.assertEqual(total,
                         chemistry.calculate_role_report(squad)["total"])


class MatchBoostTests(unittest.TestCase):
    """What chemistry actually does: a per-role effective-rating lift."""

    def _unified(self):
        return ([_rc("India", "Batsman") for _ in range(4)]
                + [_rc("Australia", "Bowler") for _ in range(4)]
                + [_rc("England", "All-rounder") for _ in range(2)]
                + [_rc("South Africa", "Wicket Keeper")])

    def _scattered(self):
        roles = ["Batsman", "Bowler", "Wicket Keeper", "All-rounder"]
        return [_rc(f"C{i}", roles[i % 4]) for i in range(11)]

    def test_boosts_are_returned_per_role(self):
        boosts = chemistry.match_boosts(self._unified())
        self.assertEqual(set(boosts), set(chemistry.ROLE_ORDER))

    def test_unified_squad_out_boosts_a_scattered_one(self):
        unified = chemistry.match_boosts(self._unified())
        scattered = chemistry.match_boosts(self._scattered())
        self.assertGreater(sum(unified.values()), sum(scattered.values()))

    def test_boost_never_penalises(self):
        # Chemistry is a bonus band — the worst squad plays at raw ratings,
        # never below them.
        for squad in (self._scattered(), [], [_rc("India", "Batsman")]):
            for boost in chemistry.match_boosts(squad).values():
                self.assertGreaterEqual(boost, 0)

    def test_boost_is_capped_at_the_published_maximum(self):
        squad = self._unified()
        for card, version in zip(squad, ("Icon", "TOTY", "IPL 2026", "BBL")):
            card.version = version
        boosts = chemistry.match_boosts(squad)
        for role, boost in boosts.items():
            self.assertLessEqual(boost, chemistry.ROLE_MAX_BONUS[role], role)
        self.assertEqual(boosts["Batsman"], 4)
        self.assertEqual(boosts["All-rounder"], 3)

    def test_swing_between_best_and_worst_is_bounded(self):
        # The whole point of the cap: chemistry decides close matches, it must
        # not overturn a rating gap.
        best = self._unified()
        for card, version in zip(best, ("Icon", "TOTY", "IPL 2026", "BBL")):
            card.version = version
        swing = max(chemistry.match_boosts(best).values())
        self.assertLessEqual(swing, 4)

    def test_engine_dicts_are_accepted_without_shims(self):
        # The match engine carries its XI as plain dicts (handlers.match._pd).
        as_dicts = [{"country": "India", "category": "Bowler",
                     "version": "Base card"} for _ in range(4)]
        self.assertEqual(chemistry.match_boosts(as_dicts)["Bowler"],
                         chemistry.match_boosts(
                             [_rc("India", "Bowler") for _ in range(4)])["Bowler"])

    def test_missing_country_or_version_does_not_raise(self):
        for squad in ([{"category": "Bowler"}],
                      [{"country": None, "category": None, "version": None}],
                      [_rc(None, None, None)]):
            boosts = chemistry.match_boosts(squad)
            self.assertEqual(set(boosts), set(chemistry.ROLE_ORDER))


class PartnershipBondTests(unittest.TestCase):
    """The live layer: the crease pair's own chemistry."""

    def test_countrymen_bond_fully(self):
        self.assertEqual(
            chemistry.partnership_bond(_rc("India", "Batsman"),
                                       _rc("India", "Batsman")), 1.0)

    def test_strangers_do_not_bond(self):
        self.assertEqual(
            chemistry.partnership_bond(_rc("India", "Batsman"),
                                       _rc("Australia", "Batsman")), 0.0)

    def test_an_icon_part_bonds_with_anyone(self):
        # This is where the Icon rule earns its place back: a legend has
        # partnered with everyone, so Lara is never dead weight at the crease.
        bond = chemistry.partnership_bond(
            _rc("West Indies", "Batsman", "Icon"),
            _rc("Australia", "Batsman"))
        self.assertEqual(bond, chemistry.PARTNERSHIP_WITH_ICON)
        self.assertGreater(bond, 0.0)
        self.assertLess(bond, 1.0)

    def test_icon_at_either_end_counts(self):
        icon = _rc("West Indies", "Batsman", "Icon")
        plain = _rc("Australia", "Batsman")
        self.assertEqual(chemistry.partnership_bond(icon, plain),
                         chemistry.partnership_bond(plain, icon))

    def test_missing_partner_is_safe(self):
        self.assertEqual(
            chemistry.partnership_bond(_rc("India", "Batsman"), None), 0.0)
        self.assertEqual(chemistry.partnership_bond(None, None), 0.0)

    def test_bond_accepts_engine_dicts(self):
        pair = {"country": "India", "category": "Batsman", "version": "Base"}
        self.assertEqual(chemistry.partnership_bond(pair, dict(pair)), 1.0)


class ClutchTests(unittest.TestCase):
    """Chemistry counts double when the game is decided."""

    def test_normal_overs_are_unmultiplied(self):
        self.assertEqual(chemistry.clutch_multiplier(5, 20), 1.0)
        self.assertEqual(chemistry.clutch_multiplier(14, 20), 1.0)

    def test_death_overs_multiply(self):
        self.assertEqual(chemistry.clutch_multiplier(16, 20),
                         chemistry.CLUTCH_MULTIPLIER)
        self.assertEqual(chemistry.clutch_multiplier(20, 20),
                         chemistry.CLUTCH_MULTIPLIER)

    def test_chase_pressure_multiplies_at_any_stage(self):
        self.assertEqual(chemistry.clutch_multiplier(3, 20, pressure=0.9),
                         chemistry.CLUTCH_MULTIPLIER)
        self.assertEqual(chemistry.clutch_multiplier(3, 20, pressure=0.1), 1.0)

    def test_clutch_scales_with_the_innings_length(self):
        # A 5-over dash should go clutch earlier in absolute overs than a 20.
        self.assertEqual(chemistry.clutch_multiplier(5, 5),
                         chemistry.CLUTCH_MULTIPLIER)
        self.assertEqual(chemistry.clutch_multiplier(5, 20), 1.0)

    def test_garbage_input_is_safe(self):
        self.assertEqual(chemistry.clutch_multiplier(None, None), 1.0)
        self.assertEqual(chemistry.clutch_multiplier("x", "y"), 1.0)
        self.assertEqual(chemistry.clutch_multiplier(1, 0), 1.0)


class FieldingCohesionTests(unittest.TestCase):

    def _unified(self):
        squad = ([_rc("India", "Batsman") for _ in range(4)]
                 + [_rc("Australia", "Bowler") for _ in range(4)]
                 + [_rc("England", "All-rounder") for _ in range(2)]
                 + [_rc("South Africa", "Wicket Keeper")])
        for card, version in zip(squad, ("Icon", "TOTY", "IPL 2026", "BBL")):
            card.version = version
        return squad

    def test_perfect_chemistry_earns_the_full_bonus(self):
        self.assertEqual(chemistry.fielding_bonus(self._unified()),
                         chemistry.FIELDING_MAX_BONUS)

    def test_scattered_side_earns_far_less(self):
        roles = ["Batsman", "Bowler", "Wicket Keeper", "All-rounder"]
        scattered = [_rc(f"C{i}", roles[i % 4]) for i in range(11)]
        self.assertLess(chemistry.fielding_bonus(scattered),
                        chemistry.fielding_bonus(self._unified()))

    def test_bonus_is_never_negative(self):
        self.assertGreaterEqual(chemistry.fielding_bonus([]), 0.0)


class BondEngineTests(unittest.TestCase):
    """The bond as the ball engine actually applies it."""

    def _mods(self, bond):
        from services.probability_engine import _apply_dynamic_mods, BASE
        probs = dict(BASE)
        _apply_dynamic_mods(probs, chem_bond=bond)
        return probs

    def test_bond_improves_running_only(self):
        base, bonded = self._mods(0.0), self._mods(1.0)
        self.assertGreater(bonded["2"], base["2"])
        self.assertGreater(bonded["1"], base["1"])
        self.assertLess(bonded["dot"], base["dot"])
        # A bond is about running, not ball-striking or survival.
        for key in ("4", "6", "W"):
            self.assertEqual(bonded[key], base[key], key)

    def test_no_bond_is_a_no_op(self):
        from services.probability_engine import BASE
        self.assertEqual(self._mods(0.0), dict(BASE))

    def test_partial_bond_is_partial(self):
        half, full = self._mods(0.5), self._mods(1.0)
        base = self._mods(0.0)
        self.assertGreater(half["2"], base["2"])
        self.assertLess(half["2"], full["2"])

    def test_bond_is_clamped(self):
        self.assertEqual(self._mods(5.0), self._mods(1.0))
        self.assertEqual(self._mods(-3.0), self._mods(0.0))

    def test_dot_never_goes_negative(self):
        self.assertGreater(self._mods(1.0)["dot"], 0)


class ThirtyToHundredScaleTests(unittest.TestCase):
    """The displayed score spans 30-100, finely enough to move on any change."""

    ROLES = ("Batsman", "Bowler", "Wicket Keeper", "All-rounder")

    def _legal_shapes(self):
        # services.xi_rules: 3-5 BAT, 3-5 BOWL, 1-2 WK, 1-3 ALR, 11 total.
        return [(b, o, w, a)
                for b in range(3, 6) for o in range(3, 6)
                for w in (1, 2) for a in range(1, 4)
                if b + o + w + a == 11]

    def _reachable(self):
        """Every displayed total any legal XI can produce."""
        import itertools

        def category(size, majority):
            return 20.0 if size <= 1 else 20.0 * (majority - 1) / (size - 1)

        tiers = [points for _threshold, points in chemistry.DIVERSITY_TIERS] + [0]
        seen = set()
        for shape in self._legal_shapes():
            per_role = [sorted({category(n, m) for m in range(1, n + 1)})
                        for n in shape]
            for combo in itertools.product(*per_role):
                for diversity in tiers:
                    for variety in tiers:
                        raw = sum(combo) + diversity + variety
                        seen.add(chemistry.SQUAD_BASE
                                 + chemistry.earned_points(raw))
        return seen

    def test_nobody_can_score_below_thirty(self):
        self.assertEqual(min(self._reachable()), 30)
        # Even the worst possible XI: every role a different country, one
        # country overall is impossible with 11 players, so use the floor case.
        worst = [_rc(f"C{i}", self.ROLES[i % 4]) for i in range(11)]
        self.assertGreaterEqual(
            chemistry.calculate_role_report(worst)["total"], 30)

    def test_the_ceiling_is_exactly_one_hundred(self):
        self.assertEqual(max(self._reachable()), 100)
        self.assertEqual(chemistry.CMUCHEM_TOTAL_MAX, 100)
        self.assertEqual(chemistry.SQUAD_BASE + chemistry.EARNED_MAX, 100)

    def test_an_empty_or_broken_xi_still_reads_thirty(self):
        for squad in ([], [_rc("India", "Batsman")]):
            total = chemistry.calculate_role_report(squad)["total"]
            self.assertGreaterEqual(total, 30)
            self.assertLessEqual(total, 100)

    def test_the_scale_is_finely_grained(self):
        # The point of leaving category unrounded: the score lands on almost
        # any integer rather than a handful of fixed values.
        reachable = {v for v in self._reachable() if 30 <= v <= 100}
        self.assertGreaterEqual(len(reachable), 60)

    def test_improving_a_unit_always_moves_the_score(self):
        def squad(unified):
            bowl = [_rc("Australia" if i < unified else f"X{i}", "Bowler")
                    for i in range(4)]
            return ([_rc("India", "Batsman") for _ in range(4)] + bowl
                    + [_rc("England", "All-rounder") for _ in range(2)]
                    + [_rc("South Africa", "Wicket Keeper")])
        totals = [chemistry.calculate_role_report(squad(n))["total"]
                  for n in range(1, 5)]
        self.assertEqual(totals, sorted(totals))
        self.assertEqual(len(set(totals)), len(totals))

    def test_earned_points_endpoints(self):
        self.assertEqual(chemistry.earned_points(0), 0)
        self.assertEqual(chemistry.earned_points(100), 70)
        self.assertEqual(chemistry.earned_points(50), 35)

    def test_earned_points_clamps(self):
        self.assertEqual(chemistry.earned_points(-40), 0)
        self.assertEqual(chemistry.earned_points(500), 70)


class LiveDisplayTests(unittest.TestCase):
    """The in-match badge and the crease-pair bond cue.

    Chemistry already decides real things during a match (a role bonus on every
    effective rating, fielding cohesion, a doubling at the death). These are the
    two renderings that finally let a player SEE it while they play, so they
    have to agree with /cmuchem rather than invent a second scale.
    """

    def _xi(self, country="India"):
        return ([_rc(country, "Batsman") for _ in range(5)]
                + [_rc(country, "Bowler") for _ in range(3)]
                + [_rc(country, "All-rounder") for _ in range(2)]
                + [_rc(country, "Wicket Keeper")])

    def test_badge_reports_the_cmuchem_number(self):
        xi = self._xi()
        total = chemistry.calculate_role_report(xi)["total"]
        badge = chemistry.live_badge(xi)
        self.assertIn(f"{total}/{chemistry.CMUCHEM_TOTAL_MAX}", badge)

    def test_a_part_built_side_shows_nothing(self):
        # Same rule as the /pxi summary: no number beats a number that moves for
        # reasons the player can't see (and the bot's synthetic XI has no cards).
        self.assertIsNone(chemistry.live_badge(self._xi()[:10]))
        self.assertIsNone(chemistry.live_badge([]))

    def test_the_badge_colour_spreads_across_the_reachable_range(self):
        # The displayed score can never drop below the 30-point squad floor, so
        # the bands have to be read on the 30-100 scale — a side stuck on one
        # colour for every possible XI would tell the player nothing.
        colours = {chemistry.live_colour(total) for total in range(30, 101)}
        self.assertEqual(len(colours), len(chemistry.LIVE_CHEM_BANDS))

    def test_a_synthetic_xi_without_country_data_is_never_scored(self):
        # handlers/vsbot.py builds its bot XI without country/version. country_of
        # folds missing data into a single "Unknown" country, so scoring one
        # would read as a perfect 11-man national block and beat every real
        # squad on the board. It must render nothing instead.
        synthetic = [SimpleNamespace(category=c)
                     for c in (["Batsman"] * 5 + ["Bowler"] * 3
                               + ["All-rounder"] * 2 + ["Wicket Keeper"])]
        self.assertIsNone(chemistry.live_badge(synthetic))
        self.assertIsNone(chemistry.live_badge(
            [_rc("", "Batsman")] + self._xi()[1:]))

    def test_a_base_card_xi_is_still_scored(self):
        # Only country gates the badge. Card version does not: an XI of ordinary
        # base cards is perfectly scorable, and Player.version is NULL on older
        # rows — requiring it would blank the badge for most real squads.
        base = [_rc("India", c, version=None)
                for c in (["Batsman"] * 5 + ["Bowler"] * 3
                          + ["All-rounder"] * 2 + ["Wicket Keeper"])]
        self.assertIsNotNone(chemistry.live_badge(base))

    def test_bond_label_tracks_the_partnership_bond(self):
        india = _rc("India", "Batsman")
        other = _rc("Australia", "Batsman")
        icon = _rc("West Indies", "Batsman", version="Icon")

        self.assertIn("Bonded", chemistry.bond_label(
            chemistry.partnership_bond(india, _rc("India", "Bowler"))))
        self.assertIn("Icon", chemistry.bond_label(
            chemistry.partnership_bond(icon, other)))
        self.assertIsNone(chemistry.bond_label(
            chemistry.partnership_bond(india, other)))

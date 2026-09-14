"""The Trait Boost where players read it: the XI card, and the live board.

The Playing XI card is where a captain decides whether the XI they are about to
field is the one they want, so this pins the things that decision reads:

  • a human side's traits appear at all — they never did, because ``_row_fields``
    returned an empty trait list for every database row and only the /lpbot
    opponent's inline traits rendered;
  • the Trait Boost is quoted per card and per team, and is *absent* for a squad
    with no traits, so nothing new appears on a card that has nothing to say;
  • the anti stat-farming gap keeps quoting the printed card ratings, next to
    the boost rather than including it — a captain who levelled their traits
    must not be told their match no longer counts because of it.

…and the same two numbers carried onto the live board, so the traits a captain
paid for stay visible while they are being spent rather than vanishing at the
toss — scoped, like Team Chemistry beside it, to the sides that actually field
their own roster.
"""

import unittest

from handlers import letsplay


class _Entry:
    """A ``UserRoster`` row, as far as the XI card is concerned."""

    def __init__(self, roster_id):
        self.id = roster_id


class _Player:
    """A ``Player`` row, as far as the XI card is concerned."""

    def __init__(self, name, rating, category="Batsman"):
        self.name = name
        self.rating = rating
        self.category = category


def _pairs(ratings, category="Batsman"):
    return [(_Entry(i + 1), _Player(f"P{i + 1}", r, category))
            for i, r in enumerate(ratings)]


def _trait(level, name="Finisher", emoji="🔥"):
    return {"effect_key": "bat_finisher", "level": level,
            "display_name": name, "emoji": emoji, "category": "Batting"}


class BattingOrderCardTests(unittest.TestCase):
    def test_an_untraited_xi_reads_exactly_as_it_always_did(self):
        text = letsplay._format_batting_order(_pairs([80] * 11), "HDR")
        self.assertIn("1. P1 <i>(80)</i>", text)
        self.assertNotIn("⚡", text)

    def test_a_traited_card_shows_its_boost_and_its_badge(self):
        text = letsplay._format_batting_order(
            _pairs([80] * 11), "HDR", trait_map={1: [_trait(5)]})
        self.assertIn("1. P1 <i>(80 ⚡+3.2)</i>", text)
        self.assertIn("🔥", text, "the trait badge belongs on a human XI too")

    def test_the_bench_is_left_plain(self):
        """Bench players are not in the XI, so their boost is not in play."""
        text = letsplay._format_batting_order(
            _pairs([80] * 11), "HDR", bench_pairs=_pairs([70]),
            trait_map={1: [_trait(5)]})
        self.assertIn("12. P1 <i>(70)</i>", text)


class FairnessNoteTests(unittest.TestCase):
    def test_no_boost_line_when_nobody_has_traits(self):
        note = letsplay._stats_fairness_note(_pairs([80] * 11), _pairs([80] * 11))
        self.assertNotIn("Trait Boost", note)
        self.assertIn("stats will count", note)

    def test_the_boost_line_names_both_sides(self):
        note = letsplay._stats_fairness_note(
            _pairs([80] * 11), _pairs([80] * 11),
            host_traits={i: [_trait(5)] for i in range(1, 12)})
        self.assertIn("⚡ <b>Trait Boost:</b>", note)
        self.assertIn("Host 80 → <b>83.2</b> (+3.2)", note)
        self.assertIn("Guest 80 → <b>80.0</b> (+0.0)", note)

    def test_the_gap_still_quotes_the_printed_card_ratings(self):
        """The boost sits beside the gap, never inside it."""
        note = letsplay._stats_fairness_note(
            _pairs([84] * 11), _pairs([80] * 11),
            host_traits={i: [_trait(5)] for i in range(1, 12)})
        self.assertIn("Host <b>84</b> vs Guest <b>80</b>", note)
        self.assertIn("stats will count", note,
                      "4 apart on the card is a fair match, boosted or not")

    def test_a_real_mismatch_still_warns_and_still_shows_the_boost(self):
        note = letsplay._stats_fairness_note(
            _pairs([88] * 11), _pairs([70] * 11),
            host_traits={i: [_trait(3)] for i in range(1, 12)})
        self.assertIn("WON'T count", note)
        self.assertIn("Trait Boost", note)

    def test_the_footnote_only_appears_when_there_is_a_boost_to_explain(self):
        plain = letsplay._stats_fairness_note(_pairs([80] * 11), _pairs([80] * 11))
        self.assertNotIn("never cost you career stats", plain)
        boosted = letsplay._stats_fairness_note(
            _pairs([80] * 11), _pairs([80] * 11),
            guest_traits={1: [_trait(4)]})
        self.assertIn("never cost you career stats", boosted)

    def test_a_practice_match_says_unranked_and_still_shows_the_boost(self):
        note = letsplay._stats_fairness_note(
            _pairs([80] * 11),
            [{"name": f"B{i}", "rating": 80, "traits": []} for i in range(11)],
            vs_bot=True, host_traits={1: [_trait(5)]})
        self.assertIn("Practice match", note)
        self.assertIn("Trait Boost", note)
        self.assertNotIn("WON'T count", note)


class SideRatingCardTests(unittest.TestCase):
    def test_the_quoted_team_overall_matches_the_ratings_printed_above_it(self):
        pairs = _pairs([80, 81, 82] + [79] * 8)
        card = letsplay._side_rating_card(pairs, {1: [_trait(5)]})
        self.assertEqual(card["base"], round(sum(p.rating for _e, p in pairs) / 11))
        self.assertAlmostEqual(card["bonus"], round(3.2 / 11, 2), places=2)

    def test_the_bot_side_is_read_from_its_inline_traits(self):
        bot_xi = [{"name": f"B{i}", "rating": 80,
                   "traits": [_trait(5)] if i == 0 else []} for i in range(11)]
        card = letsplay._side_rating_card(bot_xi)
        self.assertEqual(card["base"], 80)
        self.assertAlmostEqual(card["bonus"], round(3.2 / 11, 2), places=2)

    def test_only_the_eleven_count(self):
        """A twelfth row is a bench player and must not move the team number."""
        eleven = letsplay._side_rating_card(_pairs([80] * 11))
        twelve = letsplay._side_rating_card(_pairs([80] * 11 + [40]))
        self.assertEqual(eleven, twelve)


class LiveBoardTests(unittest.TestCase):
    """The boost carried into the match, next to Team Chemistry on the board."""

    def _state(self, bat_levels=(), bowl_levels=(), **kwargs):
        def _xi(levels):
            return [{"name": f"P{i}", "rating": 84,
                     "traits": [_trait(lv) for lv in levels] if i == 0 else []}
                    for i in range(11)]
        state = {"is_letsplay": True, "bat_team_code": "MI",
                 "bowl_team_code": "CSK",
                 "bat_xi": _xi(bat_levels), "bowl_xi": _xi(bowl_levels)}
        state.update(kwargs)
        return state

    def test_the_board_carries_both_sides_numbers(self):
        from handlers import cipl_play
        line = cipl_play._trait_boost_line(self._state(bat_levels=(5,)))
        self.assertIn("MI 84 → 84.3", line)
        self.assertIn("CSK 84 → 84.0", line)

    def test_nothing_is_printed_when_neither_side_has_traits(self):
        from handlers import cipl_play
        self.assertEqual(cipl_play._trait_boost_line(self._state()), "")

    def test_a_challenge_league_match_gets_no_line(self):
        """League squads are handed to both captains — they carry no traits."""
        from handlers import cipl_play
        state = self._state(bat_levels=(5,))
        state["is_letsplay"] = False
        self.assertEqual(cipl_play._trait_boost_line(state), "")

    def test_a_broken_state_costs_the_line_not_the_board(self):
        from handlers import cipl_play
        self.assertEqual(
            cipl_play._trait_boost_line({"is_letsplay": True, "bat_xi": None,
                                         "bowl_xi": None}), "")


class TraitMapTests(unittest.TestCase):
    def test_no_session_means_no_lookup_rather_than_a_crash(self):
        self.assertEqual(letsplay._side_trait_map(None, _pairs([80] * 11)), {})

    def test_the_bot_side_needs_no_lookup_at_all(self):
        bot_xi = [{"name": "B", "rating": 80, "traits": []}]
        self.assertEqual(letsplay._side_trait_map(object(), bot_xi), {})


if __name__ == "__main__":
    unittest.main()

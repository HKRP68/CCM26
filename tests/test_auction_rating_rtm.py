"""The squad rating rule, automatic RTM cards, /aretained and the folded purses.

  • **Rating rule** — "at least N players rated X or lower" in a finished squad
    is enforced as reachability: a signing is refused once the squad would owe
    the rule more players than it has slots left, and not before.
  • **RTM = retention spots − retained.** Cards are dealt automatically and
    follow every retention made or released until the auction opens.
  • **/aretained** lists every franchise's keeps; the live board folds the
    team purses behind a tap.
"""

import unittest

from tests.test_auction_retention import (  # noqa: F401 — module fixtures
    AuctionCase, setUpModule, tearDownModule, NOW)


def _by_name(players, name, rating=None):
    return next(p for p in players if p.name == name
                and (rating is None or p.rating == rating))


class RatingRuleTests(AuctionCase):
    """max_squad is 5; the rule asks for 3 players rated 84 or lower."""

    def setUp(self):
        super().setUp()
        self.season.max_retentions = 4
        self.A.set_rating_rules(self.session, self.season,
                                [{"max_rating": 84, "min_players": 3}])
        self.session.commit()

    def retain(self, name, rating=None):
        return self.A.retain(self.session, self.season, self.mumbai,
                             _by_name(self.players, name, rating), 100, now=NOW)

    def test_the_rule_reads_back(self):
        self.assertEqual([{"max_rating": 84, "min_players": 3}],
                         self.A.rating_rules(self.season))
        self.assertEqual("Min 3 players rated ≤84",
                         self.A.rating_rule_line(self.season))

    def test_a_signing_is_refused_only_once_the_rule_is_unreachable(self):
        self.retain("Virat Kohli", 97)
        self.retain("Jasprit Bumrah")
        # Two stars kept, three slots left, three players ≤84 still owed:
        # a third star would leave two slots for three owed players.
        with self.assertRaises(self.A.AuctionError) as caught:
            self.retain("Rashid Khan")
        self.assertIn("≤84", str(caught.exception))
        # A player the rule wants is still fine.
        self.retain("Tim David")
        self.assertEqual("≤84: 1/3",
                         self.A.rating_progress_line(self.session, self.season,
                                                     self.mumbai))

    def test_a_bid_meets_the_same_rule(self):
        self.retain("Virat Kohli", 97)
        self.retain("Jasprit Bumrah")
        self.build_pool()
        from models import AuctionLot
        lots = {lot.name: lot for lot in self.session.query(AuctionLot)
                .filter(AuctionLot.season_id == self.season.id).all()}
        # check_role_rules is the rule check validate_bid, RTM and every
        # signing share.
        with self.assertRaises(self.A.AuctionError):
            self.A.check_role_rules(self.session, self.season, self.mumbai,
                                    lots["Rashid Khan"])
        self.A.check_role_rules(self.session, self.season, self.mumbai,
                                lots["Tim David"])

    def test_a_rule_larger_than_the_squad_is_refused(self):
        with self.assertRaises(self.A.AuctionError):
            self.A.set_rating_rules(self.session, self.season,
                                    [{"max_rating": 84, "min_players": 9}])

    def test_the_rules_view_names_it(self):
        from services import auction_rich as AR
        _blocks, html_text = AR.rules_view(self.session, self.season)
        self.assertIn("Rating rule", html_text)
        self.assertIn("≤84", html_text)


class AutoRtmTests(AuctionCase):

    def setUp(self):
        super().setUp()
        self.season.max_retentions = 3
        self.A.set_rtm_rules(self.session, self.season, enabled=True)
        self.session.commit()

    def test_cards_are_retention_spots_minus_retained(self):
        self.assertEqual(3, self.mumbai.rtm_cards_total)
        self.A.retain(self.session, self.season, self.mumbai,
                      _by_name(self.players, "Virat Kohli", 97), 100, now=NOW)
        self.session.commit()
        self.session.refresh(self.mumbai)
        self.session.refresh(self.chennai)
        self.assertEqual(2, self.mumbai.rtm_cards_total)
        self.assertEqual(3, self.chennai.rtm_cards_total)

    def test_releasing_a_retention_gives_the_card_back(self):
        lot = self.A.retain(self.session, self.season, self.mumbai,
                            _by_name(self.players, "Virat Kohli", 97), 100,
                            now=NOW)
        self.session.commit()
        self.session.refresh(self.mumbai)
        self.A.unretain(self.session, self.season, self.mumbai, lot)
        self.session.commit()
        self.session.refresh(self.mumbai)
        self.assertEqual(3, self.mumbai.rtm_cards_total)

    def test_a_season_without_retention_uses_the_flat_count(self):
        self.season.max_retentions = 0
        self.A.set_rtm_rules(self.session, self.season, per_team=2)
        self.session.commit()
        self.assertEqual(2, self.mumbai.rtm_cards_total)

    def test_a_franchise_added_later_is_dealt_its_cards(self):
        late = self.A.create_franchise(self.session, self.season, "Delhi",
                                       owner_tg_id=999)
        self.assertEqual(3, late.rtm_cards_total)

    def test_rtm_off_deals_nothing(self):
        self.A.set_rtm_rules(self.session, self.season, enabled=False)
        self.mumbai.rtm_cards_total = 0
        self.session.commit()
        self.A.sync_rtm_cards(self.session, self.season)
        self.assertEqual(0, self.mumbai.rtm_cards_total)


class ViewTests(AuctionCase):

    def test_retained_view_lists_every_team(self):
        from services import auction_rich as AR
        self.season.max_retentions = 2
        self.session.commit()
        self.A.retain(self.session, self.season, self.mumbai,
                      _by_name(self.players, "Virat Kohli", 97), 100, now=NOW)
        self.session.commit()
        blocks, html_text = AR.retained_view(self.session, self.season)
        self.assertIn("Mumbai", html_text)
        self.assertIn("Chennai", html_text)
        self.assertIn("Virat Kohli", html_text)
        self.assertIn("Nobody retained", html_text)
        self.assertIn("<blockquote expandable>", html_text)
        details = [b for b in blocks if b.get("type") == "details"]
        self.assertEqual(2, len(details))

    def test_board_folds_the_purses(self):
        from services import auction_rich as AR
        self.build_pool()
        text = self.A.render_board(self.session, self.season)
        self.assertIn("Team Purses", text)
        self.assertIn("<blockquote expandable>", text)
        blocks = AR.board_blocks(self.session, self.season)
        purse = [b for b in blocks if b.get("type") == "details"]
        self.assertTrue(purse, "the purse table sits behind a tap")
        self.assertFalse(purse[0].get("is_open"))


if __name__ == "__main__":
    unittest.main()

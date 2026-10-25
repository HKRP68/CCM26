"""Warnings precede the bid that would make a squad impossible to finish."""
from types import SimpleNamespace

from tests import test_auction_features as F

setUpModule = F.setUpModule
tearDownModule = F.tearDownModule


class SquadWarningTests(F.FeatureCase):
    max_squad = 20

    def setUp(self):
        super().setUp()
        self.season.max_retentions = 20
        self.number = 0

    def player(self, rating=90, role='Batsman', country='India'):
        from models import Player
        self.number += 1
        values = dict(F.PLAYER_DEFAULTS)
        values.update(name=f'Warning {self.tag} {self.number}', rating=rating,
                      category=role, country=country)
        player = Player(**values)
        self.session.add(player)
        self.session.flush()
        return player

    def sign(self, count=1, **kwargs):
        for _ in range(count):
            self.A.retain(self.session, self.season, self.mumbai,
                          self.player(**kwargs), 20, now=F.NOW)
        self.session.flush()

    def warnings(self):
        return self.A.squad_warnings(self.session, self.season, self.mumbai)

    def test_last_four_of_twenty_slots_are_reserved_for_below_84(self):
        from models import AuctionBid
        self.A.set_rating_rules(self.session, self.season,
                                [{'max_rating': 83, 'min_players': 4}])
        self.sign(16)
        # Exercise a real rejected bid, then a real sale, for all four slots.
        pool = [self.player(84)] + [self.player(83) for _ in range(4)]
        self.A.add_players_to_pool(self.session, self.season, pool)
        self.session.commit()
        lot = self.start()
        for slots in range(4, 0, -1):
            self.assertIn(f'{slots} slots left — all must be rated ≤83.',
                          [row['message'] for row in self.warnings()])
            lot.rating = 84
            self.session.flush()
            purse = self.mumbai.purse_remaining_lakh
            before = self.session.query(AuctionBid).count()
            with self.assertRaisesRegex(self.A.AuctionError, '≤83'):
                self.A.place_bid(self.session, self.season, lot, self.mumbai,
                                 self.A.next_min_bid(self.season, lot), now=F.NOW)
            self.assertEqual(purse, self.mumbai.purse_remaining_lakh)
            self.assertEqual(before, self.session.query(AuctionBid).count())
            lot.rating = 83
            self.A.place_bid(self.session, self.season, lot, self.mumbai,
                             self.A.next_min_bid(self.season, lot), now=F.NOW)
            self.A.sell_lot(self.session, self.season, lot, now=F.NOW)
            lot = self.A.open_next_lot(self.session, self.season, now=F.NOW)
        self.assertEqual(20, self.mumbai.squad_size)
        self.assertFalse(any('rated ≤83' in row['message'] for row in self.warnings()))
        self.assertIn('Squad full', str(self.warnings()))

    def test_high_rated_players_stay_available_while_a_spare_slot_exists(self):
        self.A.set_rating_rules(self.session, self.season,
                                [{'max_rating': 83, 'min_players': 4}])
        self.sign(15)
        self.assertFalse(any(row['level'] == 'locked' for row in self.warnings()))
        self.A.check_role_rules(self.session, self.season, self.mumbai,
                               SimpleNamespace(category='Batsman', rating=99, is_overseas=False))

    def test_role_minimums_reserve_the_last_slots_across_roles(self):
        self.sign(16)
        self.A.set_role_rules(self.session, self.season,
                              {'Bowler': 2, 'All Rounder': 1, 'Wicket Keeper': 1}, {})
        self.assertIn('reserved for 1 All Rounder, 2 Bowler, 1 Wicket Keeper', str(self.warnings()))
        with self.assertRaisesRegex(self.A.AuctionError, 'no room'):
            self.A.check_role_rules(self.session, self.season, self.mumbai,
                                   SimpleNamespace(category='Batsman', rating=83, is_overseas=False))
        self.A.check_role_rules(self.session, self.season, self.mumbai,
                               SimpleNamespace(category='Bowler', rating=83, is_overseas=False))

    def test_role_maximum_and_zero_overseas_cap_are_visible_and_binding(self):
        self.sign(2)
        self.A.set_role_rules(self.session, self.season, {}, {'Batsman': 2, 'Bowler': 0})
        self.season.max_overseas = 0
        text = str(self.warnings())
        self.assertIn('Batsman: 2/2', text)
        self.assertIn('Bowler: 0/0', text)
        self.assertIn('domestic players only', text)
        with self.assertRaisesRegex(self.A.AuctionError, 'limit'):
            self.sign()

    def test_overseas_minimum_reserves_slots_and_accepts_an_overseas_signing(self):
        self.sign(18)
        self.season.min_overseas = 2
        self.assertIn('2 slots left — all must be overseas players', str(self.warnings()))
        with self.assertRaisesRegex(self.A.AuctionError, 'overseas'):
            self.sign()
        self.sign(country='England')
        self.assertIn('1 slots left — all must be overseas players', str(self.warnings()))

    def test_nested_rating_rules_count_a_player_toward_both(self):
        self.A.set_rating_rules(self.session, self.season,
            [{'max_rating': 83, 'min_players': 4}, {'max_rating': 80, 'min_players': 2}])
        self.sign(16)
        self.sign(2, rating=79)
        text = str(self.warnings())
        self.assertIn('all must be rated ≤83', text)
        self.assertNotIn('rated ≤80', text)

    def test_completed_or_unreachable_squad_asks_for_admin_review(self):
        self.sign(19)
        self.A.set_rating_rules(self.session, self.season,
                                [{'max_rating': 83, 'min_players': 4}])
        warning = self.warnings()[0]
        self.assertEqual('danger', warning['level'])
        self.assertIn('admin', warning['message'])
        self.season.status = self.A.STATUS_COMPLETED
        self.assertEqual('danger', self.warnings()[0]['level'])

    def test_purse_warning_and_minimum_squad_warning_are_visible(self):
        self.mumbai.purse_remaining_lakh = 1
        text = str(self.warnings())
        self.assertIn('Purse too low', text)
        self.assertIn('squad minimum', text)

    def test_board_rich_purse_and_squad_views_share_the_restriction(self):
        from services import auction_rich as AR
        self.A.set_rating_rules(self.session, self.season,
                                [{'max_rating': 83, 'min_players': 4}])
        self.sign(16)
        self.mumbai.name = '<Mumbai & Co>'
        warning = '4 slots left — all must be rated ≤83.'
        for text in (self.A.render_board(self.session, self.season),
                     self.A.render_purses(self.session, self.season),
                     self.A.render_squad(self.session, self.season, self.mumbai)):
            self.assertIn(warning, text)
            self.assertIn('&lt;Mumbai &amp; Co&gt;', text)
        self.assertIn(warning, str(AR.board_blocks(self.session, self.season)))
        self.assertIn(warning, str(AR.purses_view(self.session, self.season)[0]))

    def test_rtm_checks_role_and_rating_restrictions_again_before_matching(self):
        self.sign(16)
        self.A.set_rating_rules(self.session, self.season,
                                [{'max_rating': 83, 'min_players': 4}])
        self.A.add_players_to_pool(self.session, self.season, [self.players[0]])
        lot = self.start()
        lot.previous_franchise_id = self.mumbai.id
        lot.current_bidder_id = self.chennai.id
        lot.current_bid_lakh = lot.base_price_lakh
        self.season.rtm_enabled = True
        self.mumbai.rtm_cards_total = 1
        holder, reason = self.A.rtm_available(self.session, self.season, lot)
        self.assertIsNone(holder)
        self.assertIn('≤83', reason)
        lot.status, lot.rtm_stage = self.A.LOT_RTM_OFFERED, self.A.RTM_DECISION
        with self.assertRaisesRegex(self.A.AuctionError, '≤83'):
            self.A.rtm_decide(self.session, self.season, lot, self.mumbai, True, now=F.NOW)
        self.assertEqual(0, self.mumbai.rtm_cards_used)

    def test_invalid_squad_and_overseas_ranges_are_refused(self):
        for attribute, value in (('min_squad_size', 21), ('min_overseas', 9),
                                 ('min_overseas', -1), ('max_overseas', -1)):
            old = getattr(self.season, attribute)
            setattr(self.season, attribute, value)
            with self.assertRaises(self.A.AuctionError):
                self.A.validate_squad_limits(self.season)
            setattr(self.season, attribute, old)

    def test_existing_season_migration_preserves_rules_and_defaults_minimum_to_zero(self):
        import database
        from sqlalchemy import text
        self.session.commit()
        with database.engine.begin() as connection:
            connection.execute(text('ALTER TABLE auction_seasons DROP COLUMN min_overseas'))
        database._migrate_add_columns()
        database._migrate_add_columns()
        self.session.expire_all()
        self.assertEqual(0, self.season.min_overseas)
        self.assertEqual(self.max_overseas, self.season.max_overseas)
        self.assertEqual(self.max_squad, self.season.max_squad_size)

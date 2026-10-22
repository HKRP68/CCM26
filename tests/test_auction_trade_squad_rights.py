"""Pre-auction trades move current retention rights, never historical squads."""
from tests import test_auction_trade as F
from tests._previous_league import link_squads

setUpModule = F.setUpModule
tearDownModule = F.tearDownModule


class SquadRightsTests(F.TradeCase):
    def setUp(self):
        super().setUp()
        self.season.max_retentions = 3
        self.session.commit()
        link_squads(self.session, self.A, self.season,
                    {'Mumbai': self.players[:2], 'Chennai': self.players[2:4]})
        self.mine, self.theirs = self.players[0], self.players[2]

    def rights(self, team, player):
        return next(l for l in self.T.trade_squad(self.session, self.season, team)
                    if l.player_id == player.id)

    def transfer(self, lot, other=None):
        return self.complete(self.offer(self.mi, self.csk, [lot], [other] if other else []))

    def test_unretained_players_appear_and_transfer_without_signing_or_charging(self):
        lot = self.rights(self.mi, self.mine)
        other = self.rights(self.csk, self.theirs)
        trade = self.transfer(lot, other)
        self.assertEqual(self.T.STATUS_COMPLETED, trade.status)
        self.assertEqual(self.A.LOT_QUEUED, lot.status)
        self.assertEqual(self.A.ACQ_TRADE_RIGHTS, lot.acquisition)
        self.assertEqual(self.csk.id, lot.sold_to_id)
        self.assertEqual(self.csk.id, self.A.previous_squad_map(self.session, self.season)[self.mine.id].id)
        self.assertEqual([], self.A.squad(self.session, self.mi.id))
        self.assertEqual([], self.A.squad(self.session, self.csk.id))
        self.assertEqual(0, self.mi.retained_count)
        self.assertEqual(0, self.csk.retained_count)
        self.assertEqual(10000, self.mi.purse_remaining_lakh)
        self.assertEqual(10000, self.csk.purse_remaining_lakh)
        self.assert_ledger_agrees()

    def test_new_holder_can_retain_at_range_cost_and_old_holder_cannot(self):
        from services import retention_negotiation as RN
        self.A.set_range_rules(self.session, self.season, [dict(
            min_rating=0, max_rating=999, min_count=0, max_count=2, price_lakh=1700)])
        RN.set_mode(self.session, self.season, RN.MODE_RANGES)
        lot = self.rights(self.mi, self.mine)
        self.transfer(lot)
        with self.assertRaises(self.A.AuctionError):
            self.A.retain(self.session, self.season, self.mi, self.mine, by_tg_id=F.ALICE)
        self.session.rollback()
        self.A.retain(self.session, self.season, self.csk, self.mine, by_tg_id=F.BOB)
        self.assertEqual(1700, lot.sold_price_lakh)
        self.assertEqual(8300, self.csk.purse_remaining_lakh)
        self.assertEqual(1, self.csk.retained_count)
        self.session.flush()
        self.assert_ledger_agrees()

    def test_releasing_traded_retained_player_does_not_restore_old_team_claim(self):
        from services import retention_negotiation as RN
        lot = self.A.retain(self.session, self.season, self.mi, self.mine, 1500)
        self.session.commit()
        self.transfer(lot)
        self.assertEqual(0, self.mi.retained_count)
        self.assertEqual(1, self.csk.retained_count)
        self.assertEqual([self.mine.id], [p.id for p in RN.previous_squad(self.session, self.season, self.csk)
                                         if p.id == self.mine.id])
        self.A.release_by_owner(self.session, self.season, lot, F.BOB)
        self.session.commit()
        self.assertEqual(self.A.LOT_QUEUED, lot.status)
        self.assertIsNone(lot.sold_to_id)
        self.assertIsNone(lot.previous_franchise_id)
        self.assertNotIn(self.mine.id, self.A.previous_squad_map(self.session, self.season))
        for team in (self.mi, self.csk):
            with self.assertRaises(self.A.AuctionError):
                RN.find_retention_player(self.session, self.season, team, self.mine.name)
        self.assertEqual(0, self.csk.retained_count)
        self.assertEqual(10000, self.csk.purse_remaining_lakh)
        self.assert_ledger_agrees()

    def test_legacy_released_trade_also_stays_in_open_pool(self):
        lot = self.A.retain(self.session, self.season, self.mi, self.mine, 1500)
        self.session.commit()
        self.transfer(lot)
        self.A.release_by_owner(self.session, self.season, lot, F.BOB)
        lot.acquisition = self.A.ACQ_AUCTION
        lot.previous_franchise_id = self.mi.id
        self.session.flush()
        self.assertNotIn(self.mine.id, self.A.previous_squad_map(self.session, self.season))
        self.assertNotIn(lot.id, [l.id for l in self.T.trade_squad(self.session, self.season, self.mi)])

    def test_release_unretained_traded_rights_has_no_refund_or_old_claim(self):
        lot = self.rights(self.mi, self.mine)
        self.transfer(lot)
        with self.assertRaises(self.A.AuctionError):
            self.A.release_by_owner(self.session, self.season, lot, F.ALICE)
        self.A.release_by_owner(self.session, self.season, lot, F.BOB)
        self.assertEqual(10000, self.csk.purse_remaining_lakh)
        self.assertIsNone(lot.sold_to_id)
        self.assertNotIn(self.mine.id, self.A.previous_squad_map(self.session, self.season))
        self.assert_ledger_agrees()

    def test_rights_reverse_restores_ownership_and_cash(self):
        lot = self.rights(self.mi, self.mine)
        trade = self.complete(self.offer(self.mi, self.csk, [lot], cash=-100))
        self.T.reverse(self.session, trade, by_tg_id=F.BOSS)
        self.assertEqual(self.mi.id, lot.sold_to_id)
        self.assertEqual(self.mi.id, self.A.previous_squad_map(self.session, self.season)[self.mine.id].id)
        self.assertEqual(10000, self.mi.purse_remaining_lakh)
        self.assert_ledger_agrees()

    def test_used_or_released_rights_cannot_be_reversed(self):
        lot = self.rights(self.mi, self.mine)
        trade = self.transfer(lot)
        self.A.retain(self.session, self.season, self.csk, self.mine, 1500)
        with self.assertRaisesRegex(self.A.AuctionError, 'rights have been used'):
            self.T.reverse(self.session, trade, by_tg_id=F.BOSS)
        self.assertEqual(self.csk.id, lot.sold_to_id)

    def test_releasing_entire_previous_squad_does_not_create_expansion_team(self):
        for lot in self.T.trade_squad(self.session, self.season, self.mi):
            self.A.release_by_owner(self.session, self.season, lot, F.ALICE)
        self.assertEqual([], self.A.expansion_franchises(self.session, self.season))
        self.assertNotIn(self.mi.id, {f.id for f in self.A.previous_squad_map(self.session, self.season).values()})

    def test_arbitrary_pool_player_cannot_be_put_in_trade(self):
        lot = self.A._signing_lot(self.session, self.season, self.players[-1])
        trade = self.T.open_trade(self.session, self.season, self.mi, self.csk)
        with self.assertRaises(self.A.AuctionError):
            self.T.toggle_lot(self.session, trade, lot.id)

    def test_picker_lists_remaining_squad_and_reuses_lots(self):
        from handlers import auction_trade as H
        from models import AuctionLot
        trade = self.T.open_trade(self.session, self.season, self.mi, self.csk)
        keyboard = H.builder_keyboard(self.session, self.season, trade, 'a')
        texts = [button.text for row in keyboard.inline_keyboard for button in row]
        self.assertTrue(any('rights' in text for text in texts))
        count = self.session.query(AuctionLot).filter_by(season_id=self.season.id).count()
        H.builder_keyboard(self.session, self.season, trade, 'a')
        self.assertEqual(count, self.session.query(AuctionLot).filter_by(season_id=self.season.id).count())
        self.assert_ledger_agrees()

    def test_unretained_rights_unavailable_after_retention_closes(self):
        lot = self.rights(self.mi, self.mine)
        self.A.lock_retention(self.session, self.season)
        trade = self.T.open_trade(self.session, self.season, self.mi, self.csk)
        self.assertNotIn(lot.id, [l.id for l in self.T.trade_squad(self.session, self.season, self.mi)])
        with self.assertRaises(self.A.AuctionError):
            self.T.toggle_lot(self.session, trade, lot.id)

    def test_pending_retention_offer_must_be_resolved_before_rights_trade(self):
        lot = self.rights(self.mi, self.mine)
        self.A.offer_retention(self.session, self.season, self.mi, self.mine, 1500)
        self.session.commit()
        with self.assertRaisesRegex(self.A.AuctionError, 'resolve before trading'):
            self.offer(self.mi, self.csk, [lot])
        self.assertEqual(self.mi.id, lot.sold_to_id)

    def test_dynamic_talks_follow_transferred_rights(self):
        from services import retention_negotiation as RN
        RN.set_mode(self.session, self.season, RN.MODE_DYNAMIC)
        # A card inside the default dynamic slots.
        self.mine = self.players[1]
        lot = self.rights(self.mi, self.mine)
        self.transfer(lot)
        talk = RN.start_talk(self.session, self.season, self.csk, self.mine,
                             by_tg_id=F.BOB)
        self.assertEqual(self.csk.id, talk.franchise_id)
        with self.assertRaises(self.A.AuctionError):
            RN.start_talk(self.session, self.season, self.mi, self.mine, by_tg_id=F.ALICE)

    def test_queued_rights_remain_available_to_the_open_draft_after_closure(self):
        lot = self.rights(self.mi, self.mine)
        self.transfer(lot)
        self.A.lock_retention(self.session, self.season)
        self.assertEqual(self.A.LOT_QUEUED, lot.status)
        self.assertNotIn(lot, self.A.squad(self.session, self.csk.id))
        self.assert_ledger_agrees()

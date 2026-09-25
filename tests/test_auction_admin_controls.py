"""The auctioneer's player controls: reinstate, force next, unsold in bulk,
and the bid ladder.

What is pinned here:

  • **/areinstate is /awithdraw's opposite.** A withdrawn (or unsold) player
    goes back to the tail of the queue with no trace of his last time on the
    block; a sold or live one is refused.
  • **/aforce puts one player next.** Live with an empty block, he opens at
    once; otherwise he is first in the queue and nothing on the block moves.
    A withdrawn player is brought back on the way.
  • **/aunsold 67, 88 only touches players nobody has bought or bid on**, and
    names every one it skipped rather than refusing the batch.
  • **The bid ladder can be set by command and website**, is parsed once, and
    applies from the next raise.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_auction_bidding import (  # noqa: E402,F401  (module fixtures)
    ALICE, NOW, AuctionCase, setUpModule, tearDownModule)


class ReinstateTests(AuctionCase):

    def setUp(self):
        super().setUp()
        self.build_pool()

    def test_a_withdrawn_player_goes_back_to_the_tail_of_the_queue(self):
        lots = self.A.queued_lots(self.session, self.season)
        first, last = lots[0], lots[-1]
        self.A.withdraw_lot(self.session, self.season, first)
        self.session.commit()
        lot = self.A.reinstate_lot(self.session, self.season, first)
        self.session.commit()
        self.assertEqual(self.A.LOT_QUEUED, lot.status)
        self.assertGreater(lot.lot_no, last.lot_no)

    def test_an_unsold_player_can_be_reinstated_too(self):
        lot = self.start()
        self.A.pass_lot(self.session, self.season, lot)
        self.session.commit()
        lot = self.A.reinstate_lot(self.session, self.season, lot)
        self.assertEqual(self.A.LOT_QUEUED, lot.status)

    def test_a_queued_or_sold_player_is_refused(self):
        queued = self.A.queued_lots(self.session, self.season)[1]
        with self.assertRaises(self.A.AuctionError):
            self.A.reinstate_lot(self.session, self.season, queued)
        lot = self.start()
        self.A.place_bid(self.session, self.season, lot, self.mumbai,
                         lot.base_price_lakh, now=NOW, by_tg_id=ALICE)
        lot = self.A.sell_lot(self.session, self.season, lot, now=NOW)
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.reinstate_lot(self.session, self.season, lot)
        self.assertIn("undo the sale", str(caught.exception).lower())


class ForceNextTests(AuctionCase):

    def setUp(self):
        super().setUp()
        self.build_pool()

    def test_forced_before_the_start_is_the_first_lot(self):
        wanted = self.A.queued_lots(self.session, self.season)[-1]
        lot, opened = self.A.force_next(self.session, self.season, wanted)
        self.session.commit()
        self.assertFalse(opened)
        self.assertEqual(wanted.id, self.start().id)

    def test_forced_while_a_lot_is_up_waits_for_it(self):
        standing = self.start()
        wanted = self.A.queued_lots(self.session, self.season)[-1]
        lot, opened = self.A.force_next(self.session, self.season, wanted,
                                        now=NOW)
        self.session.commit()
        self.assertFalse(opened)
        self.assertEqual(standing.id,
                         self.A.current_lot(self.session, self.season).id)
        self.assertEqual(wanted.id,
                         self.A.next_queued(self.session, self.season.id).id)

    def test_forced_on_an_empty_block_opens_at_once(self):
        standing = self.start()
        self.A.pass_lot(self.session, self.season, standing)
        self.session.commit()
        wanted = self.A.queued_lots(self.session, self.season)[-1]
        lot, opened = self.A.force_next(self.session, self.season, wanted,
                                        now=NOW)
        self.session.commit()
        self.assertTrue(opened)
        self.assertEqual(self.A.LOT_ON_BLOCK, lot.status)
        self.assertEqual(wanted.id, self.season.current_lot_id)

    def test_a_withdrawn_player_is_brought_back_and_put_first(self):
        wanted = self.A.queued_lots(self.session, self.season)[2]
        self.A.withdraw_lot(self.session, self.season, wanted)
        self.session.commit()
        self.A.force_next(self.session, self.season, wanted)
        self.session.commit()
        self.assertEqual(wanted.id,
                         self.A.next_queued(self.session, self.season.id).id)

    def test_the_lot_on_the_block_cannot_be_forced(self):
        standing = self.start()
        with self.assertRaises(self.A.AuctionError):
            self.A.force_next(self.session, self.season, standing)


class BulkUnsoldTests(AuctionCase):

    def setUp(self):
        super().setUp()
        self.build_pool()

    def test_lot_numbers_and_names_both_resolve(self):
        lots = self.A.queued_lots(self.session, self.season)
        found, misses = self.A.find_lots(
            self.session, self.season,
            f"{lots[0].lot_no}, #{lots[1].lot_no} {lots[2].lot_no}, "
            f"Rinku, 999, Nobody")
        self.assertEqual([lots[0].id, lots[1].id, lots[2].id],
                         [lot.id for lot in found[:3]])
        self.assertEqual("Rinku Singh", found[3].name)
        self.assertEqual(["999", "Nobody"], [token for token, _ in misses])

    def test_queued_players_go_unsold_without_opening(self):
        lots = self.A.queued_lots(self.session, self.season)
        done, skipped = self.A.mark_unsold(self.session, self.season,
                                           lots[2:4])
        self.session.commit()
        self.assertEqual(2, len(done))
        self.assertEqual([], skipped)
        for lot in lots[2:4]:
            self.session.refresh(lot)
            self.assertEqual(self.A.LOT_UNSOLD, lot.status)

    def test_sold_and_bid_on_players_are_skipped_by_name(self):
        lot = self.start()
        self.A.place_bid(self.session, self.season, lot, self.mumbai,
                         lot.base_price_lakh, now=NOW, by_tg_id=ALICE)
        sold = self.A.sell_lot(self.session, self.season, lot, now=NOW)
        live = self.A.open_next_lot(self.session, self.season, now=NOW)
        self.A.place_bid(self.session, self.season, live, self.mumbai,
                         live.base_price_lakh, now=NOW, by_tg_id=ALICE)
        waiting = self.A.next_queued(self.session, self.season.id)
        self.session.commit()

        done, skipped = self.A.mark_unsold(self.session, self.season,
                                           [sold, live, waiting])
        self.session.commit()
        self.assertEqual([waiting.id], [lot.id for lot in done])
        reasons = {lot.id: why for lot, why in skipped}
        self.assertIn("Mumbai", reasons[sold.id])
        self.assertIn("standing bid", reasons[live.id])
        self.session.refresh(live)
        self.assertEqual(self.A.LOT_ON_BLOCK, live.status)

    def test_emptying_the_queue_in_setup_does_not_finish_the_auction(self):
        lots = self.A.queued_lots(self.session, self.season)
        self.A.mark_unsold(self.session, self.season, lots)
        self.session.commit()
        self.assertEqual(self.A.STATUS_SETUP, self.season.status)
        self.assertFalse(self.season.accelerated_done)

    def test_a_lot_opened_after_it_was_read_is_not_marked_unsold(self):
        from models import AuctionLot
        lot = self.A.queued_lots(self.session, self.season)[0]
        (self.session.query(AuctionLot).filter(AuctionLot.id == lot.id)
         .update({"status": self.A.LOT_ON_BLOCK},
                 synchronize_session=False))
        done, skipped = self.A.mark_unsold(self.session, self.season, [lot])
        self.assertEqual([], done)
        self.assertIn("just gone on the block", skipped[0][1])

    def test_the_lot_on_the_block_is_passed_when_nobody_bid(self):
        live = self.start()
        done, _ = self.A.mark_unsold(self.session, self.season, [live])
        self.session.commit()
        self.session.refresh(live)
        self.assertEqual(self.A.LOT_UNSOLD, live.status)
        self.assertIsNone(self.season.current_lot_id)


class IncrementTests(AuctionCase):

    def test_a_ladder_parses_sorts_and_gets_a_catch_all(self):
        rules = self.A.parse_increment_rules("5:20L, 2:10L, 10 = 25L")
        self.assertEqual(
            [{"upto_lakh": 200, "step_lakh": 10},
             {"upto_lakh": 500, "step_lakh": 20},
             {"upto_lakh": 1000, "step_lakh": 25},
             {"upto_lakh": 0, "step_lakh": 25}], rules)

    def test_a_single_amount_is_a_flat_step(self):
        self.assertEqual([{"upto_lakh": 0, "step_lakh": 25}],
                         self.A.parse_increment_rules("25L"))

    def test_a_step_bigger_than_its_band_is_refused(self):
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.parse_increment_rules("2:10")
        self.assertIn("10L", str(caught.exception))

    def test_two_catch_alls_are_refused(self):
        with self.assertRaises(self.A.AuctionError):
            self.A.parse_increment_rules("25L, 50L")

    def test_saving_changes_the_next_minimum_bid(self):
        self.build_pool()
        lot = self.start()
        self.A.place_bid(self.session, self.season, lot, self.mumbai,
                         lot.base_price_lakh, now=NOW, by_tg_id=ALICE)
        self.A.set_increment_rules(self.session, self.season,
                                   self.A.parse_increment_rules("1L"))
        self.assertEqual(lot.current_bid_lakh + 1,
                         self.A.next_min_bid(self.season, lot))
        self.A.set_increment_rules(self.session, self.season, None)
        self.assertEqual(self.A.DEFAULT_INCREMENT_RULES,
                         self.A.increment_rules(self.season))


if __name__ == "__main__":
    unittest.main()

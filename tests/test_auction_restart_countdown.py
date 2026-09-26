"""/arestart and the hammer countdown.

What is pinned here:

  • **A restart puts everything back.** Every player bought, passed or waiting
    goes back into the queue in the order the auction opened with; every purse
    equals its ledger and its opening amount again; the first player is on the
    block. Retained players stay signed.
  • **The countdown speaks before the hammer, in new messages** — "Selling X
    to Team for ₹…" with the first number, then one number a second — and
    brings the hammer down itself at the deadline. A bid that moves the
    deadline ends the count; one that does not re-announces the new leader.
"""

import asyncio
import unittest
from datetime import timedelta

from tests.test_auction_bidding import (  # noqa: F401 — module fixtures
    ALICE, BOB, NOW, AuctionCase, FakeBot, setUpModule, tearDownModule,
)


class RestartTests(AuctionCase):

    bid_gap = 0

    def setUp(self):
        super().setUp()
        self.season.auto_accelerated = 0
        self.session.commit()
        self.build_pool()
        self.first = self.start()
        self.session.commit()
        self.opening = [lot.id for lot in self.queue()]

    def queue(self):
        from models import AuctionLot
        return (self.session.query(AuctionLot)
                .filter(AuctionLot.season_id == self.season.id,
                        AuctionLot.status.in_((self.A.LOT_QUEUED,)
                                              + self.A.LOT_LIVE))
                .order_by(AuctionLot.lot_no.asc()).all())

    def buy(self, lot, franchise, tg_id):
        self.A.place_bid(self.session, self.season, lot, franchise,
                         lot.base_price_lakh, now=NOW, by_tg_id=tg_id)
        self.A.sell_lot(self.session, self.season, lot, now=NOW)
        self.session.commit()
        return self.A.open_next_lot(self.session, self.season, now=NOW)

    def test_everything_comes_back_from_the_first_player(self):
        lot = self.buy(self.first, self.mumbai, ALICE)
        lot = self.buy(lot, self.chennai, BOB)
        self.A.pass_lot(self.session, self.season, lot)
        self.session.commit()
        lot = self.A.open_next_lot(self.session, self.season, now=NOW)
        self.A.place_bid(self.session, self.season, lot, self.mumbai,
                         lot.base_price_lakh, now=NOW, by_tg_id=ALICE)
        self.session.commit()

        first, count = self.A.restart_auction(self.session, self.season,
                                              now=NOW)
        self.session.commit()

        self.assertEqual(self.A.STATUS_LIVE, self.season.status)
        self.assertEqual(self.first.id, first.id)
        self.assertEqual(self.A.LOT_ON_BLOCK, first.status)
        self.assertIsNone(first.current_bidder_id)
        self.assertEqual(len(self.opening), count)
        self.assertEqual(self.opening, [lot.id for lot in self.queue()])
        for franchise in (self.mumbai, self.chennai):
            self.session.refresh(franchise)
            self.assertEqual(self.purse_lakh, franchise.purse_remaining_lakh)
            self.assertEqual(0, franchise.squad_size)
        self.assert_ledger_agrees("after a restart")
        self.assertEqual([], self.A.sold_lots(self.session, self.season.id))

    def test_a_completed_auction_restarts_with_its_original_sets(self):
        from models import AuctionLot
        lot, guard = self.first, 0
        while lot is not None and guard < 30:
            self.A.pass_lot(self.session, self.season, lot)
            self.session.commit()
            lot = self.A.open_next_lot(self.session, self.season, now=NOW)
            guard += 1
        self.assertEqual(self.A.STATUS_COMPLETED, self.season.status)
        sets = {lot.id: lot.set_name for lot in self.session.query(AuctionLot)
                .filter(AuctionLot.season_id == self.season.id)}
        self.A.relist_all(self.session, self.season,
                          set_name=self.A.ACCELERATED_SET)
        self.session.commit()

        self.A.restart_auction(self.session, self.season, now=NOW)
        self.session.commit()
        self.assertEqual(self.opening, [lot.id for lot in self.queue()])
        for lot in self.queue():
            self.assertEqual(sets[lot.id], lot.set_name)
            self.assertEqual(0, lot.times_unsold)
        self.assertEqual(0, self.season.accelerated_done)

    def test_refused_once_published_or_before_it_starts(self):
        from datetime import datetime
        self.season.published_at = datetime.utcnow()
        with self.assertRaises(self.A.AuctionError):
            self.A.restart_auction(self.session, self.season, now=NOW)
        self.season.published_at = None
        self.season.status = self.A.STATUS_SETUP
        with self.assertRaises(self.A.AuctionError):
            self.A.restart_auction(self.session, self.season, now=NOW)

    def test_the_command_asks_for_confirmation_first(self):
        from types import SimpleNamespace
        from handlers import auction as H
        replies = []

        async def reply_text(text, **kwargs):
            replies.append(text)

        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=self.season.chat_id,
                                           type="supergroup"),
            effective_user=SimpleNamespace(id=ALICE),
            effective_message=SimpleNamespace(reply_text=reply_text),
            message=SimpleNamespace(reply_text=reply_text))
        self.buy(self.first, self.mumbai, ALICE)
        original = H._require_admin

        async def yes(update):
            return True

        H._require_admin = yes
        try:
            asyncio.run(H.arestart_handler(update, SimpleNamespace(args=[])))
            self.assertIn("/arestart confirm", replies[-1])
            self.session.expire_all()
            self.assertEqual(1, len(self.A.sold_lots(self.session,
                                                     self.season.id)))
            self.session.commit()      # let the handler's session write
            asyncio.run(H.arestart_handler(
                update, SimpleNamespace(args=["confirm"])))
        finally:
            H._require_admin = original
        self.session.expire_all()
        self.assertEqual([], self.A.sold_lots(self.session, self.season.id))


class CountdownTests(AuctionCase):

    bid_gap = 0

    def setUp(self):
        super().setUp()
        from services import auction_scheduler as S
        self.S = S
        self._card = S.SEND_LOT_CARD
        S.SEND_LOT_CARD = False
        self.season.auto_accelerated = 0
        self.session.commit()
        self.build_pool()
        self.lot = self.start()
        self.session.commit()
        self.bot = FakeBot()
        self.clock = [NOW]

    def tearDown(self):
        self.S.SEND_LOT_CARD = self._card
        super().tearDown()

    def run_countdown(self, on_sleep=None):
        async def sleep(seconds):
            self.clock[0] += timedelta(seconds=seconds)
            if on_sleep:
                on_sleep(self.clock[0])

        return asyncio.run(self.S.run_countdown(
            self.bot, self.season.id, self.lot.id, self.lot.deadline_at,
            self.A.countdown_seconds(self.season), sleep=sleep,
            clock=lambda: self.clock[0]))

    def test_selling_counts_three_two_one_then_sold(self):
        self.A.place_bid(self.session, self.season, self.lot, self.mumbai,
                         self.lot.base_price_lakh, now=NOW, by_tg_id=ALICE)
        self.session.commit()
        self.assertTrue(self.run_countdown())
        said = self.bot.sent
        head = next(i for i, text in enumerate(said) if "Selling" in text)
        self.assertIn("Mumbai", said[head])
        self.assertIn(self.lot.name, said[head])
        self.assertTrue(said[head].endswith("<b>3</b>"))
        # One message, edited in place: 3 → 2 → 1, then SOLD.
        counts = [text for _mid, text in self.bot.edits if "Selling" in text]
        self.assertTrue(counts[0].endswith("<b>2</b>"))
        self.assertTrue(counts[1].endswith("<b>1</b>"))
        self.assertFalse([t for t in said if t in ("<b>2</b>", "<b>1</b>")])
        self.assertTrue(any("SOLD" in text for text in said[head + 1:]))
        self.session.expire_all()
        self.assertEqual(self.A.LOT_SOLD, self.lot.status)

    def test_no_bids_counts_down_to_unsold(self):
        self.assertTrue(self.run_countdown())
        self.assertIn("UNSOLD", self.bot.sent[0])
        self.assertTrue(any("UNSOLD" in text for text in self.bot.sent[1:]))
        self.session.expire_all()
        self.assertEqual(self.A.LOT_UNSOLD, self.lot.status)

    def test_a_bid_that_moves_the_deadline_ends_the_count(self):
        deadline = self.lot.deadline_at

        def bump(now):
            if now >= deadline - timedelta(seconds=2):
                from database import get_session
                from models import AuctionLot
                other = get_session()
                row = other.query(AuctionLot).get(self.lot.id)
                if row.deadline_at == deadline:
                    row.deadline_at = deadline + timedelta(seconds=10)
                    other.commit()
                other.close()

        self.assertFalse(self.run_countdown(bump))
        self.session.expire_all()
        self.assertEqual(self.A.LOT_ON_BLOCK, self.lot.status)

    def test_the_countdown_is_editable_and_can_be_off(self):
        self.assertEqual(5, self.A.set_countdown(self.session, self.season, "5"))
        self.assertEqual(0, self.A.set_countdown(self.session, self.season,
                                                 "off"))
        with self.assertRaises(self.A.AuctionError):
            self.A.set_countdown(self.session, self.season, "99")
        self.session.commit()

        async def arm():
            return self.S.maybe_start_countdown(
                self.bot, self.session, self.season,
                now=self.lot.deadline_at - timedelta(seconds=2))

        self.assertIsNone(asyncio.run(arm()), "off means no countdown")


class BidGapTests(AuctionCase):
    """After any bid, nobody may bid again for a few seconds."""

    bid_gap = 3

    def setUp(self):
        super().setUp()
        self.build_pool()
        self.lot = self.start()
        self.session.commit()

    def bid(self, franchise, tg_id, now):
        amount = self.A.next_min_bid(self.season, self.lot)
        self.lot = self.A.place_bid(self.session, self.season, self.lot,
                                    franchise, amount, now=now, by_tg_id=tg_id)
        self.session.commit()
        return self.lot

    def test_a_bid_inside_the_gap_is_told_who_holds_the_lot(self):
        self.bid(self.mumbai, ALICE, NOW)
        with self.assertRaises(self.A.BidTooSoon) as caught:
            self.bid(self.chennai, BOB, NOW + timedelta(seconds=2))
        said = str(caught.exception)
        self.assertIn("Current bid holder", said)
        self.assertIn(f"Player: {self.lot.name}", said)
        self.assertIn("Team: Mumbai", said)
        self.session.rollback()
        self.assertEqual(self.mumbai.id, self.lot.current_bidder_id)

    def test_after_the_gap_any_team_may_answer(self):
        self.bid(self.mumbai, ALICE, NOW)
        lot = self.bid(self.chennai, BOB, NOW + timedelta(seconds=3))
        self.assertEqual(self.chennai.id, lot.current_bidder_id)

    def test_a_late_bid_leaves_time_to_answer_it(self):
        late = self.lot.deadline_at - timedelta(seconds=1)
        lot = self.bid(self.mumbai, ALICE, late)
        self.assertGreaterEqual(lot.deadline_at,
                                late + timedelta(seconds=self.bid_gap + 1))

    def test_the_gap_is_editable_and_can_be_off(self):
        self.assertEqual(0, self.A.set_bid_gap(self.session, self.season,
                                               "off"))
        self.session.commit()
        self.bid(self.mumbai, ALICE, NOW)
        lot = self.bid(self.chennai, BOB, NOW)
        self.assertEqual(self.chennai.id, lot.current_bidder_id)
        with self.assertRaises(self.A.AuctionError):
            self.A.set_bid_gap(self.session, self.season, "60")


if __name__ == "__main__":
    unittest.main()

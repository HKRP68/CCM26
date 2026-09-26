"""The staged lot clock — ``/atimer 60 40 20 10 5``.

What is pinned here:

  • **/atimer parses both shapes**: one number keeps the classic clock, five
    (the last may be ``off``) switch to the staged one, bad ordering is refused
    by name, and ``classic`` goes back.
  • **The reset**: a bid with more than 40s left keeps the clock, a bid with
    less puts it back to 40s — every time, with no cap — and the bid line says
    "Clock back to 40s".
  • **The warnings**: at 20s and 10s the room is told who the player is going
    to, with the bid buttons on it, once per stage; a bid re-arms both, and a
    lot with no bids is warned it is going UNSOLD.
  • **The final count is ONE message edited 5 → 1**, then SOLD; a bid that
    resets the clock turns it into "clock back to 40s".
  • **Classic seasons behave exactly as before.**
"""

import asyncio
import os
import sys
import unittest
from datetime import timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_auction_bidding import (  # noqa: E402,F401  (module fixtures)
    ALICE, BOB, NOW, AuctionCase, setUpModule, tearDownModule)


class RecordingBot:
    def __init__(self):
        self.sent = []       # (text, reply_markup)
        self.edits = []      # (message_id, text, reply_markup)
        self.stripped = []
        self.next_id = 900

    async def send_message(self, chat_id=None, text="", reply_markup=None,
                           **kwargs):
        self.next_id += 1
        self.sent.append((text, reply_markup))
        return SimpleNamespace(message_id=self.next_id)

    async def edit_message_text(self, chat_id=None, message_id=None, text="",
                                reply_markup=None, **kwargs):
        self.edits.append((message_id, text, reply_markup))
        return True

    async def edit_message_reply_markup(self, chat_id=None, message_id=None,
                                        reply_markup=None, **kwargs):
        self.stripped.append(message_id)
        return True

    async def pin_chat_message(self, **kwargs):
        return True

    async def unpin_chat_message(self, **kwargs):
        return True


class StagedCase(AuctionCase):

    def setUp(self):
        super().setUp()
        from services import auction_scheduler as S
        self.S = S
        self._card = S.SEND_LOT_CARD
        S.SEND_LOT_CARD = False
        S._live_buttons.clear()
        self.A.set_timer(self.session, self.season, "60 40 20 10 5")
        self.season.auto_accelerated = 0
        self.session.commit()
        self.build_pool()
        self.lot = self.start(now=NOW)
        self.session.commit()

    def tearDown(self):
        self.S.SEND_LOT_CARD = self._card
        self.S._live_buttons.clear()
        super().tearDown()

    def fresh(self):
        from models import AuctionLot
        self.session.expire_all()
        return (self.session.query(AuctionLot)
                .filter(AuctionLot.id == self.lot.id).first())

    def bid(self, franchise, tg_id, at):
        lot = self.fresh()
        self.A.place_bid(self.session, self.season, lot, franchise,
                         self.A.next_min_bid(self.season, lot), now=at,
                         by_tg_id=tg_id)
        self.session.commit()
        return self.fresh()

    def tick(self, bot, at):
        asyncio.run(self.S._tick_one(SimpleNamespace(bot=bot), self.session,
                                     self.season, at))


class TimerCommandTests(StagedCase):

    def test_five_numbers_switch_to_the_staged_clock(self):
        self.assertEqual((60, 40, 20, 10, 5), self.A.staged_clock(self.season))
        self.assertEqual(60, (self.lot.deadline_at - NOW).total_seconds())
        self.assertIn("resets to 40s", self.A.staged_summary(self.season))

    def test_the_count_can_be_off_and_the_order_is_enforced(self):
        self.A.set_timer(self.session, self.season, "90 30 15 8 off")
        self.assertEqual((90, 30, 15, 8, 0), self.A.staged_clock(self.season))
        for bad in ("60 70 20 10 5", "60 40 40 10 5", "60 40 20 20 5",
                    "60 40 20 10 10", "60 40 20 10 99", "x 40 20 10 5",
                    "60 40"):
            with self.assertRaises(self.A.AuctionError, msg=bad):
                self.A.set_timer(self.session, self.season, bad)

    def test_one_number_and_classic(self):
        self.A.set_timer(self.session, self.season, "75")
        self.assertEqual(75, self.A.staged_clock(self.season)[0])
        with self.assertRaises(self.A.AuctionError):
            self.A.set_timer(self.session, self.season, "30")  # under reset 40
        self.A.set_timer(self.session, self.season, "classic")
        self.assertIsNone(self.A.staged_clock(self.season))
        self.assertEqual(30, self.A.set_timer(self.session, self.season, "30"))

    def test_the_countdown_must_sit_under_the_2nd_warning(self):
        with self.assertRaises(self.A.AuctionError):
            self.A.set_countdown(self.session, self.season, "10")
        self.assertEqual(3, self.A.set_countdown(self.session, self.season, "3"))

    def test_new_auctions_default_to_the_staged_clock(self):
        season = self.A.create_season(self.session, "Staged default")
        self.assertIsNone(self.A.staged_clock(season))
        self.A.apply_staged_defaults(season)
        self.assertEqual(self.A.STAGED_DEFAULTS, self.A.staged_clock(season))

    def test_the_stage_thresholds_follow_the_season(self):
        self.assertEqual(0, self.A.going_stage_for(21, self.season))
        self.assertEqual(1, self.A.going_stage_for(20, self.season))
        self.assertEqual(2, self.A.going_stage_for(10, self.season))
        self.assertEqual(0, self.A.going_stage_for(15))   # classic: 10 / 5


class ResetTests(StagedCase):

    def test_an_early_bid_keeps_the_clock(self):
        deadline = self.lot.deadline_at
        lot = self.bid(self.mumbai, ALICE, NOW + timedelta(seconds=5))  # 55 left
        self.assertEqual(deadline, lot.deadline_at)

    def test_a_late_bid_resets_to_40_every_time(self):
        at = NOW
        franchises = [(self.mumbai, ALICE), (self.chennai, BOB)]
        for i in range(8):               # far past any old anti-snipe cap
            lot = self.fresh()
            at = lot.deadline_at - timedelta(seconds=3)
            franchise, tg = franchises[i % 2]
            lot = self.bid(franchise, tg, at)
            self.assertEqual(at + timedelta(seconds=40), lot.deadline_at)
        self.assertEqual(0, int(lot.extensions_used or 0))

    def test_the_bid_line_says_the_clock_went_back(self):
        from services import auction_rich as AR
        at = self.lot.deadline_at - timedelta(seconds=12)
        self.bid(self.mumbai, ALICE, at)
        events = [e for e in self.A.pending_events(self.session, self.season,
                                                   limit=100)
                  if e.kind == "bid"]
        self.assertIn("Clock back to <b>40s</b>",
                      AR.bid_burst_html(self.session, self.season, events[-1:],
                                        now=at))

    def test_a_classic_season_keeps_anti_snipe(self):
        self.A.set_timer(self.session, self.season, "classic")
        self.session.commit()
        deadline = self.lot.deadline_at
        lot = self.bid(self.mumbai, ALICE, deadline - timedelta(seconds=15))
        self.assertEqual(deadline, lot.deadline_at)


class WarningTests(StagedCase):

    def setUp(self):
        super().setUp()
        # Everything the lot opening said is already out.
        self.bot = RecordingBot()
        self.tick(self.bot, NOW + timedelta(seconds=1))
        self.bot.sent.clear()

    def warnings(self):
        return [(t, m) for t, m in self.bot.sent if "warning" in t]

    def test_first_and_second_warning_once_each_with_buttons(self):
        self.bid(self.mumbai, ALICE, NOW + timedelta(seconds=2))
        deadline = self.fresh().deadline_at
        self.tick(self.bot, deadline - timedelta(seconds=19))
        self.tick(self.bot, deadline - timedelta(seconds=17))
        said = self.warnings()
        self.assertEqual(1, len(said), "said once per stage")
        text, markup = said[0]
        self.assertIn("1st warning", text)
        self.assertIn("Selling", text)
        self.assertIn("Mumbai", text)
        self.assertIsNotNone(markup)
        self.tick(self.bot, deadline - timedelta(seconds=9))
        said = self.warnings()
        self.assertEqual(2, len(said))
        self.assertIn("2nd warning", said[1][0])

    def test_a_bid_after_a_warning_rearms_both(self):
        self.bid(self.mumbai, ALICE, NOW + timedelta(seconds=2))
        deadline = self.fresh().deadline_at
        at = deadline - timedelta(seconds=9)
        self.tick(self.bot, at)
        self.assertEqual(1, len(self.warnings()))
        lot = self.bid(self.chennai, BOB, at)
        self.assertEqual(at + timedelta(seconds=40), lot.deadline_at)
        self.tick(self.bot, lot.deadline_at - timedelta(seconds=19))
        latest = self.warnings()[-1][0]
        self.assertIn("1st warning", latest)
        self.assertIn("Chennai", latest)

    def test_no_bids_is_warned_it_is_going_unsold(self):
        self.tick(self.bot, self.lot.deadline_at - timedelta(seconds=19))
        text = self.warnings()[-1][0]
        self.assertIn("UNSOLD", text)
        self.assertIn("No bids yet", text)

    def test_a_classic_season_sends_no_warnings(self):
        self.A.set_timer(self.session, self.season, "classic")
        self.session.commit()
        self.tick(self.bot, self.lot.deadline_at - timedelta(seconds=9))
        self.assertEqual([], self.warnings())


class EditedCountdownTests(StagedCase):

    def run_count(self, on_sleep=None):
        clock = [NOW]

        async def sleep(seconds):
            clock[0] += timedelta(seconds=seconds)
            if on_sleep:
                on_sleep(clock[0])

        self.bot = RecordingBot()
        lot = self.fresh()
        return asyncio.run(self.S.run_countdown(
            self.bot, self.season.id, lot.id, lot.deadline_at,
            self.A.countdown_seconds(self.season), sleep=sleep,
            clock=lambda: clock[0]))

    def test_one_message_edited_five_to_one_then_sold(self):
        self.bid(self.mumbai, ALICE, NOW + timedelta(seconds=1))
        self.assertTrue(self.run_count())
        counts = [t for t, _ in self.bot.sent if "Selling" in t]
        self.assertEqual(1, len(counts), "the count is ONE message")
        self.assertTrue(counts[0].endswith("<b>5</b>"))
        edited = [t for _mid, t, _m in self.bot.edits]
        self.assertEqual(["4", "3", "2", "1"],
                         [t.rsplit("<b>", 1)[1].rstrip("</b>") for t in edited])
        self.assertTrue(any("SOLD" in t for t, _ in self.bot.sent))
        self.assertEqual(self.A.LOT_SOLD, self.fresh().status)

    def test_a_reset_ends_the_count_and_says_so(self):
        self.bid(self.mumbai, ALICE, NOW + timedelta(seconds=1))
        deadline = self.fresh().deadline_at

        def late_bid(now):
            if now >= deadline - timedelta(seconds=3):
                lot = self.fresh()
                if lot.deadline_at == deadline:
                    self.A.place_bid(self.session, self.season, lot,
                                     self.chennai,
                                     self.A.next_min_bid(self.season, lot),
                                     now=now, by_tg_id=BOB)
                    self.session.commit()

        self.assertFalse(self.run_count(late_bid))
        last = self.bot.edits[-1][1]
        self.assertIn("New bid", last)
        self.assertIn("Chennai", last)
        self.assertIn("clock back to", last)
        self.assertEqual(self.A.LOT_ON_BLOCK, self.fresh().status)


if __name__ == "__main__":
    unittest.main()

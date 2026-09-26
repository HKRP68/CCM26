"""The TeleAuction-style room: buttons under every bid, popups, dot shortcuts,
the leaderboard and bid history, and anti-spam that never holds one team back
because another one just bid.

What is pinned here:

  • **The bid keyboard** is the minimum, one step above it, and a second row of
    💼 My Purse / 📊 Status popups — and the prices follow the bid ladder.
  • **The popups** answer the presser privately: their own franchise, or who
    leads the lot.
  • **Every bid line carries the buttons**, and the previous one loses them,
    so the room is never offered a column of stale prices.
  • **The SOLD card** reads the buyer's purse, squad and spend back.
  • **Dot shortcuts** reach the same handlers as the slash commands, and do
    nothing in a chat without an auction.
  • **Many teams can bid together**: the room-wide gap is off by default, and
    a bare /bid that loses a race lands at the fresh minimum instead.
  • **Anti-spam is per person**: a cooldown, a burst mute, refusals said once.
"""

import asyncio
import os
import sys
import unittest
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_auction_bidding import (  # noqa: E402,F401  (module fixtures)
    ALICE, BOB, NOW, AuctionCase, setUpModule, tearDownModule)


class RoomCase(AuctionCase):
    """A live lot on the real clock, and a fake room to talk to."""

    def setUp(self):
        super().setUp()
        from services import auction_antispam as SPAM
        SPAM.reset()
        self.SPAM = SPAM
        self.build_pool()
        self.lot = self.start(now=datetime.utcnow())
        self.session.commit()
        self.replies = []
        self.answers = []

    def _update(self, user_id, args=(), *, text=None, data=None,
                chat_id=None, chat_type="supergroup"):
        async def reply_text(text, **kwargs):
            self.replies.append(text)
            return SimpleNamespace(message_id=1)

        async def answer(text=None, show_alert=False, **kwargs):
            self.answers.append((text, show_alert))

        async def set_message_reaction(**kwargs):
            return True

        update = SimpleNamespace(
            effective_chat=SimpleNamespace(
                id=self.season.chat_id if chat_id is None else chat_id,
                type=chat_type),
            effective_user=SimpleNamespace(id=user_id, username="u",
                                           first_name="U"),
            effective_message=SimpleNamespace(reply_text=reply_text,
                                              message_id=7, text=text),
            callback_query=(SimpleNamespace(data=data, answer=answer)
                            if data is not None else None))
        context = SimpleNamespace(
            args=list(args),
            bot=SimpleNamespace(set_message_reaction=set_message_reaction))
        return update, context

    def run_handler(self, handler, user_id, args=(), **kwargs):
        update, context = self._update(user_id, args, **kwargs)
        asyncio.run(handler(update, context))

    def fresh_lot(self):
        from models import AuctionLot
        self.session.expire_all()
        return (self.session.query(AuctionLot)
                .filter(AuctionLot.id == self.lot.id).first())


class KeyboardTests(RoomCase):

    def test_two_bid_buttons_and_the_popup_row(self):
        from services import auction_rich as AR
        keyboard = AR.bid_keyboard(self.season, self.lot)
        rows = keyboard.inline_keyboard
        self.assertEqual(2, len(rows))
        minimum = self.A.next_min_bid(self.season, self.lot)
        step = self.A.increment_for(self.season, minimum)
        self.assertEqual([f"{AR.BID_CB}{self.lot.id}_{minimum}",
                          f"{AR.BID_CB}{self.lot.id}_{minimum + step}"],
                         [b.callback_data for b in rows[0]])
        self.assertTrue(rows[0][0].text.startswith("🎯 Open"))
        self.assertEqual(["au_me_purse", "au_me_lot"],
                         [b.callback_data for b in rows[1]])

    def test_after_a_bid_the_buttons_follow_the_ladder(self):
        from services import auction_rich as AR
        self.run_handler(__import__("handlers.auction", fromlist=["x"]).bid_handler,
                         ALICE)
        lot = self.fresh_lot()
        keyboard = AR.bid_keyboard(self.season, lot)
        first = keyboard.inline_keyboard[0][0]
        self.assertTrue(first.text.startswith("💰 Bid"))
        self.assertTrue(first.callback_data.endswith(
            f"_{self.A.next_min_bid(self.season, lot)}"))

    def test_popup_prefix_is_shared_and_an_auction_button(self):
        from services.auction_focus import is_auction_callback
        from services.button_access import SHARED_CALLBACK_PREFIXES
        self.assertIn("au_me_", SHARED_CALLBACK_PREFIXES)
        self.assertTrue(is_auction_callback("au_me_purse"))


class PopupTests(RoomCase):

    def test_my_purse_answers_the_owner_privately(self):
        from handlers import auction as H
        self.run_handler(H.me_callback, ALICE, data="au_me_purse")
        text, alert = self.answers[-1]
        self.assertTrue(alert)
        self.assertIn("Mumbai", text)
        self.assertIn("Max bid", text)
        self.assertIn(f"/{self.season.max_squad_size}", text)
        self.assertLessEqual(len(text), 200)

    def test_a_stranger_is_told_they_own_no_franchise(self):
        from handlers import auction as H
        self.run_handler(H.me_callback, 99999, data="au_me_purse")
        self.assertIn("don't own a franchise", self.answers[-1][0])

    def test_status_names_the_leader(self):
        from handlers import auction as H
        self.run_handler(H.bid_handler, ALICE)
        self.run_handler(H.me_callback, ALICE, data="au_me_lot")
        text = self.answers[-1][0]
        self.assertIn(self.lot.name[:20], text)
        self.assertIn("Mumbai (you)", text)
        self.assertIn("Next bid", text)


class RecordingBot:
    def __init__(self):
        self.sent = []          # (text, reply_markup)
        self.stripped = []      # message ids whose buttons came off
        self.next_id = 700

    async def send_message(self, chat_id=None, text="", reply_markup=None,
                           **kwargs):
        self.next_id += 1
        self.sent.append((text, reply_markup))
        return SimpleNamespace(message_id=self.next_id)

    async def edit_message_reply_markup(self, chat_id=None, message_id=None,
                                        reply_markup=None, **kwargs):
        self.stripped.append(message_id)
        return True


class BidLineTests(RoomCase):

    def setUp(self):
        super().setUp()
        from services import auction_scheduler as S
        self.S = S
        self._gap = S.BID_MESSAGE_GAP
        S.BID_MESSAGE_GAP = 0
        S._last_bid_message.clear()
        S._live_buttons.clear()
        # Everything that opened the lot is already said.
        self.season.announced_event_id = max(
            (e.id for e in self.A.pending_events(self.session, self.season,
                                                 limit=500)),
            default=self.season.announced_event_id)
        self.session.commit()

    def tearDown(self):
        self.S.BID_MESSAGE_GAP = self._gap
        self.S._live_buttons.clear()
        super().tearDown()

    def bid(self, franchise, tg_id):
        lot = self.fresh_lot()
        self.A.place_bid(self.session, self.season, lot, franchise,
                         self.A.next_min_bid(self.season, lot), by_tg_id=tg_id)
        self.session.commit()

    def test_each_bid_line_carries_buttons_and_the_old_one_loses_them(self):
        bot = RecordingBot()
        self.bid(self.mumbai, ALICE)
        asyncio.run(self.S.drain_events(bot, self.session, self.season))
        first_text, first_markup = bot.sent[-1]
        self.assertIn("💥", first_text)
        self.assertIn("Opening bid", first_text)
        self.assertIsNotNone(first_markup)
        first_id = bot.next_id

        self.bid(self.chennai, BOB)
        asyncio.run(self.S.drain_events(bot, self.session, self.season))
        second_text, second_markup = bot.sent[-1]
        self.assertIn("Outbids <b>Mumbai</b>", second_text)
        self.assertIn(f"tg://user?id={ALICE}", second_text,
                      "the outbid owner is tagged, so they are notified")
        self.assertIsNotNone(second_markup)
        self.assertEqual([first_id], bot.stripped,
                         "only the newest message keeps live buttons")

    def test_a_sale_takes_the_buttons_off(self):
        bot = RecordingBot()
        self.bid(self.mumbai, ALICE)
        asyncio.run(self.S.drain_events(bot, self.session, self.season))
        held = bot.next_id
        self.A.sell_lot(self.session, self.season, self.fresh_lot())
        self.session.commit()
        asyncio.run(self.S.drain_events(bot, self.session, self.season))
        self.assertIn(held, bot.stripped)
        sold = [t for t, _ in bot.sent if "SOLD" in t]
        self.assertTrue(sold)
        self.assertIn("Purse left", sold[-1])
        self.assertIn("Players:", sold[-1])
        self.assertIn("Spent", sold[-1])


class DotShortcutTests(RoomCase):

    def test_parse(self):
        from handlers import auction as H
        handler, args = H.parse_dot_command(".bid 2cr")
        self.assertIs(H.bid_handler, handler)
        self.assertEqual(["2cr"], args)
        self.assertIs(H.apurse_handler, H.parse_dot_command(".PURSE")[0])
        self.assertIs(H.aleaderboard_handler, H.parse_dot_command(".lb")[0])
        for text in (".", "..bid", ". bid", ".hello there", "bid", ""):
            self.assertIsNone(H.parse_dot_command(text)[0], text)

    def test_dot_bid_lands_a_bid(self):
        from handlers import auction as H
        self.run_handler(H.dot_command_handler, ALICE, text=".bid")
        lot = self.fresh_lot()
        self.assertEqual(self.mumbai.id, lot.current_bidder_id)
        self.assertEqual(lot.base_price_lakh, lot.current_bid_lakh)

    def test_dot_bid_with_an_amount(self):
        from handlers import auction as H
        amount = self.A.next_min_bid(self.season, self.lot) + 100
        self.run_handler(H.dot_command_handler, ALICE,
                         text=f".bid {self.A._bid_hint(amount)}")
        self.assertEqual(amount, self.fresh_lot().current_bid_lakh)

    def test_a_chat_without_an_auction_is_left_alone(self):
        from handlers import auction as H
        self.run_handler(H.dot_command_handler, ALICE, text=".bid",
                         chat_id=-424242)
        self.assertEqual([], self.replies)
        self.assertIsNone(self.fresh_lot().current_bidder_id)


class ViewTests(RoomCase):

    def test_leaderboard_ranks_by_spend(self):
        from services import auction_rich as AR
        lot = self.fresh_lot()
        self.A.place_bid(self.session, self.season, lot, self.chennai,
                         lot.base_price_lakh + 200, by_tg_id=BOB)
        self.A.sell_lot(self.session, self.season, self.fresh_lot())
        self.session.commit()
        _blocks, html_text = AR.leaderboard_view(self.session, self.season)
        self.assertLess(html_text.index("Chennai"), html_text.index("Mumbai"))
        self.assertIn("🥇", html_text)

    def test_my_bids_reads_won_and_lost(self):
        from services import auction_rich as AR
        lot = self.fresh_lot()
        self.A.place_bid(self.session, self.season, lot, self.mumbai,
                         lot.base_price_lakh, by_tg_id=ALICE)
        lot = self.fresh_lot()
        self.A.place_bid(self.session, self.season, lot, self.chennai,
                         self.A.next_min_bid(self.season, lot), by_tg_id=BOB)
        self.A.sell_lot(self.session, self.season, self.fresh_lot())
        self.session.commit()
        _b, mumbai = AR.mybids_view(self.session, self.season, self.mumbai)
        _b, chennai = AR.mybids_view(self.session, self.season, self.chennai)
        self.assertIn("Lost to Chennai", mumbai)
        self.assertIn("🟢 Won", chennai)

    def test_new_commands_pass_the_focus_gate(self):
        from services.auction_focus import AUCTION_COMMANDS
        for name in ("aleaderboard", "alb", "amybids", "amybidhistory"):
            self.assertIn(name, AUCTION_COMMANDS)


class TogetherTests(RoomCase):
    """Many teams can /bid together."""

    def test_the_room_wide_gap_is_off_by_default(self):
        season = self.A.create_season(self.session, "Gap default check")
        self.session.flush()
        self.assertEqual(0, self.A.bid_gap_seconds(season))
        self.assertEqual(0, self.A.bid_gap_seconds(SimpleNamespace()))
        self.assertEqual(3, self.A.set_bid_gap(self.session, season, "3"))

    def test_a_bare_bid_that_loses_the_race_lands_at_the_fresh_minimum(self):
        """Alice and Bob both send /bid at the same instant.

        Bob's handler read the minimum before Alice's bid landed; the retry
        takes the next step instead of refusing him.
        """
        from handlers import auction as H
        base = self.lot.base_price_lakh
        self.run_handler(H.bid_handler, ALICE)
        real = self.A.next_min_bid
        calls = {"n": 0}

        def stale_then_real(season, lot):
            calls["n"] += 1
            return base if calls["n"] == 1 else real(season, lot)

        H.A.next_min_bid = stale_then_real
        try:
            self.run_handler(H.bid_handler, BOB)
        finally:
            H.A.next_min_bid = real
        lot = self.fresh_lot()
        self.assertEqual(self.chennai.id, lot.current_bidder_id)
        self.assertEqual(real(self.season, SimpleNamespace(
            current_bid_lakh=base, base_price_lakh=base)), lot.current_bid_lakh)
        self.assertEqual(2, lot.bid_count)
        self.assertEqual([], self.replies, "both bids land silently")

    def test_a_named_amount_is_never_raised_for_anyone(self):
        from handlers import auction as H
        base = self.lot.base_price_lakh
        self.run_handler(H.bid_handler, ALICE)
        self.run_handler(H.bid_handler, BOB, (self.A._bid_hint(base),))
        lot = self.fresh_lot()
        self.assertEqual(self.mumbai.id, lot.current_bidder_id)
        self.assertIn("Beaten to it", self.replies[-1])
        self.assertIn("Mumbai", self.replies[-1])

    def test_a_losing_button_press_says_who_leads(self):
        from handlers import auction as H
        base = self.lot.base_price_lakh
        self.run_handler(H.bid_handler, ALICE)
        self.run_handler(H.bid_callback, BOB,
                         data=f"au_bid_{self.lot.id}_{base}")
        text, alert = self.answers[-1]
        self.assertIn("Beaten to it", text)
        self.assertTrue(alert)


class AntiSpamTests(RoomCase):

    def test_cooldown_then_through(self):
        v = self.SPAM.check_bid(1, 2, now=100.0)
        self.assertTrue(v.allowed)
        self.assertEqual(self.SPAM.COOLDOWN,
                         self.SPAM.check_bid(1, 2, now=100.3).state)
        self.assertTrue(self.SPAM.check_bid(1, 2, now=101.2).allowed)
        # Another person, or the same person in another room, is untouched.
        self.assertTrue(self.SPAM.check_bid(1, 3, now=100.3).allowed)
        self.assertTrue(self.SPAM.check_bid(9, 2, now=100.3).allowed)

    def test_a_burst_mutes_once_with_one_warning_then_expires(self):
        t = 200.0
        # One tap lands; the rest inside the cooldown are dropped, and one
        # drop too many earns the mute.
        verdicts = [self.SPAM.check_bid(1, 2, now=t + i * 0.1)
                    for i in range(self.SPAM.BURST_LIMIT + 2)]
        self.assertEqual(self.SPAM.MUTED, verdicts[-1].state)
        self.assertTrue(verdicts[-1].warn)
        again = self.SPAM.check_bid(1, 2, now=t + 3)
        self.assertEqual(self.SPAM.MUTED, again.state)
        self.assertFalse(again.warn, "the warning is said once")
        later = self.SPAM.check_bid(1, 2, now=t + 2 + self.SPAM.MUTE_SECONDS)
        self.assertTrue(later.allowed)

    def test_a_hot_bidding_war_never_trips_the_mute(self):
        """An owner who bids every time they are outbid is not a spammer."""
        for i in range(40):
            self.assertTrue(self.SPAM.check_bid(1, 2, now=300.0 + i * 1.1).allowed)

    def test_the_same_refusal_is_said_once(self):
        self.assertTrue(self.SPAM.should_say(1, 2, 5, "no", now=10.0))
        self.assertFalse(self.SPAM.should_say(1, 2, 5, "no", now=12.0))
        self.assertTrue(self.SPAM.should_say(1, 2, 5, "other", now=12.0))
        self.assertTrue(self.SPAM.should_say(1, 2, 5, "no",
                                             now=10.0 + self.SPAM.REFUSAL_REPEAT))

    def test_a_second_tap_inside_the_cooldown_is_dropped_silently(self):
        from handlers import auction as H
        self.run_handler(H.bid_handler, ALICE)
        self.run_handler(H.bid_handler, BOB)
        # Alice again, straight away: the cooldown drops it without a word.
        self.run_handler(H.bid_handler, ALICE)
        lot = self.fresh_lot()
        self.assertEqual(self.chennai.id, lot.current_bidder_id)
        self.assertEqual([], self.replies)

    def test_a_button_tap_inside_the_cooldown_gets_a_toast(self):
        from handlers import auction as H
        self.run_handler(H.bid_handler, ALICE)
        minimum = self.A.next_min_bid(self.season, self.fresh_lot())
        self.run_handler(H.bid_callback, ALICE,
                         data=f"au_bid_{self.lot.id}_{minimum}")
        text, alert = self.answers[-1]
        self.assertIn("Easy", text)
        self.assertFalse(alert)


if __name__ == "__main__":
    unittest.main()

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
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

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

    def test_a_fast_rebid_after_being_outbid_is_accepted(self):
        from handlers import auction as H
        self.run_handler(H.bid_handler, ALICE)
        self.run_handler(H.bid_handler, BOB)
        # Alice has a new opportunity, even within the one-second cooldown.
        self.run_handler(H.bid_handler, ALICE)
        lot = self.fresh_lot()
        self.assertEqual(self.mumbai.id, lot.current_bidder_id)
        self.assertEqual(3, lot.bid_count)
        self.assertEqual([], self.replies)

    def test_a_duplicate_typed_bid_gets_visible_feedback(self):
        from handlers import auction as H
        self.run_handler(H.bid_handler, ALICE)
        self.run_handler(H.bid_handler, ALICE)
        self.assertEqual(1, self.fresh_lot().bid_count)
        self.assertIn('wait', self.replies[-1])
        self.assertIn('another team raises', self.replies[-1])

    def test_a_new_opportunity_clears_a_duplicate_burst_mute(self):
        first = (1, 0, None)
        for i in range(self.SPAM.BURST_LIMIT + 2):
            v = self.SPAM.check_bid(1, 2, now=500+i*.1, revision=first)
        self.assertEqual(self.SPAM.MUTED, v.state)
        self.assertTrue(self.SPAM.check_bid(1, 2, now=500.8,
                                             revision=(1, 2, None)).allowed)
        self.assertEqual(self.SPAM.COOLDOWN, self.SPAM.check_bid(
            1, 2, now=500.9, revision=(1, 2, None)).state)

    def test_subsecond_bidding_war_does_not_trigger_a_mute(self):
        for i in range(40):
            self.assertTrue(self.SPAM.check_bid(1, 2, now=600+i*.01,
                                                revision=(1, i*2, None)).allowed)

    def test_a_new_lot_and_rtm_final_offer_are_fresh_opportunities(self):
        own = SimpleNamespace(id=1)
        leading = SimpleNamespace(id=10, current_bidder_id=1, bid_count=5, rtm_stage=None)
        self.assertIsNone(self.SPAM.bid_revision(leading, own))
        leading.rtm_stage = 'final_offer'
        self.assertEqual((10, 5, 'final_offer'), self.SPAM.bid_revision(leading, own))
        self.assertTrue(self.SPAM.check_bid(1, 2, now=700,
                                            revision=(10, 0, None)).allowed)
        self.assertTrue(self.SPAM.check_bid(1, 2, now=700.1,
                                            revision=(11, 0, None)).allowed)

    def test_a_button_tap_inside_the_cooldown_gets_a_toast(self):
        from handlers import auction as H
        self.run_handler(H.bid_handler, ALICE)
        minimum = self.A.next_min_bid(self.season, self.fresh_lot())
        self.run_handler(H.bid_callback, ALICE,
                         data=f"au_bid_{self.lot.id}_{minimum}")
        text, alert = self.answers[-1]
        self.assertIn("Easy", text)
        self.assertFalse(alert)


class BidResponsivenessTests(RoomCase):

    def test_admin_worker_can_cancel_a_countdown_on_its_own_event_loop(self):
        from services import auction_scheduler as S
        async def run():
            loop = asyncio.get_running_loop()
            loop.set_debug(True)
            task = asyncio.create_task(asyncio.Event().wait())
            await asyncio.sleep(0)
            S._countdowns[self.season.id] = (self.lot.id, task)
            try:
                await asyncio.to_thread(S.cancel_countdown, self.season.id)
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertNotIn(self.season.id, S._countdowns)
            finally:
                task.cancel()
                S._countdowns.pop(self.season.id, None)
        asyncio.run(run())

    def test_recall_does_not_block_other_updates(self):
        from handlers import auction as H
        sold = self.lot
        self.A.place_bid(self.session, self.season, sold, self.mumbai,
                         sold.base_price_lakh, by_tg_id=ALICE)
        self.A.sell_lot(self.session, self.season, sold)
        self.session.commit()
        update, context = self._update(ALICE, sold.name.split())
        gate = threading.Event()
        released, threads = [], []
        real = self.A.recall_sale

        def slow(*args, **kwargs):
            threads.append(threading.get_ident())
            released.append(gate.wait(timeout=1))
            return real(*args, **kwargs)

        async def run():
            loop_id = threading.get_ident()
            asyncio.get_running_loop().call_later(.02, gate.set)
            with patch.object(H, '_is_auction_admin', return_value=True), \
                    patch.object(H.A, 'recall_sale', side_effect=slow):
                await H.arecall_handler(update, context)
            self.assertTrue(threads)
            self.assertTrue(all(t != loop_id for t in threads))

        asyncio.run(run())
        self.assertEqual([True], released)
        self.assertEqual(self.A.LOT_ON_BLOCK, self.fresh_lot().status)
        self.assert_ledger_agrees('recall in a worker')

    def test_slow_admin_lookup_and_views_leave_the_loop_responsive(self):
        from handlers import auction as H
        update, context = self._update(ALICE)

        async def check(call, target, replacement):
            gate = threading.Event()
            released = []

            def slow(*args, **kwargs):
                released.append(gate.wait(timeout=1))
                return replacement(*args, **kwargs)

            asyncio.get_running_loop().call_later(.02, gate.set)
            with patch.object(H, target, side_effect=slow):
                await call()
            self.assertEqual([True], released)

        async def run():
            await check(lambda: H._require_admin(update), '_is_auction_admin', lambda _: True)
            await check(lambda: H._view(update, context, lambda *_: 'Ready'),
                        '_read_season', H._read_season)

        asyncio.run(run())
        self.assertTrue(any('Ready' in text for text in self.replies))

    def test_finish_simulation_and_trade_commands_leave_the_loop_responsive(self):
        from handlers import auction as H
        from handlers import auction_trade as T
        from services import auction_simulator as S
        update, context = self._update(ALICE, ['go'])

        async def run():
            gate = threading.Event()
            released = []
            def slow(*args, **kwargs):
                released.append(gate.wait(timeout=1))
                return dict(sold=0, accel_sold=0, rtm=0, autofilled=0)
            asyncio.get_running_loop().call_later(.02, gate.set)
            with patch.object(H, '_is_auction_admin', return_value=True), \
                    patch.object(S, 'playback_pending', return_value=False), \
                    patch.object(S, 'simulate_finish', side_effect=slow):
                await H.afinish_handler(update, context)
            self.assertEqual([True], released)
            gate.clear()
            asyncio.get_running_loop().call_later(.02, gate.set)
            def trade_work(*args):
                self.assertTrue(gate.wait(timeout=1))
                return 'Trade ready'
            with patch.object(T, 'get_session', H.get_session), \
                    patch.object(T, '_read_season', H._read_season):
                await T._run(update, trade_work)

        asyncio.run(run())
        self.assertTrue(any('Trade ready' in text for text in self.replies))

    def test_shared_command_session_is_closed_before_sending_its_reply(self):
        from handlers import auction as H
        update, _ = self._update(ALICE)
        real, closed = H.get_session, []
        loop_thread = threading.get_ident()
        def tracked():
            self.assertNotEqual(loop_thread, threading.get_ident())
            session = real()
            close = session.close
            def close_session():
                close()
                closed.append(True)
            session.close = close_session
            return session
        async def reply(*args, **kwargs):
            self.assertEqual(loop_thread, threading.get_ident())
            self.assertEqual([True], closed)
        with patch.object(H, 'get_session', side_effect=tracked), \
                patch.object(H, '_reply_card', side_effect=reply):
            asyncio.run(H._with_auction(update, lambda *_: 'Done'))

    def test_slow_bid_database_does_not_block_the_bot_event_loop(self):
        from handlers import auction as H
        update, context = self._update(ALICE)
        gate = threading.Event()
        released = []
        threads = []
        real = self.A.place_bid

        def slow(*args, **kwargs):
            threads.append(threading.get_ident())
            released.append(gate.wait(timeout=1))
            return real(*args, **kwargs)

        async def run():
            loop_id = threading.get_ident()
            asyncio.get_running_loop().call_later(.02, gate.set)
            with patch.object(H.A, 'place_bid', side_effect=slow):
                await H.bid_handler(update, context)
            self.assertTrue(all(t != loop_id for t in threads))

        asyncio.run(run())
        self.assertEqual([True], released, 'the loop must run while the DB is busy')
        self.assertEqual(1, self.fresh_lot().bid_count)

    def test_reaction_failure_falls_back_to_a_committed_bid_receipt(self):
        from handlers import auction as H
        update, context = self._update(ALICE)

        async def fail(**kwargs):
            raise RuntimeError('reactions unavailable')

        context.bot.set_message_reaction = fail
        asyncio.run(H.bid_handler(update, context))
        self.assertEqual(1, self.fresh_lot().bid_count)
        self.assertIn('Bid accepted', self.replies[-1])

    def test_callback_ack_failure_never_reports_a_committed_bid_as_failed(self):
        from handlers import auction as H
        update, context = self._update(ALICE, data=f'au_bid_{self.lot.id}_{self.lot.base_price_lakh}')

        async def fail(*args, **kwargs):
            raise RuntimeError('callback expired')

        update.callback_query.answer = fail
        asyncio.run(H.bid_callback(update, context))
        self.assertEqual(1, self.fresh_lot().bid_count)
        self.assertIn('Bid accepted', self.replies[-1])

    def test_session_is_closed_before_telegram_acknowledgement(self):
        from handlers import auction as H
        update, context = self._update(ALICE)
        closed = []
        real = H.get_session

        def tracked():
            db = real()
            close = db.close
            def close_session():
                closed.append(True)
                close()
            db.close = close_session
            return db

        async def acknowledge(**kwargs):
            self.assertTrue(closed)
            return True

        context.bot.set_message_reaction = acknowledge
        with patch.object(H, 'get_session', side_effect=tracked):
            asyncio.run(H.bid_handler(update, context))
        self.assertEqual(1, self.fresh_lot().bid_count)


if __name__ == "__main__":
    unittest.main()

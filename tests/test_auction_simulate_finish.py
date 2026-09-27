"""``/afinish`` — end an auction by simulating the rest of it.

What is pinned here:

  • **Every squad the simulation builds is one the live auction could have
    built.** Squad min/max, the overseas cap, role ceilings, purses that never
    go negative and a ledger that still reconciles.
  • **The room's own decision stands.** A standing bid on the block is sold at
    its price before anything is simulated.
  • **It is deterministic.** The same auction simulates the same way twice.
  • **The playback is slow and survives completion.** ``sim_*`` events are
    drained one per gap, and a completed season is still drained while it has
    any left — and only then.
"""

import asyncio
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_auction_bidding import (  # noqa: E402,F401  (module fixtures)
    ALICE, BOB, CAROL, NOW, AuctionCase, setUpModule, tearDownModule)

ROLES = ("Batsman", "Bowler", "All Rounder", "Wicket Keeper")
COUNTRIES = ("India", "India", "India", "Australia", "England")


class SimulateCase(AuctionCase):
    min_squad = 6
    max_squad = 9
    max_overseas = 3
    purse_lakh = 5_000

    def setUp(self):
        super().setUp()
        from models import Player
        from services import auction_simulator as S
        self.S = S
        self.delhi = self.A.create_franchise(self.session, self.season, "Delhi",
                                             owner_tg_id=CAROL,
                                             owner_name="Carol")
        extra = []
        for n in range(40):
            player = Player(name=f"Sim Player {self.season.id}-{n}",
                            rating=65 + (n * 7) % 33,
                            category=ROLES[n % 4],
                            country=COUNTRIES[n % 5], version="Base",
                            bat_hand="Right", bowl_hand="Right",
                            bowl_style="Medium Pacer", bat_rating=70,
                            bowl_rating=60, is_active=True)
            self.session.add(player)
            extra.append(player)
        self.session.flush()
        self.build_pool(self.players[:8] + extra)

    def teams(self):
        return self.A.franchises(self.session, self.season.id)

    def assert_squads_legal(self):
        caps = self.A.role_maximums(self.season)
        for franchise in self.teams():
            squad = self.A.squad(self.session, franchise.id)
            self.assertEqual(len(squad), franchise.squad_size, franchise.name)
            self.assertLessEqual(franchise.squad_size, self.max_squad)
            self.assertGreaterEqual(franchise.purse_remaining_lakh, 0)
            self.assertLessEqual(sum(1 for lot in squad if lot.is_overseas),
                                 self.max_overseas, franchise.name)
            names = [lot.name.lower() for lot in squad]
            self.assertEqual(len(names), len(set(names)), franchise.name)
            counts = self.A.role_counts(self.session, franchise.id)
            for role, cap in caps.items():
                self.assertLessEqual(counts.get(role, 0), cap)
        self.assert_ledger_agrees("after the simulation")


class SimulateFinishTests(SimulateCase):

    def test_preview_changes_nothing(self):
        self.start()
        self.session.commit()
        info = self.S.preview(self.session, self.season)
        self.assertEqual(48, info["remaining"])
        self.assertEqual(3, len(info["teams"]))
        self.assertEqual(self.A.STATUS_LIVE, self.season.status)

    def test_every_lot_resolves_and_every_squad_is_complete_and_legal(self):
        self.start()
        summary = self.S.simulate_finish(self.session, self.season, seed=7)
        self.session.commit()
        self.assertEqual(self.A.STATUS_COMPLETED, self.season.status)
        self.assertIsNone(self.A.current_lot(self.session, self.season))
        self.assertIsNone(self.A.next_queued(self.session, self.season.id))
        for franchise in self.teams():
            self.assertGreaterEqual(franchise.squad_size, self.min_squad,
                                    franchise.name)
        self.assertGreater(summary["sold"], 0)
        self.assert_squads_legal()

    def test_role_ceilings_are_respected(self):
        self.A.set_role_rules(self.session, self.season, {},
                              {"Wicket Keeper": 1, "Bowler": 3})
        self.session.commit()
        self.start()
        self.S.simulate_finish(self.session, self.season, seed=3)
        self.session.commit()
        self.assert_squads_legal()

    def test_a_standing_bid_is_honoured_at_its_price(self):
        lot = self.start()
        price = lot.base_price_lakh
        self.A.place_bid(self.session, self.season, lot, self.chennai, price,
                         now=NOW, by_tg_id=BOB)
        self.session.commit()
        self.S.simulate_finish(self.session, self.season, seed=1)
        self.session.commit()
        self.session.refresh(lot)
        self.assertEqual(self.A.LOT_SOLD, lot.status)
        self.assertEqual(self.chennai.id, lot.sold_to_id)
        self.assertEqual(price, lot.sold_price_lakh)
        self.assert_squads_legal()

    def test_it_works_before_the_auction_has_started(self):
        self.S.simulate_finish(self.session, self.season, seed=5)
        self.session.commit()
        self.assertEqual(self.A.STATUS_COMPLETED, self.season.status)
        self.assert_squads_legal()

    def test_the_same_seed_simulates_the_same_auction(self):
        self.start()
        self.session.commit()
        self.S.simulate_finish(self.session, self.season, seed=11)
        first = sorted((lot.name, lot.sold_to_id, lot.sold_price_lakh)
                       for lot in self.A.sold_lots(self.session, self.season.id))
        self.session.rollback()
        self.S.simulate_finish(self.session, self.season, seed=11)
        second = sorted((lot.name, lot.sold_to_id, lot.sold_price_lakh)
                        for lot in self.A.sold_lots(self.session, self.season.id))
        self.assertEqual(first, second)

    def test_a_finished_auction_is_refused(self):
        self.A.cancel(self.session, self.season)
        with self.assertRaises(self.A.AuctionError):
            self.S.simulate_finish(self.session, self.season)

    def test_the_playback_is_written_in_order_and_ends_on_a_sim_event(self):
        self.start()
        self.session.commit()
        cursor = self.season.announced_event_id or 0
        self.S.simulate_finish(self.session, self.season, seed=2)
        self.session.commit()
        events = [e for e in self.A.pending_events(self.session, self.season,
                                                    limit=500)
                  if e.id > cursor]
        kinds = [e.kind for e in events]
        self.assertIn("sim_intro", kinds)
        self.assertIn("sim_lot", kinds)
        self.assertIn("sim_team", kinds)
        self.assertEqual("sim_outro", kinds[-1])
        self.assertLess(kinds.index("sim_intro"), kinds.index("sim_lot"))
        self.assertEqual(3, kinds.count("sim_team"))

    def test_quick_mode_batches_the_lots(self):
        self.start()
        self.session.commit()
        self.S.simulate_finish(self.session, self.season, seed=2, quick=True)
        self.session.commit()
        kinds = [e.kind for e in self.A.pending_events(self.session,
                                                       self.season, limit=500)]
        self.assertIn("sim_batch", kinds)
        self.assertNotIn("sim_lot", kinds)

    def test_mute_skips_to_the_final_summary(self):
        self.start()
        self.session.commit()
        self.S.simulate_finish(self.session, self.season, seed=2)
        self.session.commit()
        skipped = self.S.mute_playback(self.session, self.season)
        self.assertGreater(skipped, 0)
        pending = self.A.pending_events(self.session, self.season, limit=50)
        self.assertEqual(["sim_outro"], [e.kind for e in pending])

    def test_every_sim_event_renders(self):
        from services import auction_rich as AR
        self.start()
        self.session.commit()
        self.S.simulate_finish(self.session, self.season, seed=2)
        self.session.commit()
        for event in self.A.pending_events(self.session, self.season,
                                           limit=500):
            text = AR.event_html(self.session, self.season, event)
            self.assertTrue(text)
            self.assertLess(len(text), 4096, event.kind)


class PlaybackTests(SimulateCase):

    def setUp(self):
        super().setUp()
        from services import auction_scheduler as SCH
        self.SCH = SCH
        self.sent = []
        SCH._last_sim_message.clear()
        SCH.SEND_LOT_CARD = False

    def bot(self):
        async def send_message(**kwargs):
            self.sent.append(kwargs["text"])
            return SimpleNamespace(message_id=len(self.sent))

        async def noop(**kwargs):
            return True
        return SimpleNamespace(send_message=send_message,
                               edit_message_text=noop,
                               edit_message_reply_markup=noop,
                               pin_chat_message=noop, unpin_chat_message=noop)

    def test_one_sim_message_per_gap(self):
        self.start()
        self.session.commit()
        self.season.announced_event_id = max(
            e.id for e in self.A.pending_events(self.session, self.season,
                                                limit=500))
        self.S.simulate_finish(self.session, self.season, seed=2)
        self.session.commit()
        bot = self.bot()
        asyncio.run(self.SCH.drain_events(bot, self.session, self.season,
                                          limit=10))
        self.assertEqual(1, len(self.sent))
        asyncio.run(self.SCH.drain_events(bot, self.session, self.season,
                                          limit=10))
        self.assertEqual(1, len(self.sent), "the gap holds the second one")

    def test_completed_seasons_are_drained_only_while_a_sim_is_pending(self):
        self.start()
        self.session.commit()
        self.S.simulate_finish(self.session, self.season, seed=2)
        self.session.commit()
        ids = [s.id for s in self.SCH.playback_seasons(self.session)]
        self.assertIn(self.season.id, ids)
        self.S.mute_playback(self.session, self.season)
        self.season.announced_event_id = max(
            e.id for e in self.A.pending_events(self.session, self.season,
                                                limit=500))
        self.session.commit()
        ids = [s.id for s in self.SCH.playback_seasons(self.session)]
        self.assertNotIn(self.season.id, ids)


class FinishCommandTests(SimulateCase):

    def setUp(self):
        super().setUp()
        self.replies = []
        self._prev_admins = os.environ.get("BOT_ADMIN_IDS")
        os.environ["BOT_ADMIN_IDS"] = str(CAROL)

    def tearDown(self):
        if self._prev_admins is None:
            os.environ.pop("BOT_ADMIN_IDS", None)
        else:
            os.environ["BOT_ADMIN_IDS"] = self._prev_admins
        super().tearDown()

    def _run(self, user_id, args=()):
        from handlers import auction as H

        async def reply_text(text, **kwargs):
            self.replies.append(text)
            return SimpleNamespace(message_id=1)

        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=self.season.chat_id,
                                           type="supergroup"),
            effective_user=SimpleNamespace(id=user_id, username="u",
                                           first_name="U"),
            effective_message=SimpleNamespace(reply_text=reply_text,
                                              message_id=7))
        context = SimpleNamespace(args=list(args), bot=SimpleNamespace())
        self.session.commit()
        self.session.close()
        asyncio.run(H.afinish_handler(update, context))
        from database import get_session
        self.session = get_session()
        self.season = self.session.merge(self.season)
        return self.replies

    def test_bare_afinish_previews_and_asks_to_confirm(self):
        self.start()
        self._run(CAROL)
        self.assertIn("/afinish go", self.replies[-1])
        self.assertEqual(self.A.STATUS_LIVE, self.season.status)

    def test_afinish_go_completes_the_auction(self):
        self.start()
        self._run(CAROL, ("go",))
        self.assertEqual(self.A.STATUS_COMPLETED, self.season.status)
        self.assertIn("⏩", self.replies[-1])

    def test_a_stranger_cannot_finish_it(self):
        self.start()
        self._run(ALICE, ("go",))
        self.assertIn("admin", self.replies[-1].lower())
        self.assertEqual(self.A.STATUS_LIVE, self.season.status)


if __name__ == "__main__":
    unittest.main()

"""Dynamic retention: slots, the Demand Meter, and a player who can say no.

Pinned here:

  • **The toggle.** Classic stays exactly as it was; a season switches only
    while nobody is kept, and only one system answers at a time.
  • **Slots are editable** — add, edit, remove, move, presets — but a slot a
    signed player or an open talk is using cannot vanish or change shape.
  • **Floors and the budget.** Nothing goes under its slot's floor, the total
    never passes the retention budget, and every refusal names the number.
  • **The negotiation.** Accept at or above the hidden price, counter when
    close, reject otherwise; the last chance spent sends the player to the
    auction and nobody can approach him again. Every signing still goes
    through ``retain()``, so the ledger agrees with the purse throughout.
"""

import unittest

from tests.test_auction_retention import (  # noqa: F401 — module fixtures
    ALICE, BOB, CAROL, AuctionCase, setUpModule, tearDownModule)


class DynamicCase(AuctionCase):
    max_squad = 8
    min_squad = 2

    def setUp(self):
        super().setUp()
        from services import retention_negotiation as RN
        self.RN = RN
        RN.set_mode(self.session, self.season, RN.MODE_DYNAMIC)
        self.session.commit()
        self.by_name = {p.name: p for p in self.players if p.version == "Base"}

    def p(self, name):
        return self.by_name[name]

    def talk(self, name, franchise=None, *, map_lakh=None, personality=None):
        t = self.RN.start_talk(self.session, self.season,
                               franchise or self.mumbai, self.p(name))
        if map_lakh is not None:
            t.map_lakh = map_lakh
        if personality is not None:
            t.personality = personality
        self.session.flush()
        return t


class ModeTests(DynamicCase):

    def test_switching_to_dynamic_sets_the_budget_and_slot_count(self):
        self.assertTrue(self.RN.is_dynamic(self.season))
        self.assertEqual(5300, self.season.retention_max_spend_lakh)
        self.assertEqual(3, self.season.max_retentions)

    def test_switching_is_refused_once_somebody_is_kept(self):
        self.A.retain(self.session, self.season, self.mumbai,
                      self.p("Jasprit Bumrah"), 2300)
        with self.assertRaises(self.A.AuctionError):
            self.RN.set_mode(self.session, self.season, self.RN.MODE_CLASSIC)

    def test_classic_admin_offers_are_refused_in_dynamic_mode(self):
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.offer_retention(self.session, self.season, self.mumbai,
                                   self.p("Jasprit Bumrah"), 2300)
        self.assertIn("/retain", str(caught.exception))

    def test_talks_are_refused_in_classic_mode(self):
        self.RN.set_mode(self.session, self.season, self.RN.MODE_CLASSIC)
        with self.assertRaises(self.A.AuctionError):
            self.RN.start_talk(self.session, self.season, self.mumbai,
                               self.p("Jasprit Bumrah"))

    def test_classic_mode_still_prices_off_the_ladder(self):
        self.RN.set_mode(self.session, self.season, self.RN.MODE_CLASSIC)
        lot = self.A.retain(self.session, self.season, self.mumbai,
                            self.p("Rinku Singh"))
        self.assertEqual(1800, lot.sold_price_lakh)
        self.assertIsNone(lot.retention_slot)


class SlotTests(DynamicCase):

    def test_a_player_fills_the_slot_his_rating_reaches(self):
        f = self.mumbai
        self.assertEqual("elite", self.RN.slot_for(
            self.session, self.season, f, 95)["key"])
        self.assertEqual("premium", self.RN.slot_for(
            self.session, self.season, f, 88)["key"])
        self.assertEqual("core", self.RN.slot_for(
            self.session, self.season, f, 84)["key"])
        with self.assertRaises(self.A.AuctionError):
            self.RN.slot_for(self.session, self.season, f, 74)

    def test_a_better_player_can_take_a_lower_open_slot(self):
        self.A.retain(self.session, self.season, self.mumbai,
                      self.p("Jasprit Bumrah"), 2300)
        slot = self.RN.slot_for(self.session, self.season, self.mumbai, 93)
        self.assertEqual("premium", slot["key"])

    def test_a_retention_below_the_floor_is_refused(self):
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.retain(self.session, self.season, self.mumbai,
                          self.p("Jasprit Bumrah"), 1500)
        self.assertIn("23 Cr", str(caught.exception))

    def test_no_price_means_the_slot_floor(self):
        lot = self.A.retain(self.session, self.season, self.mumbai,
                            self.p("Sanju Samson"))
        self.assertEqual(1800, lot.sold_price_lakh)
        self.assertEqual("premium", lot.retention_slot)

    def test_the_budget_moves_between_slots_but_never_past_the_total(self):
        self.A.retain(self.session, self.season, self.mumbai,
                      self.p("Jasprit Bumrah"), 2300)
        self.A.retain(self.session, self.season, self.mumbai,
                      self.p("Sanju Samson"), 1800)
        self.A.retain(self.session, self.season, self.mumbai,
                      self.p("Tim David"), 1200)
        self.assertEqual(5300, self.A.retention_spent(self.session,
                                                      self.mumbai.id))
        self.assert_ledger_agrees()

    def test_overspending_names_what_is_left(self):
        self.A.retain(self.session, self.season, self.chennai,
                      self.p("Jasprit Bumrah"), 2900)
        self.A.retain(self.session, self.season, self.chennai,
                      self.p("Sanju Samson"), 1800)
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.retain(self.session, self.season, self.chennai,
                          self.p("Tim David"), 1200)
        self.assertIn("6 Cr", str(caught.exception))

    def test_unretain_frees_the_slot(self):
        lot = self.A.retain(self.session, self.season, self.mumbai,
                            self.p("Jasprit Bumrah"), 2300)
        self.A.unretain(self.session, self.season, self.mumbai, lot)
        self.assertIsNone(lot.retention_slot)
        self.assertEqual("elite", self.RN.slot_for(
            self.session, self.season, self.mumbai, 95)["key"])

    def test_the_two_slot_preset_has_a_flexible_slot(self):
        self.RN.save_slots(self.session, self.season, self.RN.preset(2))
        self.assertEqual(2, self.season.max_retentions)
        slot = self.RN.slot_for(self.session, self.season, self.mumbai, 74)
        self.assertEqual("flexible", slot["key"])


class SlotEditingTests(DynamicCase):

    def test_add_edit_move_remove(self):
        RN, s = self.RN, self.session
        RN.set_budget(s, self.season, 6000)
        RN.add_slot(s, self.season, "Uncapped", 70, 82, 400, "🧢")
        self.assertEqual(4, self.season.max_retentions)
        RN.edit_slot(s, self.season, "Uncapped", floor_lakh=500, high=80)
        _i, slot = RN.find_slot(self.season, "uncapped")
        self.assertEqual((500, 80), (slot["floor_lakh"], slot["max_rating"]))
        RN.move_slot(s, self.season, "Uncapped", 1)
        self.assertEqual("uncapped", RN.slots(self.season)[0]["key"])
        RN.remove_slot(s, self.season, "1")
        self.assertEqual(3, len(RN.slots(self.season)))

    def test_bad_slots_are_refused(self):
        with self.assertRaises(self.A.AuctionError):
            self.RN.add_slot(self.session, self.season, "Odd", 90, 80, 100)
        with self.assertRaises(self.A.AuctionError):
            self.RN.add_slot(self.session, self.season, "Elite", 70, 80, 100)
        with self.assertRaises(self.A.AuctionError):
            # 23 + 18 + 12 + 5 = 58 Cr of floors against a 53 Cr budget.
            self.RN.add_slot(self.session, self.season, "Extra", 70, 80, 500)

    def test_a_filled_slot_cannot_be_removed_or_reshaped(self):
        self.A.retain(self.session, self.season, self.mumbai,
                      self.p("Jasprit Bumrah"), 2300)
        with self.assertRaises(self.A.AuctionError):
            self.RN.remove_slot(self.session, self.season, "Elite")
        with self.assertRaises(self.A.AuctionError):
            self.RN.edit_slot(self.session, self.season, "Elite",
                              floor_lakh=2000)
        # Renaming it is harmless.
        self.RN.edit_slot(self.session, self.season, "Elite", label="Icon")
        self.assertEqual("Icon", self.RN.slots(self.season)[0]["label"])

    def test_rules_edit_and_reset(self):
        self.RN.update_rules(self.session, self.season, counter_pct=80,
                             chances=4)
        self.assertEqual(80, self.RN.rules(self.season)["counter_pct"])
        with self.assertRaises(self.A.AuctionError):
            self.RN.update_rules(self.session, self.season, chances=0)
        self.RN.reset_rules(self.session, self.season)
        self.assertEqual(3, self.RN.rules(self.season)["chances"])


class DemandTests(DynamicCase):

    def test_the_demand_meter_rises_with_rating(self):
        RN = self.RN
        self.assertEqual((1300, 1700), RN.demand_range(self.season, 83))
        self.assertEqual((2800, 3200), RN.demand_range(self.season, 96))
        self.assertEqual((2800, 3200), RN.demand_range(self.season, 99))
        lows = [RN.demand_range(self.season, r)[0] for r in range(83, 97)]
        self.assertEqual(lows, sorted(lows))
        self.assertIn("VERY HIGH", RN.demand_meter(self.season, 96))
        self.assertIn("FAIR", RN.demand_meter(self.season, 83))

    def test_personality_and_price_are_fixed_per_player(self):
        RN, p = self.RN, self.p("Rashid Khan")
        first = RN.personality_for(self.season, p.id, p.rating)
        self.assertEqual(first, RN.personality_for(self.season, p.id, p.rating))
        a = RN.minimum_acceptable(self.season, p.id, 93, "balanced", 0, 2300)
        b = RN.minimum_acceptable(self.season, p.id, 93, "balanced", 0, 2300)
        self.assertEqual(a, b)

    def test_no_superstar_below_the_line(self):
        for pid in range(1, 200):
            self.assertNotEqual(
                "superstar", self.RN.personality_for(self.season, pid, 90))

    def test_loyalty_lowers_the_price(self):
        RN, pid = self.RN, 4242
        fresh = RN.minimum_acceptable(self.season, pid, 90, "loyal", 0, 0)
        loyal = RN.minimum_acceptable(self.season, pid, 90, "loyal", 4, 0)
        self.assertLess(loyal, fresh)


class NegotiationTests(DynamicCase):

    def test_reject_counter_accept_ask(self):
        t = self.talk("Jasprit Bumrah", map_lakh=2600, personality="balanced")
        verdict, _line, _lot = self.RN.make_offer(
            self.session, self.season, t, 2300, ALICE)
        self.assertEqual(self.RN.REJECT, verdict)
        verdict, line, _lot = self.RN.make_offer(
            self.session, self.season, t, 2400, ALICE)
        self.assertEqual(self.RN.COUNTER, verdict)
        self.assertEqual(2600, t.counter_lakh)
        self.assertIn("26 Cr", line)
        lot = self.RN.accept_counter(self.session, self.season, t, ALICE)
        self.assertEqual(2600, lot.sold_price_lakh)
        self.assertEqual("elite", lot.retention_slot)
        self.assertEqual(self.RN.TALK_SIGNED, t.status)
        self.assertEqual(1, self.mumbai.retained_count)
        self.assert_ledger_agrees()

    def test_an_offer_at_the_price_is_accepted(self):
        t = self.talk("Sanju Samson", map_lakh=2100, personality="balanced")
        verdict, _line, lot = self.RN.make_offer(
            self.session, self.season, t, 2100, ALICE)
        self.assertEqual(self.RN.ACCEPT, verdict)
        self.assertEqual("premium", lot.retention_slot)

    def test_three_rejections_send_him_to_the_auction(self):
        t = self.talk("Jasprit Bumrah", map_lakh=3000, personality="balanced")
        for price in (2300, 2350):
            verdict, _l, _x = self.RN.make_offer(
                self.session, self.season, t, price, ALICE)
            self.assertEqual(self.RN.REJECT, verdict)
        verdict, _l, _x = self.RN.make_offer(
            self.session, self.season, t, 2400, ALICE)
        self.assertEqual(self.RN.WALKOUT, verdict)
        self.assertEqual(self.RN.TALK_FAILED, t.status)
        from models import AuctionLot
        lot = (self.session.query(AuctionLot)
               .filter(AuctionLot.season_id == self.season.id,
                       AuctionLot.player_id == t.player_id).one())
        self.assertEqual(self.A.LOT_QUEUED, lot.status)
        self.assertEqual(self.RN.WALKOUT_SET, lot.set_name)
        self.assertEqual(self.mumbai.id, lot.previous_franchise_id)
        for franchise in (self.mumbai, self.chennai):
            with self.assertRaises(self.A.AuctionError):
                self.RN.start_talk(self.session, self.season, franchise,
                                   self.p("Jasprit Bumrah"))
        # The slot is still open for somebody else.
        self.assertEqual("elite", self.RN.slot_for(
            self.session, self.season, self.mumbai, 95)["key"])

    def test_a_lowball_makes_him_dig_in(self):
        t = self.talk("Jasprit Bumrah", map_lakh=3200, personality="balanced")
        verdict, line, _x = self.RN.make_offer(
            self.session, self.season, t, 2300, ALICE)
        self.assertEqual(self.RN.REJECT, verdict)
        self.assertEqual(3375, t.map_lakh)
        self.assertIn("insulting", line)

    def test_offers_must_rise_and_respect_the_floor(self):
        t = self.talk("Jasprit Bumrah", map_lakh=3000, personality="balanced")
        with self.assertRaises(self.A.AuctionError):
            self.RN.make_offer(self.session, self.season, t, 2000, ALICE)
        self.RN.make_offer(self.session, self.season, t, 2500, ALICE)
        with self.assertRaises(self.A.AuctionError):
            self.RN.make_offer(self.session, self.season, t, 2500, ALICE)
        self.assertEqual(1, t.attempts)

    def test_only_the_franchise_negotiates(self):
        t = self.talk("Jasprit Bumrah")
        with self.assertRaises(self.A.AuctionError):
            self.RN.make_offer(self.session, self.season, t, 2500, BOB)

    def test_one_player_one_franchise_one_talk(self):
        self.talk("Jasprit Bumrah")
        with self.assertRaises(self.A.AuctionError):
            self.talk("Jasprit Bumrah", self.chennai)
        with self.assertRaises(self.A.AuctionError):
            self.talk("Sanju Samson")

    def test_money_minded_never_counters(self):
        t = self.talk("Jasprit Bumrah", map_lakh=2600, personality="money")
        verdict, _l, _x = self.RN.make_offer(
            self.session, self.season, t, 2500, ALICE)
        self.assertEqual(self.RN.REJECT, verdict)
        self.assertIsNone(t.counter_lakh)

    def test_a_superstar_snubs_a_first_offer_under_the_top(self):
        t = self.talk("Jasprit Bumrah", map_lakh=2500, personality="superstar")
        _low, high = self.RN.demand_range(self.season, 95)
        verdict, _l, _x = self.RN.make_offer(
            self.session, self.season, t, high - 100, ALICE)
        self.assertEqual(self.RN.REJECT, verdict)
        verdict, _l, _x = self.RN.make_offer(
            self.session, self.season, t, high - 50, ALICE)
        self.assertEqual(self.RN.ACCEPT, verdict)

    def test_walking_away_before_an_offer_is_free_after_is_final(self):
        t = self.talk("Jasprit Bumrah")
        self.RN.withdraw_talk(self.session, self.season, t, ALICE)
        self.assertEqual(self.RN.TALK_WITHDRAWN, t.status)
        t = self.talk("Jasprit Bumrah", map_lakh=3000, personality="balanced")
        self.RN.make_offer(self.session, self.season, t, 2300, ALICE)
        self.RN.withdraw_talk(self.session, self.season, t, ALICE)
        self.assertEqual(self.RN.TALK_FAILED, t.status)

    def test_an_admin_cancel_is_always_clean(self):
        t = self.talk("Jasprit Bumrah", map_lakh=3000, personality="balanced")
        self.RN.make_offer(self.session, self.season, t, 2300, ALICE)
        self.RN.withdraw_talk(self.session, self.season, t, admin=True)
        self.assertEqual(self.RN.TALK_WITHDRAWN, t.status)
        self.talk("Jasprit Bumrah")


class CommandTests(DynamicCase):

    def _run(self, handler, user_id, args=()):
        import asyncio
        from types import SimpleNamespace
        self.replies = getattr(self, "replies", [])

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
        asyncio.run(handler(update, context))
        return self.replies

    def _as_admin(self, handler, args=()):
        import os
        previous = os.environ.get("BOT_ADMIN_IDS")
        os.environ["BOT_ADMIN_IDS"] = str(CAROL)
        try:
            return self._run(handler, CAROL, args)
        finally:
            if previous is None:
                os.environ.pop("BOT_ADMIN_IDS", None)
            else:
                os.environ["BOT_ADMIN_IDS"] = previous

    def test_aretslot_add_and_list(self):
        from handlers import auction as H
        self._as_admin(H.aretrule_handler, ("budget", "60"))
        self._as_admin(H.aretslot_handler, ("add", "Uncapped", "|", "70-82",
                                            "|", "0.5"))
        self.session.expire_all()
        labels = [s["label"] for s in self.RN.slots(self.season)]
        self.assertIn("Uncapped", labels)
        self._as_admin(H.aretslot_handler, ())
        self.assertIn("Uncapped", self.replies[-1])

    def test_aretrule_sets_a_knob(self):
        from handlers import auction as H
        self._as_admin(H.aretrule_handler, ("counter", "85"))
        self.session.expire_all()
        self.assertEqual(85, self.RN.rules(self.season)["counter_pct"])

    def test_an_owner_negotiates_with_retain(self):
        from handlers import auction as H
        from models import Player
        from tests.test_auction_retention import _PID
        solo = Player(name=f"Talk Solo {next(_PID)}", rating=90,
                      category="Batsman", country="India", version="Base",
                      bat_hand="Right", bowl_hand="Right",
                      bowl_style="Medium Pacer", bat_rating=90,
                      bowl_rating=40, is_active=True)
        self.session.add(solo)
        self.session.commit()
        from tests._previous_league import link_squads
        link_squads(self.session, self.A, self.season,
                    {"Mumbai": [solo], "Chennai": [self.p("Jasprit Bumrah")]})
        self._run(H.retain_handler, ALICE, (solo.name.split()[-1],))
        self.assertIn("Retention talks", self.replies[-1])
        self.assertIn("Premium", self.replies[-1])
        self._run(H.retain_handler, ALICE, (solo.name, "|", "18"))
        self.session.expire_all()
        talk = self.RN.open_talk_for(self.session, self.season, self.mumbai)
        if talk is not None:
            self.assertEqual(1, talk.attempts)
        else:
            # A generous hidden price could make 18 Cr an instant yes.
            self.assertEqual(1, len(self.A.retained(self.session,
                                                    self.mumbai.id)))
        # Chennai never held him: refused before any talk opens.
        self._run(H.retain_handler, BOB, (solo.name, "|", "30"))
        self.assertIn("on Chennai&#x27;s squad last season", self.replies[-1])

    def test_aretmode_is_admin_only(self):
        from handlers import auction as H
        self._run(H.aretmode_handler, ALICE, ("classic",))
        self.assertIn("Only auction admins", self.replies[-1])


if __name__ == "__main__":
    unittest.main()

"""Unlimited stock in the shared player and trait markets.

``quantity = 0`` means "never runs out". That one convention has to hold in four
places or the market half-works:

  * the stock helpers, which every caller asks instead of comparing counts;
  * the buy paths, whose atomic UPDATE must not fence an unlimited slot;
  * the listing paths, which must not stamp SOLD OUT on something buyable;
  * the admin edit path, which has to be able to put a slot back to unlimited.

A limited run (a positive ``quantity``) still works exactly as it did, including
losing the race for the last copy — that is what the second half of these tests
pins, because "unlimited" must not have been implemented by deleting the stock
rules.
"""

import unittest

from services import global_market as gm


class _Slot:
    def __init__(self, quantity=0, purchased_count=0, **kw):
        self.id = kw.get("id", 1)
        self.slot_index = kw.get("slot_index", 0)
        self.quantity = quantity
        self.purchased_count = purchased_count
        self.is_active = True
        self.base_price = kw.get("base_price", 100)
        self.final_price = kw.get("final_price", 100)
        self.discount_pct = kw.get("discount_pct", 0)
        self.player_id = kw.get("player_id", 1)
        self.trait_id = kw.get("trait_id", 1)


class StockHelperTests(unittest.TestCase):
    def test_zero_quantity_is_unlimited_and_never_sells_out(self):
        slot = _Slot(quantity=0, purchased_count=9999)
        self.assertTrue(gm.is_unlimited(slot))
        self.assertFalse(gm.is_sold_out(slot))
        self.assertIsNone(gm.stock_left(slot))
        self.assertEqual(gm.stock_label(slot), gm.INFINITY)

    def test_a_negative_or_missing_quantity_reads_as_unlimited(self):
        for quantity in (-1, None, "", "junk"):
            with self.subTest(quantity=quantity):
                self.assertTrue(gm.is_unlimited(_Slot(quantity=quantity)))
                self.assertFalse(gm.is_sold_out(_Slot(quantity=quantity)))

    def test_a_limited_run_still_sells_out(self):
        slot = _Slot(quantity=3, purchased_count=2)
        self.assertFalse(gm.is_unlimited(slot))
        self.assertFalse(gm.is_sold_out(slot))
        self.assertEqual(gm.stock_left(slot), 1)
        self.assertEqual(gm.stock_label(slot), "1 left")

        slot.purchased_count = 3
        self.assertTrue(gm.is_sold_out(slot))
        self.assertEqual(gm.stock_left(slot), 0)

    def test_unlimited_is_the_documented_zero(self):
        self.assertEqual(gm.UNLIMITED, 0)


class _Result:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class _FakeSession:
    """Enough session to drive ``buy_trait`` / ``buy_player`` without a database.

    ``execute`` is the interesting part: it records the UPDATE it was handed so
    the test can assert whether a stock guard was attached, and applies the
    increment the way the database would.
    """

    def __init__(self, slot, trait=None, player=None, limited_race_loss=False):
        self.slot = slot
        self.trait = trait
        self.player = player
        self.added = []
        self.statements = []
        self.limited_race_loss = limited_race_loss

    def query(self, model):
        session = self

        class _Q:
            def __init__(self):
                pass

            def filter(self, *a):
                return self

            def first(self):
                return session.slot

            def get(self, _id):
                name = model.__name__
                if name == "Trait":
                    return session.trait
                if name == "Player":
                    return session.player
                return None

        return _Q()

    def execute(self, statement):
        self.statements.append(statement)
        # A limited slot's UPDATE carries the stock guard, so it is the only one
        # that can lose the race. Read off the compiled SQL rather than
        # SQLAlchemy's private _where_criteria, so a change to their internals
        # can't quietly turn this assertion into a no-op.
        guarded = "purchased_count <" in str(statement)
        if guarded and (self.limited_race_loss
                        or self.slot.purchased_count >= self.slot.quantity):
            return _Result(0)
        self.slot.purchased_count += 1
        return _Result(1)

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        pass


class _Trait:
    id = 1
    name = "Yorker Specialist"
    emoji = "🎯"
    category = "Bowling"
    rarity = "rare"
    effect_key = "bowl_yorker"
    base_price = None
    is_active = True


class _User:
    def __init__(self, gems=100_000):
        self.id = 1
        self.total_gems = gems
        self.total_coins = 10_000_000
        self.roster_count = 0


class BuyTraitStockTests(unittest.TestCase):
    def test_an_unlimited_trait_slot_can_be_bought_without_end(self):
        slot = _Slot(quantity=0, final_price=10)
        session = _FakeSession(slot, trait=_Trait())
        user = _User()
        for i in range(50):
            ok, msg = gm.buy_trait(session, user, 0)
            self.assertTrue(ok, f"buy {i} failed: {msg}")
        self.assertEqual(slot.purchased_count, 50)
        self.assertEqual(user.total_gems, 100_000 - 500)
        # Demand is still recorded even though nothing depletes.
        self.assertEqual(len(session.added), 100)  # inventory + audit per buy

    def test_an_unlimited_buy_carries_no_stock_guard(self):
        session = _FakeSession(_Slot(quantity=0, final_price=10), trait=_Trait())
        gm.buy_trait(session, _User(), 0)
        self.assertNotIn("purchased_count <", str(session.statements[0]))

    def test_a_limited_buy_does_carry_the_stock_guard(self):
        """The other half: the guard is what keeps a limited run race-safe."""
        session = _FakeSession(_Slot(quantity=3, final_price=10), trait=_Trait())
        gm.buy_trait(session, _User(), 0)
        self.assertIn("purchased_count <", str(session.statements[0]))

    def test_a_limited_trait_slot_still_runs_out(self):
        slot = _Slot(quantity=2, final_price=10)
        session = _FakeSession(slot, trait=_Trait())
        user = _User()
        self.assertTrue(gm.buy_trait(session, user, 0)[0])
        self.assertTrue(gm.buy_trait(session, user, 0)[0])
        ok, msg = gm.buy_trait(session, user, 0)
        self.assertFalse(ok)
        self.assertIn("Sold out", msg)
        self.assertEqual(slot.purchased_count, 2)

    def test_a_limited_slot_still_loses_the_race_atomically(self):
        """Two captains, one copy left: the UPDATE decides, not the read."""
        slot = _Slot(quantity=5, purchased_count=0, final_price=10)
        session = _FakeSession(slot, trait=_Trait(), limited_race_loss=True)
        ok, msg = gm.buy_trait(session, _User(), 0)
        self.assertFalse(ok)
        self.assertIn("Sold out", msg)

    def test_gems_are_still_required(self):
        session = _FakeSession(_Slot(quantity=0, final_price=500), trait=_Trait())
        ok, msg = gm.buy_trait(session, _User(gems=100), 0)
        self.assertFalse(ok)
        self.assertIn("Not enough gems", msg)


class TraitMarketPricingTests(unittest.TestCase):
    def test_the_discount_roll_never_undercuts_the_resale_floor(self):
        """The gem printer this roll used to leave open.

        A discount deeper than ``TRAIT_DISCOUNT_RANGE`` sells a trait for less
        than /selltrait pays back for it, and unlimited stock would make that an
        unbounded loop rather than a ten-copy annoyance.
        """
        from config import (TRAIT_DISCOUNT_RANGE, trait_list_price,
                            trait_sell_value)
        deepest = max(TRAIT_DISCOUNT_RANGE)
        for _ in range(300):
            self.assertLessEqual(gm.roll_trait_discount(), deepest)
        for rarity in ("common", "rare", "epic", "elite"):
            cheapest = trait_list_price(rarity) * (100 - deepest) // 100
            self.assertLess(trait_sell_value(1, rarity), cheapest, rarity)

    def test_elite_traits_are_rolled_far_less_often(self):
        class _T:
            def __init__(self, rarity, effect_key="bat_anchor"):
                self.rarity = rarity
                self.effect_key = effect_key

        # Distinct instances per slot — a repeated reference would make the
        # "no duplicate slots" assertion meaningless.
        pool = [_T(rarity)
                for rarity in ("common", "rare", "epic", "elite")
                for _ in range(10)]
        elite_seen = 0
        rounds = 400
        for _ in range(rounds):
            picked = gm.pick_traits_by_rarity(pool, 5)
            self.assertEqual(len(picked), 5)
            self.assertEqual(len({id(p) for p in picked}), 5, "no duplicate slots")
            elite_seen += sum(1 for p in picked if p.rarity == "elite")
        # An even sample of this pool would be 25% Elite; the weights should put
        # it far below that. (Weights are 50/30/15/5.)
        share = elite_seen / (rounds * 5)
        self.assertLess(share, 0.15, f"Elite showed up {share:.0%} of the time")

    def test_a_trait_price_follows_its_rarity(self):
        class _T:
            rarity = "elite"
            effect_key = "elite_goat"
            base_price = None

        from config import trait_list_price
        self.assertEqual(gm._calc_trait_price(_T()), trait_list_price("elite"))


class AdminSlotEditTests(unittest.TestCase):
    """The admin page has to be able to reach unlimited, and back."""

    class _Session:
        def __init__(self, slot):
            self.slot = slot

        def query(self, model):
            session = self

            class _Q:
                def get(self, _id):
                    return session.slot

            return _Q()

        def flush(self):
            pass

    def _edit(self, fn, **fields):
        slot = _Slot(quantity=5, purchased_count=5)
        ok, _msg = fn(self._Session(slot), 1, **fields)
        self.assertTrue(ok)
        return slot

    def test_setting_quantity_to_zero_makes_a_slot_unlimited_again(self):
        for fn in (gm.update_player_slot, gm.update_trait_slot):
            with self.subTest(fn.__name__):
                slot = self._edit(fn, quantity=0)
                self.assertTrue(gm.is_unlimited(slot))
                # A slot that had sold out is buyable again.
                self.assertFalse(gm.is_sold_out(slot))

    def test_a_negative_quantity_is_clamped_to_unlimited(self):
        for fn in (gm.update_player_slot, gm.update_trait_slot):
            with self.subTest(fn.__name__):
                self.assertEqual(self._edit(fn, quantity=-5).quantity, 0)

    def test_a_positive_quantity_still_sets_a_limited_run(self):
        for fn in (gm.update_player_slot, gm.update_trait_slot):
            with self.subTest(fn.__name__):
                slot = self._edit(fn, quantity=12)
                self.assertEqual(slot.quantity, 12)
                self.assertFalse(gm.is_unlimited(slot))

    def test_junk_leaves_the_quantity_alone(self):
        for fn in (gm.update_player_slot, gm.update_trait_slot):
            with self.subTest(fn.__name__):
                self.assertEqual(self._edit(fn, quantity="lots").quantity, 5)


if __name__ == "__main__":
    unittest.main()

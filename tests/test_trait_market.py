"""/selltrait and /tradetrait — the trait resale and trait-swap economy.

Both are priced off what a trait actually cost to build (the Lv.1 shop price plus
every upgrade paid on the way up), so the two tables can never drift apart from
the shop:

    Level        1      2      3      4      5
    buy        150    350    750  1,550  3,050   gems invested
    sell       127    297    637  1,317  2,592   always 15% below buy
    trade fee   15     25     45     85    160   each side

The rules that decide an outcome live in ``services.trait_trading_service``, so
these run with no Telegram and a fake session.
"""

import unittest

import config
from config import (
    TRAIT_MARKET_BUY_COST, TRAIT_SELL_DISCOUNT_PCT, TRAIT_TRADE_FEE_BASE,
    TRAIT_UPGRADE_COSTS, trait_buy_value, trait_sell_value, trait_trade_fee,
)
from services import trait_trading_service as tts

LEVELS = (1, 2, 3, 4, 5)


class PricingTests(unittest.TestCase):
    def test_buy_value_is_the_shop_price_plus_every_upgrade(self):
        self.assertEqual(trait_buy_value(1), TRAIT_MARKET_BUY_COST)
        running = TRAIT_MARKET_BUY_COST
        for level in sorted(TRAIT_UPGRADE_COSTS):
            running += TRAIT_UPGRADE_COSTS[level]
            self.assertEqual(trait_buy_value(level + 1), running)

    def test_selling_is_always_fifteen_percent_below_buy_value(self):
        for level in LEVELS:
            buy = trait_buy_value(level)
            self.assertEqual(trait_sell_value(level),
                             buy * (100 - TRAIT_SELL_DISCOUNT_PCT) // 100)
            # ...which is a real loss at every level, never a profit.
            self.assertLess(trait_sell_value(level), buy)

    def test_the_documented_tables(self):
        self.assertEqual([trait_buy_value(l) for l in LEVELS],
                         [150, 350, 750, 1550, 3050])
        self.assertEqual([trait_sell_value(l) for l in LEVELS],
                         [127, 297, 637, 1317, 2592])
        self.assertEqual([trait_trade_fee(l) for l in LEVELS],
                         [15, 25, 45, 85, 160])

    def test_a_level_one_swap_costs_the_specified_fifteen_gems(self):
        self.assertEqual(trait_trade_fee(1), TRAIT_TRADE_FEE_BASE)
        self.assertEqual(TRAIT_TRADE_FEE_BASE, 15)

    def test_resale_returns_more_than_it_used_to(self):
        """The 30%-haircut table this economy shipped with, pinned as a floor.

        Resale was raised because a 30% loss made /selltrait a last resort. If a
        later tuning pass ever drops it back to or below those numbers, that is
        a regression in the thing this change existed to fix, not a re-tune.
        """
        for level, old in zip(LEVELS, (105, 245, 525, 1085, 2135)):
            self.assertGreater(trait_sell_value(level), old, level)

    def test_swapping_costs_less_than_it_used_to(self):
        """Same guard from the other side: the old fee table is a ceiling."""
        for level, old in zip(LEVELS, (30, 50, 90, 170, 320)):
            self.assertLess(trait_trade_fee(level), old, level)

    def test_both_prices_rise_with_every_level(self):
        for low, high in zip(LEVELS, LEVELS[1:]):
            self.assertLess(trait_sell_value(low), trait_sell_value(high))
            self.assertLess(trait_trade_fee(low), trait_trade_fee(high))

    def test_a_swap_never_costs_more_than_a_fresh_trait_until_level_four(self):
        """The fee is meant to bite at the top end without being absurd."""
        for level in (1, 2, 3):
            self.assertLess(trait_trade_fee(level), TRAIT_MARKET_BUY_COST)
        self.assertGreater(trait_trade_fee(5), TRAIT_MARKET_BUY_COST)

    def test_a_swap_is_always_cheaper_than_selling_and_rebuying(self):
        """Otherwise nobody would ever use /tradetrait."""
        for level in LEVELS:
            loss_via_resale = trait_buy_value(level) - trait_sell_value(level)
            self.assertLess(trait_trade_fee(level), loss_via_resale)

    def test_junk_levels_are_clamped_not_crashes(self):
        self.assertEqual(trait_sell_value(None), trait_sell_value(1))
        self.assertEqual(trait_sell_value(0), trait_sell_value(1))
        self.assertEqual(trait_sell_value(99), trait_sell_value(5))
        self.assertEqual(trait_trade_fee("nonsense"), trait_trade_fee(1))

    def test_the_tables_follow_a_repriced_shop(self):
        """Change the shop price and resale/trading move with it."""
        original = config.TRAIT_MARKET_BUY_COST
        try:
            config.TRAIT_MARKET_BUY_COST = 300
            rebuilt = config._trait_buy_values()
            self.assertEqual(rebuilt[1], 300)
            self.assertEqual(rebuilt[2], 300 + TRAIT_UPGRADE_COSTS[1])
        finally:
            config.TRAIT_MARKET_BUY_COST = original


# ── Fakes ────────────────────────────────────────────────────────────

class FakeUser:
    def __init__(self, uid, gems, username="cap"):
        self.id = uid
        self.telegram_id = 900 + uid
        self.total_gems = gems
        self.total_coins = 0
        self.username = username
        self.first_name = username


class FakeTrait:
    def __init__(self, tid, name):
        self.id = tid
        self.name = name
        self.emoji = "✨"
        self.category = "batting"


class FakeInv:
    def __init__(self, iid, user_id, trait_id, level):
        self.id = iid
        self.user_id = user_id
        self.trait_id = trait_id
        self.level = level


class FakeSession:
    """Serves ``_owned_inventory_trait``'s two lookups and records deletes."""

    def __init__(self, invs, traits):
        self.invs = {i.id: i for i in invs}
        self.traits = {t.id: t for t in traits}
        self.deleted = []
        self.flushed = 0

    def query(self, model):
        session = self
        name = model.__name__

        class _Q:
            def __init__(self):
                self._filters = []

            def get(self, key):
                return session.traits.get(key) if name == "Trait" else None

            def filter(self, *conditions):
                self._filters.extend(conditions)
                return self

            def order_by(self, *a):
                return self

            def all(self):
                return []

            def first(self):
                # Only TraitInventory is looked up by filter in these paths;
                # the conditions carry the id + user_id being asked for.
                wanted = _wanted(self._filters)
                inv = session.invs.get(wanted.get("id"))
                if inv is None or inv in session.deleted:
                    return None
                if "user_id" in wanted and inv.user_id != wanted["user_id"]:
                    return None
                return inv

        return _Q()

    def delete(self, obj):
        self.deleted.append(obj)

    def add(self, obj):
        pass

    def flush(self):
        self.flushed += 1


def _wanted(conditions):
    """Pull ``{column: value}`` out of SQLAlchemy-style equality clauses."""
    out = {}
    for cond in conditions:
        try:
            column = cond.left.name
            value = cond.right.value
        except AttributeError:
            continue
        out[column] = value
    return out


class SellTraitTests(unittest.TestCase):
    def setUp(self):
        self.trait = FakeTrait(1, "Yorker Specialist")
        self.inv = FakeInv(10, user_id=1, trait_id=1, level=3)
        self.user = FakeUser(1, gems=500)
        self.session = FakeSession([self.inv], [self.trait])
        self._log = tts.log_activity
        tts.log_activity = lambda *a, **k: None

    def tearDown(self):
        tts.log_activity = self._log

    def test_selling_pays_the_level_price_and_removes_the_trait(self):
        ok, msg, detail = tts.sell_inventory_trait(self.session, self.user, 10)
        self.assertTrue(ok, msg)
        self.assertEqual(detail["gems"], trait_sell_value(3))
        self.assertEqual(detail["buy_value"], trait_buy_value(3))
        self.assertEqual(self.user.total_gems, 500 + trait_sell_value(3))
        self.assertIn(self.inv, self.session.deleted)
        self.assertIn("Yorker Specialist", detail["label"])

    def test_a_trait_that_is_not_yours_cannot_be_sold(self):
        other = FakeUser(2, gems=0)
        ok, msg, detail = tts.sell_inventory_trait(self.session, other, 10)
        self.assertFalse(ok)
        self.assertIsNone(detail)
        self.assertEqual(other.total_gems, 0)
        self.assertEqual(self.session.deleted, [])

    def test_selling_the_same_trait_twice_pays_once(self):
        ok, _msg, _d = tts.sell_inventory_trait(self.session, self.user, 10)
        self.assertTrue(ok)
        paid = self.user.total_gems
        ok2, msg2, _d2 = tts.sell_inventory_trait(self.session, self.user, 10)
        self.assertFalse(ok2)
        self.assertIn("isn't in your inventory", msg2)
        self.assertEqual(self.user.total_gems, paid)


class TradeTraitTests(unittest.TestCase):
    LEVEL = 2

    def setUp(self):
        self.t1 = FakeTrait(1, "Power Hitter")
        self.t2 = FakeTrait(2, "Death Bowler")
        self.inv1 = FakeInv(11, user_id=1, trait_id=1, level=self.LEVEL)
        self.inv2 = FakeInv(22, user_id=2, trait_id=2, level=self.LEVEL)
        self.fee = trait_trade_fee(self.LEVEL)
        self.u1 = FakeUser(1, gems=self.fee * 3, username="alpha")
        self.u2 = FakeUser(2, gems=self.fee * 3, username="beta")
        self.session = FakeSession([self.inv1, self.inv2], [self.t1, self.t2])
        self._log = tts.log_activity
        tts.log_activity = lambda *a, **k: None

    def tearDown(self):
        tts.log_activity = self._log

    def _swap(self):
        return tts.complete_trait_swap(self.session, self.u1, self.u2, 11, 22)

    def test_a_swap_moves_both_traits_and_charges_both_sides(self):
        before1, before2 = self.u1.total_gems, self.u2.total_gems
        result = self._swap()

        self.assertTrue(result["success"], result["message"])
        self.assertEqual(result["fee"], self.fee)
        self.assertEqual(self.inv1.user_id, 2)
        self.assertEqual(self.inv2.user_id, 1)
        self.assertEqual(self.u1.total_gems, before1 - self.fee)
        self.assertEqual(self.u2.total_gems, before2 - self.fee)

    def test_the_fee_leaves_the_economy(self):
        total_before = self.u1.total_gems + self.u2.total_gems
        self._swap()
        self.assertEqual(total_before - (self.u1.total_gems + self.u2.total_gems),
                         self.fee * 2)

    def test_levels_must_match(self):
        self.inv2.level = self.LEVEL + 1
        result = self._swap()
        self.assertFalse(result["success"])
        self.assertIn("same level", result["message"])
        self.assertEqual(self.inv1.user_id, 1)   # nothing moved

    def test_the_same_trait_at_the_same_level_is_refused(self):
        self.inv2.trait_id = self.inv1.trait_id
        result = self._swap()
        self.assertFalse(result["success"])
        self.assertIn("change nothing", result["message"])

    def test_a_captain_who_cannot_pay_gets_no_half_done_trade(self):
        self.u2.total_gems = self.fee - 1
        before1 = self.u1.total_gems
        result = self._swap()

        self.assertFalse(result["success"])
        self.assertIn("trade fee", result["message"])
        self.assertIn("beta", result["message"])
        self.assertEqual(self.inv1.user_id, 1)
        self.assertEqual(self.inv2.user_id, 2)
        self.assertEqual(self.u1.total_gems, before1)

    def test_a_trait_someone_no_longer_owns_cancels_the_trade(self):
        self.inv2.user_id = 3          # applied or traded away meanwhile
        result = self._swap()
        self.assertFalse(result["success"])
        self.assertIn("no longer in inventory", result["message"])

    def test_the_quote_matches_what_completion_charges(self):
        quote = tts.quote_trait_swap(self.session, self.u1, self.u2, 11, 22)
        self.assertTrue(quote["ok"])
        self.assertEqual(quote["fee"], self.fee)
        self.assertEqual(quote["level"], self.LEVEL)
        self.assertEqual(quote["fee"], self._swap()["fee"])

    def test_the_quote_refuses_what_completion_would_refuse(self):
        self.inv2.level = 5
        quote = tts.quote_trait_swap(self.session, self.u1, self.u2, 11, 22)
        self.assertFalse(quote["ok"])
        self.assertIn("same level", quote["message"])


if __name__ == "__main__":
    unittest.main()

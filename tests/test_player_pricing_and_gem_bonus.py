"""Selling returns 55% of the buy price, and elite signings pay gems back.

Two rules, one economy:

* A card is worth ``SELL_VALUE_PCT`` of what it costs. There is no separate
  hand-maintained sell column any more, so a re-priced card can't drift away
  from its own resale value — buy 100, sell 55, at every rating.
* Buying a card rated above 95 rebates 0.1% of the coins spent as gems: a 97
  OVR at 4,450,000 🪙 hands back 4,450 💎. The rebate is sized on what the
  buyer actually paid, so a discounted buy can never mint more than it cost.

The rebate is a limited-time offer rather than a permanent rule — an admin
opens and closes it, sets its rating and rate, and can give it a window that
opens and closes it on its own. ``OfferTests`` covers that state machine; the
rate arithmetic above it stays pure.
"""

import unittest
from datetime import datetime, timedelta

import config
from config import (
    BUY_VALUES, GEM_BONUS_BPS, GEM_BONUS_MIN_RATING, SELL_VALUE_PCT,
    get_buy_gem_bonus, get_buy_value, get_sell_value, sell_value_of,
)
from services import buy_bonus


class SellValueTests(unittest.TestCase):
    def test_the_headline_example(self):
        """Buy 100 → sell 55."""
        self.assertEqual(sell_value_of(100), 55)

    def test_the_rate_is_fifty_five_percent(self):
        self.assertEqual(SELL_VALUE_PCT, 55)

    def test_every_rating_returns_the_same_share(self):
        for rating, buy in BUY_VALUES.items():
            with self.subTest(rating=rating):
                self.assertEqual(get_sell_value(rating),
                                 (buy * SELL_VALUE_PCT + 50) // 100)

    def test_selling_never_beats_buying(self):
        """The loop has to lose money, or it's a coin printer."""
        for rating in BUY_VALUES:
            with self.subTest(rating=rating):
                self.assertLess(get_sell_value(rating), get_buy_value(rating))

    def test_the_cheapest_card_is_still_worth_something(self):
        self.assertGreater(get_sell_value(min(BUY_VALUES)), 0)

    def test_an_unknown_rating_falls_back_instead_of_crashing(self):
        self.assertEqual(get_buy_value(3), config.FALLBACK_BUY_VALUE)
        self.assertEqual(get_sell_value(3),
                         sell_value_of(config.FALLBACK_BUY_VALUE))

    def test_the_legacy_pair_table_reports_the_new_sell(self):
        """``BUY_SELL`` is kept for older callers — it must not go stale."""
        for rating, (buy, sell) in config.BUY_SELL.items():
            with self.subTest(rating=rating):
                self.assertEqual(buy, get_buy_value(rating))
                self.assertEqual(sell, get_sell_value(rating))


class GemBonusMathTests(unittest.TestCase):
    def test_the_headline_example(self):
        """97 OVR costs 4,450,000 🪙 and pays back 4,450 💎."""
        self.assertEqual(get_buy_value(97), 4_450_000)
        self.assertEqual(get_buy_gem_bonus(97), 4_450)

    def test_the_rate_is_a_tenth_of_a_percent(self):
        self.assertEqual(GEM_BONUS_BPS / 10_000, 0.001)

    def test_only_cards_above_95_earn_it(self):
        self.assertEqual(GEM_BONUS_MIN_RATING, 96)
        self.assertEqual(get_buy_gem_bonus(95), 0)
        self.assertEqual(get_buy_gem_bonus(94), 0)
        self.assertGreater(get_buy_gem_bonus(96), 0)
        self.assertGreater(get_buy_gem_bonus(100), 0)

    def test_it_scales_with_what_was_actually_paid(self):
        """A discounted buy rebates less — the perk can't out-earn its cost."""
        full = get_buy_gem_bonus(97)
        half = get_buy_gem_bonus(97, get_buy_value(97) // 2)
        self.assertEqual(half, full // 2)

    def test_a_free_card_rebates_nothing(self):
        self.assertEqual(get_buy_gem_bonus(100, 0), 0)
        self.assertEqual(get_buy_gem_bonus(100, -5_000), 0)

    def test_junk_ratings_are_zero_not_a_crash(self):
        self.assertEqual(get_buy_gem_bonus(None), 0)
        self.assertEqual(get_buy_gem_bonus("elite"), 0)

    def test_the_rebate_is_always_smaller_than_the_price(self):
        for rating in BUY_VALUES:
            with self.subTest(rating=rating):
                self.assertLess(get_buy_gem_bonus(rating),
                                get_buy_value(rating))


class FakeUser:
    def __init__(self, gems=0):
        self.id = 7
        self.total_gems = gems


class FakePlayer:
    def __init__(self, rating):
        self.id = 42
        self.name = "Test Player"
        self.rating = rating


class FakeSession:
    """Just enough session for ``log_activity`` to add its row."""

    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)


def an_offer(**kw):
    """The default live offer, with any term overridden."""
    kw.setdefault("active", True)
    kw.setdefault("min_rating", GEM_BONUS_MIN_RATING)
    kw.setdefault("bps", GEM_BONUS_BPS)
    return buy_bonus.Offer(**kw)


class AwardTests(unittest.TestCase):
    def test_an_elite_buy_credits_the_gems(self):
        user, session = FakeUser(gems=10), FakeSession()
        gems = buy_bonus.award_buy_gem_bonus(
            session, user, FakePlayer(97), get_buy_value(97), offer=an_offer())
        self.assertEqual(gems, 4_450)
        self.assertEqual(user.total_gems, 4_460)
        self.assertEqual(len(session.added), 1)  # the audit row

    def test_an_ordinary_buy_touches_nothing(self):
        user, session = FakeUser(gems=10), FakeSession()
        gems = buy_bonus.award_buy_gem_bonus(
            session, user, FakePlayer(90), get_buy_value(90), offer=an_offer())
        self.assertEqual(gems, 0)
        self.assertEqual(user.total_gems, 10)
        self.assertEqual(session.added, [])

    def test_a_closed_offer_pays_nothing_to_anyone(self):
        user, session = FakeUser(gems=10), FakeSession()
        gems = buy_bonus.award_buy_gem_bonus(
            session, user, FakePlayer(100), get_buy_value(100),
            offer=an_offer(active=False))
        self.assertEqual(gems, 0)
        self.assertEqual(user.total_gems, 10)
        self.assertEqual(session.added, [])

    def test_a_null_gem_balance_starts_from_zero(self):
        user, session = FakeUser(gems=None), FakeSession()
        gems = buy_bonus.award_buy_gem_bonus(
            session, user, FakePlayer(100), get_buy_value(100),
            offer=an_offer())
        self.assertEqual(user.total_gems, gems)

    def test_the_admins_terms_are_what_gets_paid(self):
        """A re-tuned offer pays its own rate, not the baked-in one."""
        user, session = FakeUser(), FakeSession()
        gems = buy_bonus.award_buy_gem_bonus(
            session, user, FakePlayer(90), get_buy_value(90),
            offer=an_offer(min_rating=90, bps=100))     # 90+, 1%
        self.assertEqual(gems, get_buy_value(90) // 100)

    def test_the_lines_only_appear_when_a_bonus_was_paid(self):
        live = an_offer()
        self.assertIn("4,450", buy_bonus.bonus_line(4_450, offer=live))
        self.assertEqual(buy_bonus.bonus_line(0, offer=live), "")
        self.assertIn("4,450", buy_bonus.teaser_line(97, offer=live))
        self.assertEqual(buy_bonus.teaser_line(90, offer=live), "")

    def test_a_closed_offer_advertises_nothing(self):
        """The card must go quiet the moment an admin shuts the offer off."""
        closed = an_offer(active=False)
        self.assertEqual(buy_bonus.teaser_line(97, offer=closed), "")

    def test_a_closing_offer_shows_its_countdown(self):
        now = datetime(2026, 1, 1, 12, 0)
        live = an_offer(now=now, ends_at=now + timedelta(hours=3, minutes=20))
        self.assertIn("ends in 3h 20m", buy_bonus.teaser_line(97, offer=live))
        self.assertIn("ends in 3h 20m", buy_bonus.bonus_line(4_450, offer=live))

    def test_an_open_ended_offer_shows_no_countdown(self):
        self.assertNotIn("ends in", buy_bonus.teaser_line(97, offer=an_offer()))


class FakeConfigRow:
    def __init__(self, **kw):
        self.gem_bonus_enabled = kw.get("enabled", True)
        self.gem_bonus_min_rating = kw.get("min_rating", 96)
        self.gem_bonus_bps = kw.get("bps", 10)
        self.gem_bonus_starts_at = kw.get("starts_at")
        self.gem_bonus_ends_at = kw.get("ends_at")


class ConfigSession:
    """A session whose only job is to hand back one GameConfig row."""

    def __init__(self, row):
        self.row = row

    def query(self, *a, **k):
        return self

    def first(self):
        if isinstance(self.row, Exception):
            raise self.row
        return self.row


class OfferTests(unittest.TestCase):
    """The offer's state machine: open, closed, scheduled, expired."""

    NOW = datetime(2026, 1, 1, 12, 0)

    def _offer(self, **kw):
        return buy_bonus.current_offer(
            ConfigSession(FakeConfigRow(**kw)), now=self.NOW)

    def test_open_with_no_window_is_live(self):
        offer = self._offer()
        self.assertTrue(offer.active)
        self.assertIsNone(offer.seconds_left)
        self.assertEqual(offer.gems_for(97), 4_450)

    def test_the_admin_switch_closes_it(self):
        offer = self._offer(enabled=False)
        self.assertFalse(offer.active)
        self.assertEqual(offer.gems_for(97), 0)

    def test_it_waits_for_its_start_time(self):
        offer = self._offer(starts_at=self.NOW + timedelta(hours=1))
        self.assertFalse(offer.active)
        self.assertTrue(offer.scheduled)
        self.assertEqual(offer.gems_for(97), 0)

    def test_it_opens_once_the_start_time_passes(self):
        offer = self._offer(starts_at=self.NOW - timedelta(minutes=1))
        self.assertTrue(offer.active)

    def test_it_closes_itself_at_the_end_time(self):
        offer = self._offer(ends_at=self.NOW)
        self.assertFalse(offer.active)
        self.assertFalse(offer.scheduled)
        self.assertEqual(offer.gems_for(97), 0)

    def test_a_running_window_reports_the_time_left(self):
        offer = self._offer(ends_at=self.NOW + timedelta(hours=2))
        self.assertTrue(offer.active)
        self.assertEqual(offer.seconds_left, 7200)

    def test_the_admins_rating_decides_who_qualifies(self):
        offer = self._offer(min_rating=90)
        self.assertGreater(offer.gems_for(90), 0)
        self.assertEqual(offer.gems_for(89), 0)

    def test_the_admins_rate_decides_the_payout(self):
        offer = self._offer(bps=50)                       # 0.5%
        self.assertEqual(offer.gems_for(97), get_buy_value(97) * 50 // 10_000)
        self.assertEqual(offer.percent, 0.5)

    def test_a_missing_config_row_falls_back_to_the_baked_rate(self):
        offer = buy_bonus.current_offer(ConfigSession(None), now=self.NOW)
        self.assertTrue(offer.active)
        self.assertEqual(offer.min_rating, GEM_BONUS_MIN_RATING)

    def test_an_unreadable_config_keeps_paying_rather_than_going_quiet(self):
        """A database hiccup must not silently withdraw a promised bonus."""
        offer = buy_bonus.current_offer(
            ConfigSession(RuntimeError("db down")), now=self.NOW)
        self.assertTrue(offer.active)
        self.assertEqual(offer.gems_for(97), 4_450)

    def test_null_terms_fall_back_rather_than_crashing(self):
        offer = self._offer(min_rating=None, bps=None)
        self.assertEqual(offer.min_rating, GEM_BONUS_MIN_RATING)
        self.assertEqual(offer.bps, GEM_BONUS_BPS)


class CountdownTests(unittest.TestCase):
    def test_it_reads_the_way_a_person_would_say_it(self):
        cases = {
            30: "under a minute",
            60 * 45: "45m",
            3600 * 3 + 60 * 20: "3h 20m",
            3600 * 5: "5h",
            86400 * 2 + 3600 * 4: "2d 4h",
            86400 * 3: "3d",
        }
        for seconds, expected in cases.items():
            with self.subTest(seconds=seconds):
                self.assertEqual(buy_bonus.format_time_left(seconds), expected)

    def test_no_end_time_means_no_countdown(self):
        self.assertEqual(buy_bonus.format_time_left(None), "")


class MarketPriceFloorTests(unittest.TestCase):
    """A slot must never sell for less than releasing the card pays back.

    Releasing pays ``get_sell_value`` whatever the owner paid, so a cheaper
    slot is a coin printer — buy, release, buy again, on unlimited stock.
    """

    class Slot:
        def __init__(self, base, final=None):
            self.slot_index = 0
            self.base_price = base
            self.final_price = base if final is None else final

    class Buyer:
        """A free member — no membership discount."""
        subscription_tier = None
        subscription_expires_at = None

    class Diamond(Buyer):
        subscription_tier = "diamond"

    def _prices(self, slot, user=None):
        from services.global_market import player_slot_prices
        return player_slot_prices(user or self.Buyer(), slot, FakePlayer(97))

    def test_the_floor_sits_just_above_resale(self):
        from config import MARKET_PRICE_FLOOR_MARGIN, market_price_floor
        self.assertEqual(market_price_floor(97),
                         get_sell_value(97) + MARKET_PRICE_FLOOR_MARGIN)

    def test_a_normally_priced_slot_is_untouched(self):
        listed, price = self._prices(self.Slot(get_buy_value(97)))
        self.assertEqual(listed, get_buy_value(97))
        self.assertEqual(price, get_buy_value(97))

    def test_an_underpriced_sale_is_lifted_to_the_floor(self):
        from config import market_price_floor
        listed, price = self._prices(self.Slot(get_buy_value(97), 2_000_000))
        self.assertEqual(price, market_price_floor(97))
        self.assertEqual(listed, market_price_floor(97))
        self.assertGreater(price, get_sell_value(97))

    def test_a_custom_listing_price_is_floored_too(self):
        from config import market_price_floor
        _listed, price = self._prices(self.Slot(1_000))
        self.assertEqual(price, market_price_floor(97))

    def test_the_round_trip_always_loses_money(self):
        """The property the floor exists to protect, at every rating."""
        from config import market_price_floor
        for rating in BUY_VALUES:
            with self.subTest(rating=rating):
                self.assertGreater(market_price_floor(rating),
                                   get_sell_value(rating))

    def test_a_membership_discount_cannot_dive_under_the_floor(self):
        """The floor lands after the discount, not before it."""
        from config import market_price_floor
        slot = self.Slot(get_buy_value(97), market_price_floor(97))
        _listed, price = self._prices(slot, self.Diamond())
        self.assertGreaterEqual(price, market_price_floor(97))

    def test_a_discount_on_a_normal_price_still_works(self):
        """The floor must not quietly cancel the membership perk."""
        slot = self.Slot(get_buy_value(97))
        _l, free_price = self._prices(slot, self.Buyer())
        _l, dia_price = self._prices(slot, self.Diamond())
        self.assertLess(dia_price, free_price)


class UndoRecordTests(unittest.TestCase):
    """The undo has to know about the rebate, or buy-and-undo prints gems."""

    def _record(self, **kw):
        import json

        from services import undo_service

        class _Query:
            def filter(self, *a, **k):
                return self

            def first(self):
                return None

        session = FakeSession()
        session.query = lambda *a, **k: _Query()
        undo_service.record_buy(
            session, 7, roster_id=1, player_id=42,
            player_name="Test Player", rating=97, price=4_450_000, **kw)
        return json.loads(session.added[0].payload)

    def test_the_rebate_is_remembered(self):
        self.assertEqual(self._record(gem_bonus=4_450)["gem_bonus"], 4_450)

    def test_an_ordinary_buy_records_no_rebate(self):
        self.assertEqual(self._record()["gem_bonus"], 0)


if __name__ == "__main__":
    unittest.main()

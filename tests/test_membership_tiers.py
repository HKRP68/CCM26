"""Tests for the Bronze and Diamond membership tiers.

Pins the four-tier ladder (bronze < silver < platinum < diamond), the perks each
tier unlocks, the balanced upgrade paths between them, and the rule that
Autoplay stays locked for free users.
"""

import sys
import types
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace


def _load_service():
    # Real config carries the SUBSCRIPTION_TIERS table the service reads.
    # python-dotenv isn't installed in the test env — stub load_dotenv.
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *a, **k: None
    sys.modules.setdefault("dotenv", dotenv)
    sys.modules.pop("services.subscription_service", None)
    # Stub the lazily-imported side effects so no DB/pack catalogue is needed.
    activity = types.ModuleType("services.activity_service")
    activity.log_activity = lambda *a, **k: None
    sys.modules["services.activity_service"] = activity
    pack = types.ModuleType("services.pack_service")
    pack.list_packs = lambda *a, **k: []          # empty catalogue → packs skipped
    pack.grant_pack = lambda *a, **k: None
    sys.modules["services.pack_service"] = pack

    from services import subscription_service
    return subscription_service


def _member(tier, **kw):
    return SimpleNamespace(
        id=kw.pop("id", 1),
        total_coins=kw.pop("total_coins", 0),
        total_gems=kw.pop("total_gems", 0),
        quest_points=kw.pop("quest_points", 0),
        subscription_tier=tier,
        subscription_expires_at=(None if tier == "none"
                                 else datetime.utcnow() + timedelta(days=20)),
        subscription_activated_at=datetime.utcnow(),
        **kw)


class TierLadderTests(unittest.TestCase):
    def setUp(self):
        self.svc = _load_service()

    def test_all_four_tiers_exist_cheapest_first(self):
        self.assertEqual(list(self.svc.VALID_TIERS),
                         ["bronze", "silver", "platinum", "diamond"])

    def test_ranks_follow_price(self):
        ranks = [self.svc.tier_rank(t) for t in self.svc.VALID_TIERS]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(self.svc.tier_rank("none"), 0)
        self.assertGreater(self.svc.tier_rank("diamond"),
                           self.svc.tier_rank("platinum"))

    def test_prices(self):
        from config import SUBSCRIPTION_TIERS
        self.assertEqual(SUBSCRIPTION_TIERS["bronze"]["price_inr"], 19)
        self.assertEqual(SUBSCRIPTION_TIERS["diamond"]["price_inr"], 149)

    def test_upgrade_targets_are_the_higher_tiers(self):
        self.assertEqual(self.svc.upgrade_targets("bronze"),
                         ["silver", "platinum", "diamond"])
        self.assertEqual(self.svc.upgrade_targets("platinum"), ["diamond"])
        self.assertEqual(self.svc.upgrade_targets("diamond"), [])
        # A free user can be "upgraded" into anything (falls back to activate).
        self.assertEqual(self.svc.upgrade_targets("none"),
                         ["bronze", "silver", "platinum", "diamond"])


class BronzePerksTests(unittest.TestCase):
    def setUp(self):
        self.svc = _load_service()
        self.user = _member("bronze")

    def test_instant_bundle(self):
        from config import SUBSCRIPTION_TIERS
        instant = SUBSCRIPTION_TIERS["bronze"]["instant"]
        self.assertEqual(instant["coins"], 29000)
        self.assertEqual(instant["gems"], 150)

    def test_activation_credits_the_bundle(self):
        u = _member("none")
        self.svc.activate(None, u, "bronze")
        self.assertEqual(u.subscription_tier, "bronze")
        self.assertEqual(u.total_coins, 29000)
        self.assertEqual(u.total_gems, 150)

    def test_mysterybox_every_15_days(self):
        self.assertEqual(self.svc.mysterybox_cooldown_seconds(self.user),
                         15 * 86400)

    def test_premium_commands_unlocked(self):
        # /autobuild and /wpmbot are part of the ₹19 tier.
        self.assertTrue(self.svc.has_premium_commands(self.user))

    def test_no_autoplay_no_market_discount_no_top_drops(self):
        self.assertFalse(self.svc.has_autoplay(self.user))
        self.assertEqual(self.svc.market_discount_pct(self.user), 0)
        self.assertFalse(self.svc.has_weekly_card(self.user))
        self.assertIsNone(self.svc.coin_chest_config(self.user))
        self.assertEqual(self.svc.daily_login_multiplier(self.user), 1)


class DiamondPerksTests(unittest.TestCase):
    def setUp(self):
        self.svc = _load_service()
        self.user = _member("diamond")

    def test_instant_bundle(self):
        from config import SUBSCRIPTION_TIERS
        instant = SUBSCRIPTION_TIERS["diamond"]["instant"]
        self.assertEqual(instant["coins"], 2000000)
        self.assertEqual(instant["gems"], 1500)
        self.assertEqual(instant["quest_points"], 1500)
        self.assertEqual([p.lower() for p in instant["packs"]],
                         ["legend pack", "ultimate legend pack"])

    def test_activation_credits_the_bundle(self):
        u = _member("none")
        self.svc.activate(None, u, "diamond")
        self.assertEqual(u.total_coins, 2000000)
        self.assertEqual(u.total_gems, 1500)
        self.assertEqual(u.quest_points, 1500)

    def test_mysterybox_every_2_days(self):
        self.assertEqual(self.svc.mysterybox_cooldown_seconds(self.user),
                         2 * 86400)

    def test_weekly_card_and_five_chests_per_7_days(self):
        self.assertTrue(self.svc.has_weekly_card(self.user))
        chests = self.svc.coin_chest_config(self.user)
        self.assertEqual(chests["count"], 5)
        self.assertEqual(chests["cooldown_days"], 7)

    def test_ten_percent_market_discount(self):
        self.assertEqual(self.svc.market_discount_pct(self.user), 10)
        # 10% off the base price, beating Platinum's 5% final_price column.
        self.assertEqual(self.svc.market_price(self.user, 100000, 95000), 90000)

    def test_double_daily_login_reward(self):
        self.assertEqual(self.svc.daily_login_multiplier(self.user), 2)

    def test_premium_commands_and_autoplay(self):
        self.assertTrue(self.svc.has_premium_commands(self.user))
        self.assertTrue(self.svc.has_autoplay(self.user))


class MarketDiscountTests(unittest.TestCase):
    def setUp(self):
        self.svc = _load_service()

    def test_free_and_low_tiers_pay_base_price(self):
        for tier in ("none", "bronze", "silver"):
            self.assertEqual(
                self.svc.market_price(_member(tier), 100000, 95000), 100000,
                f"{tier} must pay the full base price")

    def test_platinum_keeps_its_five_percent(self):
        self.assertEqual(
            self.svc.market_price(_member("platinum"), 100000, 95000), 95000)

    def test_expired_diamond_pays_full_price(self):
        lapsed = _member("diamond")
        lapsed.subscription_expires_at = datetime.utcnow() - timedelta(days=1)
        self.assertEqual(self.svc.market_price(lapsed, 100000, 95000), 100000)


class UpgradePathTests(unittest.TestCase):
    """Every upgrade must be a top-up, and hopping must never beat going direct."""

    def setUp(self):
        self.svc = _load_service()

    def _coins(self, frm, to):
        return self.svc.upgrade_rewards(frm, to)["coins"]

    def test_every_step_up_has_an_explicit_topup(self):
        for frm in self.svc.VALID_TIERS:
            for to in self.svc.upgrade_targets(frm):
                r = self.svc.upgrade_rewards(frm, to)
                self.assertGreater(r["coins"], 0, f"{frm}→{to} grants no coins")

    def test_topup_is_smaller_than_a_fresh_activation(self):
        from config import SUBSCRIPTION_TIERS
        for frm in self.svc.VALID_TIERS:
            for to in self.svc.upgrade_targets(frm):
                self.assertLess(self.svc.upgrade_rewards(frm, to)["coins"],
                                SUBSCRIPTION_TIERS[to]["instant"]["coins"],
                                f"{frm}→{to} pays out like a second subscription")

    def test_hopping_never_pays_more_than_going_direct(self):
        # bronze → silver → platinum → diamond vs bronze → diamond
        hop = (self._coins("bronze", "silver")
               + self._coins("silver", "platinum")
               + self._coins("platinum", "diamond"))
        self.assertLessEqual(hop, self._coins("bronze", "diamond"))
        self.assertLessEqual(self._coins("bronze", "silver")
                             + self._coins("silver", "diamond"),
                             self._coins("bronze", "diamond"))
        self.assertLessEqual(self._coins("bronze", "platinum")
                             + self._coins("platinum", "diamond"),
                             self._coins("bronze", "diamond"))

    def test_silver_to_platinum_topup_is_unchanged(self):
        r = self.svc.upgrade_rewards("silver", "platinum")
        self.assertEqual((r["coins"], r["gems"], r["quest_points"]),
                         (451000, 251, 251))

    def test_platinum_to_diamond_credits_only_the_new_pack(self):
        r = self.svc.upgrade_rewards("platinum", "diamond")
        self.assertEqual(r["coins"], 500000)
        # Platinum already granted the Legend Pack.
        self.assertEqual([p.lower() for p in r["packs"]],
                         ["ultimate legend pack"])

    def test_silver_to_diamond_upgrade_keeps_remaining_time(self):
        u = _member("silver", total_coins=1000)
        expiry_before = u.subscription_expires_at
        res = self.svc.upgrade(None, u, "diamond")
        self.assertEqual(u.subscription_tier, "diamond")
        self.assertEqual(res["from_tier"], "silver")
        self.assertEqual(u.subscription_expires_at, expiry_before)
        self.assertEqual(u.total_coins, 1000 + 975000)

    def test_bronze_can_step_to_any_higher_tier(self):
        for target in ("silver", "platinum", "diamond"):
            u = _member("bronze")
            res = self.svc.upgrade(None, u, target)
            self.assertEqual(u.subscription_tier, target)
            self.assertEqual(res["from_tier"], "bronze")

    def test_cannot_downgrade_via_upgrade(self):
        with self.assertRaises(ValueError):
            self.svc.upgrade(None, _member("diamond"), "platinum")
        with self.assertRaises(ValueError):
            self.svc.upgrade(None, _member("silver"), "bronze")


class CooldownLadderTests(unittest.TestCase):
    """Pins the exact per-tier cooldowns for /claim, /gspin and /daily.

    One config number (cooldown_reduction_min_per_hour) drives all three, so
    these assertions are what stops a retune from silently skewing a command.
    """

    # command → (base seconds, {tier: expected seconds})
    EXPECTED = {
        "/claim": (3600, {"none": 3600, "bronze": 3600, "silver": 55 * 60,
                          "platinum": 50 * 60, "diamond": 45 * 60}),
        "/gspin": (28800, {"none": 28800, "bronze": 28800,
                           "silver": 7 * 3600 + 20 * 60,
                           "platinum": 6 * 3600 + 40 * 60,
                           "diamond": 6 * 3600}),
        "/daily": (86400, {"none": 86400, "bronze": 86400,
                           "silver": 22 * 3600, "platinum": 20 * 3600,
                           "diamond": 18 * 3600}),
    }

    def setUp(self):
        self.svc = _load_service()

    def test_base_cooldowns_match_config(self):
        from config import CLAIM_COOLDOWN, GSPIN_COOLDOWN, DAILY_COOLDOWN
        self.assertEqual(CLAIM_COOLDOWN, self.EXPECTED["/claim"][0])
        self.assertEqual(GSPIN_COOLDOWN, self.EXPECTED["/gspin"][0])
        self.assertEqual(DAILY_COOLDOWN, self.EXPECTED["/daily"][0])

    def test_every_tier_cooldown_matches_the_spec(self):
        for command, (base, per_tier) in self.EXPECTED.items():
            for tier, expected in per_tier.items():
                self.assertEqual(
                    self.svc.cooldown_seconds(_member(tier), base), expected,
                    f"{command} on {tier}")

    def test_bronze_runs_on_free_user_timers(self):
        for base in (3600, 28800, 86400):
            self.assertEqual(self.svc.cooldown_seconds(_member("bronze"), base),
                             base)

    def test_cooldowns_shorten_as_the_tier_rises(self):
        for base in (3600, 28800, 86400):
            values = [self.svc.cooldown_seconds(_member(t), base)
                      for t in ("none", "bronze", "silver", "platinum", "diamond")]
            self.assertEqual(values, sorted(values, reverse=True))

    def test_expired_tier_falls_back_to_the_base_cooldown(self):
        lapsed = _member("diamond")
        lapsed.subscription_expires_at = datetime.utcnow() - timedelta(days=1)
        self.assertEqual(self.svc.cooldown_seconds(lapsed, 3600), 3600)


class AutoplayLockTests(unittest.TestCase):
    """Autoplay is paid-only — free users must stay locked out of the Mini App
    toggle (the Arena reads this same helper to render the 🔒 pill)."""

    def setUp(self):
        self.svc = _load_service()

    def test_free_user_has_no_autoplay(self):
        self.assertFalse(self.svc.has_autoplay(_member("none")))
        self.assertFalse(self.svc.has_autoplay(None))

    def test_expired_subscriber_has_no_autoplay(self):
        lapsed = _member("diamond")
        lapsed.subscription_expires_at = datetime.utcnow() - timedelta(days=1)
        self.assertFalse(self.svc.has_autoplay(lapsed))

    def test_paid_tiers_from_silver_up_have_autoplay(self):
        self.assertFalse(self.svc.has_autoplay(_member("bronze")))
        for tier in ("silver", "platinum", "diamond"):
            self.assertTrue(self.svc.has_autoplay(_member(tier)), tier)


class MessagingTests(unittest.TestCase):
    def setUp(self):
        self.svc = _load_service()

    def test_upsell_lists_every_tier_with_its_price(self):
        msg = self.svc.premium_required_message("The Mystery Box")
        for bit in ("Bronze", "₹19", "Silver", "₹59", "Platinum", "₹99",
                    "Diamond", "₹149"):
            self.assertIn(bit, msg)

    def test_upsell_min_tier_hides_tiers_without_the_perk(self):
        msg = self.svc.premium_required_message("Coin Chests",
                                                min_tier="platinum")
        self.assertIn("Platinum", msg)
        self.assertIn("Diamond", msg)
        self.assertNotIn("Bronze", msg)
        self.assertNotIn("Silver", msg)

    def test_tier_label(self):
        self.assertIn("Diamond", self.svc.tier_label("diamond"))
        self.assertEqual(self.svc.tier_label("none"), "Free")


if __name__ == "__main__":
    unittest.main()

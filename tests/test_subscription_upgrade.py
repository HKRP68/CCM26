"""Tests for Silver → Platinum subscription upgrades.

An upgrade must credit only "the rest" — the higher tier's instant bundle
minus what the lower tier already granted — and must preserve the member's
remaining paid time rather than resetting the clock or re-granting the full
bundle.
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


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.svc = _load_service()

    def _silver_user(self, days_left=20):
        return SimpleNamespace(
            id=1, total_coins=1000, total_gems=10, quest_points=5,
            subscription_tier="silver",
            subscription_expires_at=datetime.utcnow() + timedelta(days=days_left),
            subscription_activated_at=datetime.utcnow() - timedelta(days=10),
        )

    def test_raw_delta_is_platinum_minus_silver(self):
        # instant_delta is the fallback math (no explicit upgrade_from bundle).
        d = self.svc.instant_delta("silver", "platinum")
        # 1,000,000 − 49,000 ; 1000 − 499 ; 1000 − 499
        self.assertEqual(d["coins"], 951000)
        self.assertEqual(d["gems"], 501)
        self.assertEqual(d["quest_points"], 501)
        # Star Pack already granted by Silver is dropped; Legend Pack is new.
        self.assertEqual([p.lower() for p in d["packs"]], ["legend pack"])

    def test_upgrade_uses_small_topup_not_full_bundle(self):
        # The configured upgrade_from top-up wins over both the full instant
        # bundle AND the raw delta.
        r = self.svc.upgrade_rewards("silver", "platinum")
        self.assertEqual(r["coins"], 451000)
        self.assertEqual(r["gems"], 251)
        self.assertEqual(r["quest_points"], 251)
        self.assertEqual([p.lower() for p in r["packs"]], ["legend pack"])

    def test_upgrade_credits_the_topup(self):
        u = self._silver_user()
        before_coins = u.total_coins
        res = self.svc.upgrade(None, u, "platinum")
        self.assertEqual(u.subscription_tier, "platinum")
        self.assertEqual(res["from_tier"], "silver")
        # Top-up, not the full 1,000,000 Platinum bundle nor the 951k delta.
        self.assertEqual(u.total_coins, before_coins + 451000)
        self.assertEqual(u.total_gems, 10 + 251)
        self.assertEqual(u.quest_points, 5 + 251)

    def test_upgrade_preserves_remaining_time(self):
        u = self._silver_user(days_left=20)
        expiry_before = u.subscription_expires_at
        self.svc.upgrade(None, u, "platinum")
        # Clock is untouched — they keep the ~20 days they already paid for.
        self.assertEqual(u.subscription_expires_at, expiry_before)

    def test_upgrade_from_none_falls_back_to_activation(self):
        u = SimpleNamespace(
            id=2, total_coins=0, total_gems=0, quest_points=0,
            subscription_tier="none", subscription_expires_at=None,
            subscription_activated_at=None)
        res = self.svc.upgrade(None, u, "platinum")
        self.assertEqual(u.subscription_tier, "platinum")
        # Fresh activation → full Platinum instant bundle.
        self.assertEqual(u.total_coins, 1000000)
        self.assertIsNotNone(u.subscription_expires_at)
        self.assertIsNone(res.get("from_tier"))

    def test_cannot_upgrade_to_same_or_lower_tier(self):
        plat = SimpleNamespace(
            id=3, total_coins=0, total_gems=0, quest_points=0,
            subscription_tier="platinum",
            subscription_expires_at=datetime.utcnow() + timedelta(days=5),
            subscription_activated_at=datetime.utcnow())
        with self.assertRaises(ValueError):
            self.svc.upgrade(None, plat, "silver")
        with self.assertRaises(ValueError):
            self.svc.upgrade(None, plat, "platinum")

    def test_can_upgrade_helper(self):
        self.assertTrue(self.svc.can_upgrade(self._silver_user(), "platinum"))
        self.assertFalse(self.svc.can_upgrade(self._silver_user(), "silver"))
        self.assertTrue(self.svc.tier_rank("platinum") > self.svc.tier_rank("silver"))


if __name__ == "__main__":
    unittest.main()

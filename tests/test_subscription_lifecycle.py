"""Tests for subscription lifecycle: no auto-renewal + fresh-start re-subscribe.

A subscription never auto-renews — an expired tier simply lapses and reads as
``none``. When a lapsed member subscribes again they must be treated exactly
like a first-time member: a fresh paid window, the instant rewards re-granted,
AND their subscriber recurring-drop cooldowns (Mystery Box / weekly card / coin
chests) reset so the first drop is available immediately rather than inheriting
a stale timer from the previous subscription.

Also pins the tiered Mystery Box cadence (Silver 8 days, Platinum 4 days).
"""

import sys
import types
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace


def _load_service():
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *a, **k: None
    sys.modules.setdefault("dotenv", dotenv)
    sys.modules.pop("services.subscription_service", None)
    activity = types.ModuleType("services.activity_service")
    activity.log_activity = lambda *a, **k: None
    sys.modules["services.activity_service"] = activity
    pack = types.ModuleType("services.pack_service")
    pack.list_packs = lambda *a, **k: []
    pack.grant_pack = lambda *a, **k: None
    sys.modules["services.pack_service"] = pack
    # sqlalchemy isn't installed in the test env, so the real `models` module
    # can't import. Stub it with a bare UserStats the helper can reference.
    models_mod = types.ModuleType("models")

    class UserStats:
        user_id = None

    models_mod.UserStats = UserStats
    sys.modules["models"] = models_mod
    from services import subscription_service
    return subscription_service


class _FakeStats:
    """Stand-in for a UserStats row carrying the subscriber cooldown fields."""

    def __init__(self):
        self.last_mysterybox = datetime.utcnow() - timedelta(days=1)
        self.last_weekly = datetime.utcnow() - timedelta(days=1)
        self.last_coinchest = datetime.utcnow() - timedelta(days=1)
        self.coinchest_used = 3


class _FakeQuery:
    def __init__(self, stats):
        self._stats = stats

    def filter(self, *a, **k):
        return self

    def first(self):
        return self._stats


class _FakeSession:
    """Minimal session whose .query(UserStats) returns a single fake stats row."""

    def __init__(self, stats):
        self._stats = stats

    def query(self, *a, **k):
        return _FakeQuery(self._stats)


class MysteryBoxCadenceTests(unittest.TestCase):
    def setUp(self):
        self.svc = _load_service()

    def _user(self, tier):
        return SimpleNamespace(
            id=1, subscription_tier=tier,
            subscription_expires_at=datetime.utcnow() + timedelta(days=5))

    def test_silver_cadence_is_8_days(self):
        self.assertEqual(
            self.svc.mysterybox_cooldown_seconds(self._user("silver")),
            8 * 86400)

    def test_platinum_cadence_is_4_days(self):
        self.assertEqual(
            self.svc.mysterybox_cooldown_seconds(self._user("platinum")),
            4 * 86400)


class ReSubscribeFreshStartTests(unittest.TestCase):
    def setUp(self):
        self.svc = _load_service()

    def _lapsed_silver(self):
        # Tier still set on the row but expiry is in the past → reads as none.
        return SimpleNamespace(
            id=1, total_coins=0, total_gems=0, quest_points=0,
            subscription_tier="silver",
            subscription_expires_at=datetime.utcnow() - timedelta(days=2),
            subscription_activated_at=datetime.utcnow() - timedelta(days=40))

    def test_expired_tier_reads_as_none_no_auto_renew(self):
        u = self._lapsed_silver()
        self.assertEqual(self.svc.get_tier(u), "none")
        self.assertFalse(self.svc.is_subscribed(u))

    def test_resubscribe_after_lapse_grants_first_time_rewards(self):
        u = self._lapsed_silver()
        stats = _FakeStats()
        res = self.svc.activate(_FakeSession(stats), u, "silver")
        # Fresh paid window + full first-time instant bundle re-granted.
        self.assertGreater(u.subscription_expires_at, datetime.utcnow())
        self.assertEqual(u.total_coins, 49000)
        self.assertIsNotNone(res.get("instant_granted"))

    def test_resubscribe_after_lapse_resets_subscriber_cooldowns(self):
        u = self._lapsed_silver()
        stats = _FakeStats()
        self.svc.activate(_FakeSession(stats), u, "silver")
        # First-time treatment: stale Mystery Box / weekly / chest timers wiped.
        self.assertIsNone(stats.last_mysterybox)
        self.assertIsNone(stats.last_weekly)
        self.assertIsNone(stats.last_coinchest)
        self.assertEqual(stats.coinchest_used, 0)

    def test_same_tier_renewal_preserves_cooldowns(self):
        # Still-active Silver, admin re-activates the same tier: this is a
        # renewal (stack time, no re-grant) and must NOT wipe running cooldowns.
        u = SimpleNamespace(
            id=1, total_coins=100, total_gems=0, quest_points=0,
            subscription_tier="silver",
            subscription_expires_at=datetime.utcnow() + timedelta(days=10),
            subscription_activated_at=datetime.utcnow() - timedelta(days=20))
        stats = _FakeStats()
        before = stats.last_mysterybox
        res = self.svc.activate(_FakeSession(stats), u, "silver")
        self.assertIsNone(res.get("instant_granted"))   # no re-grant
        self.assertEqual(u.total_coins, 100)            # currencies untouched
        self.assertEqual(stats.last_mysterybox, before)  # cooldown preserved

    def test_deactivate_ends_subscription_and_clears_cooldowns(self):
        u = SimpleNamespace(
            id=1, subscription_tier="platinum",
            subscription_expires_at=datetime.utcnow() + timedelta(days=5))
        stats = _FakeStats()
        self.svc.deactivate(_FakeSession(stats), u)
        self.assertEqual(u.subscription_tier, "none")
        self.assertIsNone(u.subscription_expires_at)
        self.assertIsNone(stats.last_mysterybox)
        self.assertEqual(stats.coinchest_used, 0)


if __name__ == "__main__":
    unittest.main()

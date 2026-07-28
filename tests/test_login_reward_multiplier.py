"""Tests for the Mini App daily-login reward multiplier.

💎 Diamond members get "2X Daily Login Reward" — the ladder payout (coins AND
gems) is doubled on claim, every other tier is unchanged, and a lapsed
subscription drops straight back to 1×.
"""

import sys
import types
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace


# Real modules displaced by the stubs below, so tearDownModule can put them
# back. unittest imports every test module before running any of them, so a
# stub left in sys.modules would otherwise be inherited by whatever runs next.
_DISPLACED = {}


def _stub(name, module):
    """Install a stub module, remembering whatever it displaced."""
    if name not in _DISPLACED:
        _DISPLACED[name] = sys.modules.get(name)
    sys.modules[name] = module


def tearDownModule():
    """Undo every stub this module installed."""
    import services
    for name, original in _DISPLACED.items():
        if original is None:
            sys.modules.pop(name, None)
            # Drop the attribute the import machinery cached on the package
            # too, or `from services import x` keeps handing out the stub.
            attr = name.split(".", 1)[1] if name.startswith("services.") else None
            if attr and hasattr(services, attr):
                delattr(services, attr)
        else:
            sys.modules[name] = original
    _DISPLACED.clear()
    for name in ("services.subscription_service", "services.login_streak_service"):
        sys.modules.pop(name, None)


def _load_service():
    if "dotenv" not in sys.modules:
        dotenv = types.ModuleType("dotenv")
        dotenv.load_dotenv = lambda *a, **k: None
        _stub("dotenv", dotenv)
    # sqlalchemy isn't installed in the test env, so the real `models` module
    # can't import. login_streak_service only needs the name to exist.
    models_mod = types.ModuleType("models")

    class UserStats:
        user_id = None

    class Pack:
        pass

    models_mod.UserStats = UserStats
    models_mod.Pack = Pack
    _stub("models", models_mod)
    activity = types.ModuleType("services.activity_service")
    activity.log_activity = lambda *a, **k: None
    _stub("services.activity_service", activity)
    pack = types.ModuleType("services.pack_service")
    pack.list_packs = lambda *a, **k: []
    pack.grant_pack = lambda *a, **k: None
    _stub("services.pack_service", pack)
    sys.modules.pop("services.subscription_service", None)
    sys.modules.pop("services.login_streak_service", None)
    from services import login_streak_service
    return login_streak_service


def _member(tier, expires_in_days=20):
    return SimpleNamespace(
        id=1, total_coins=0, total_gems=0,
        subscription_tier=tier,
        subscription_expires_at=(None if tier == "none" else
                                 datetime.utcnow()
                                 + timedelta(days=expires_in_days)))


class _Stats:
    """Stand-in for the UserStats row the ladder reads/writes."""

    def __init__(self, streak=1):
        self.login_streak = streak
        self.login_best_streak = streak
        self.last_login_date = datetime.utcnow().strftime("%Y-%m-%d")
        self.login_reward_claimed_date = None


class LoginRewardMultiplierTests(unittest.TestCase):
    def setUp(self):
        self.svc = _load_service()

    def _claim(self, tier, streak):
        user, stats = _member(tier), _Stats(streak)
        result = self.svc.claim_login_reward(None, user, stats)
        self.assertTrue(result["ok"], result)
        return user, result["reward"]

    def test_day3_reward_is_doubled_for_diamond(self):
        # Day 3 of the ladder pays 500 coins + 1 gem at 1×.
        user, reward = self._claim("diamond", streak=3)
        self.assertEqual(reward["coins"], 1000)
        self.assertEqual(reward["gems"], 2)
        self.assertEqual(reward["tier_multiplier"], 2)
        self.assertEqual(user.total_coins, 1000)
        self.assertEqual(user.total_gems, 2)

    def test_day7_jackpot_is_doubled_for_diamond(self):
        _, reward = self._claim("diamond", streak=7)
        self.assertEqual(reward["coins"], 6000)   # 3,000 × 2
        self.assertEqual(reward["gems"], 4)       # 2 × 2

    def test_other_tiers_and_free_users_get_the_base_reward(self):
        for tier in ("none", "bronze", "silver", "platinum"):
            user, reward = self._claim(tier, streak=3)
            self.assertEqual(reward["coins"], 500, tier)
            self.assertEqual(reward["gems"], 1, tier)
            self.assertEqual(reward["tier_multiplier"], 1, tier)
            self.assertEqual(user.total_coins, 500, tier)

    def test_lapsed_diamond_falls_back_to_single_reward(self):
        user, stats = _member("diamond", expires_in_days=-1), _Stats(3)
        reward = self.svc.claim_login_reward(None, user, stats)["reward"]
        self.assertEqual(reward["coins"], 500)
        self.assertEqual(reward["tier_multiplier"], 1)

    def test_status_reports_the_multiplier_for_the_miniapp_banner(self):
        status = self.svc.get_login_status(None, _Stats(3), user=_member("diamond"))
        self.assertEqual(status["tier_multiplier"], 2)
        status = self.svc.get_login_status(None, _Stats(3), user=_member("silver"))
        self.assertEqual(status["tier_multiplier"], 1)
        # Callers that don't pass a user still get a valid payload.
        self.assertEqual(
            self.svc.get_login_status(None, _Stats(3))["tier_multiplier"], 1)

    def test_double_claim_same_day_still_refused(self):
        user, stats = _member("diamond"), _Stats(3)
        self.assertTrue(self.svc.claim_login_reward(None, user, stats)["ok"])
        second = self.svc.claim_login_reward(None, user, stats)
        self.assertFalse(second["ok"])
        self.assertEqual(second["error"], "already_claimed")
        self.assertEqual(user.total_coins, 1000)  # not credited twice


if __name__ == "__main__":
    unittest.main()

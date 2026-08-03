"""Which slot a Lucky Card Pick spends, and what that costs the player.

The clients send no ``ad_token`` in two completely different situations:

  * the free pick of the cycle is available — nothing is owed, and a saved ad
    must NOT be spent on it; and
  * the free pick is gone and the player has an ad banked from earlier — the
    round is paid for out of the ledger, with no second ad shown.

Both arrive at ``/api/webapp/spin`` as an identical token-less request, so the
rule that separates them lives entirely in the endpoint: ad evidence is only
touched once ``free_available`` is false. Get it wrong in one direction and a
watched ad is destroyed to pay for a pick that was free; get it wrong in the
other and the saved ad is never redeemed at all.

These run against the real Flask endpoint rather than the services underneath,
because the ordering is the thing under test and it only exists there.
"""

import os
import sys
import tempfile
import unittest

_PREV_ENV = {}
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "admin",
                 "services.ad_service", "services.quota_service",
                 "services.config_service")

TG_ID = 8_800_100_777


def setUpModule():
    global _PREV_ENV, _SAVED_MODULES, _TMP, _ENGINE

    _PREV_ENV = {k: os.environ.get(k)
                 for k in ("DATABASE_URL", "WEBAPP_DEV_MODE", "BOT_TOKEN",
                           "ADMIN_SECRET")}
    _SAVED_MODULES = {name: sys.modules.get(name) for name in _MODULE_NAMES}
    for name in _MODULE_NAMES:
        sys.modules.pop(name, None)

    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"
    # DEV_<telegram id> auth, which _webapp_auth honours only under this flag.
    os.environ["WEBAPP_DEV_MODE"] = "1"
    os.environ.setdefault("BOT_TOKEN", "test-token")
    os.environ.setdefault("ADMIN_SECRET", "test-secret")

    from database import Base, engine
    import models  # noqa: F401

    _ENGINE = engine
    Base.metadata.create_all(bind=engine)


def tearDownModule():
    try:
        _ENGINE.dispose()
    except Exception:
        pass
    for key, value in _PREV_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    for name, module in _SAVED_MODULES.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module
    try:
        os.unlink(_TMP.name)
    except Exception:
        pass


class SpinSlotTests(unittest.TestCase):
    """A token-less pick: free first, banked ad only once free is gone."""

    def setUp(self):
        import admin
        from database import SessionLocal
        from models import AdReward, User, UserStats
        from services import ad_service

        self.admin = admin
        self.ad_service = ad_service
        # post_miniapp_activity broadcasts to Telegram; nothing to test here
        # and no bot to receive it.
        self._real_post = admin.post_miniapp_activity
        admin.post_miniapp_activity = lambda *a, **k: None

        admin.app.config["TESTING"] = True
        self.client = admin.app.test_client()

        self.db = SessionLocal()
        # A clean slate per test: same telegram id, no carried-over state.
        self.db.query(AdReward).filter(AdReward.telegram_id == TG_ID).delete()
        user = self.db.query(User).filter(User.telegram_id == TG_ID).first()
        if user:
            self.db.query(UserStats).filter(
                UserStats.user_id == user.id).delete()
            self.db.delete(user)
            self.db.commit()
        user = User(telegram_id=TG_ID, first_name="Tester",
                    total_coins=0, total_gems=0, roster_count=0)
        self.db.add(user)
        self.db.commit()
        self.user_id = user.id

    def tearDown(self):
        self.admin.post_miniapp_activity = self._real_post
        self.db.close()

    def _spin(self):
        """POST a token-less pick, exactly as a client with a saved ad does."""
        res = self.client.post(
            "/api/webapp/spin",
            headers={"Authorization": f"tma DEV_{TG_ID}",
                     "Content-Type": "application/json"},
            json={})
        return res.status_code, res.get_json()

    def _credits(self):
        return self.ad_service.count_credits(self.db, TG_ID)

    def test_a_free_spin_does_not_consume_a_credit(self):
        """The free pick costs nothing, so it must not spend a watched ad.

        Spending one here is the exact failure the credit ledger exists to
        prevent: the player watched an ad, the app gave them a pick they were
        owed anyway, and the ad is gone.
        """
        self.ad_service.bank_credit(self.db, TG_ID, "test")
        self.assertEqual(self._credits(), 1)

        status, body = self._spin()
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["slot_type"], "free")
        self.assertIsNone(body["verified_via"])
        # Still banked, and reported back so the client can say so.
        self.assertEqual(self._credits(), 1)
        self.assertEqual(body["ads_saved"], 1)

    def test_the_next_spin_redeems_the_saved_ad(self):
        """…and once the free pick is gone, the same request spends it."""
        self.ad_service.bank_credit(self.db, TG_ID, "test")

        status, _ = self._spin()                 # burns the free slot
        self.assertEqual(status, 200)

        status, body = self._spin()
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["slot_type"], "ad")
        self.assertEqual(body["verified_via"], "ad_credit")
        self.assertEqual(self._credits(), 0)
        self.assertEqual(body["ads_saved"], 0)

    def test_a_third_spin_with_nothing_banked_asks_for_an_ad(self):
        """One ad, one reward: the credit does not keep paying out."""
        self.ad_service.bank_credit(self.db, TG_ID, "test")
        self._spin()                             # free
        self._spin()                             # the saved ad

        status, body = self._spin()
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"], "ad_required")
        self.assertEqual(self._credits(), 0)


if __name__ == "__main__":
    unittest.main()

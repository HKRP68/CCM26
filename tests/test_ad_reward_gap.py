"""The hour between rewarded ads, and the promise that one is never wasted.

Two reports sat behind this file:

  * ad-gated spins could be taken back to back, so a cycle's whole ad quota was
    drained in a couple of minutes; and
  * a spin could cost a watched ad and pay nothing — the wheel turned for a
    long time and the reward never arrived.

The rules that answer both are here:

  • an ad-gated use starts a gap (``GameConfig.ad_reward_gap_minutes``, default
    60) before the next one unlocks — and the FREE use is never subject to it;
  • a no-fill pass waits the gap out too, or "no ad available" would be the
    cheap way around it;
  • an ad that is proven but can't be spent is BANKED as a credit and redeemed
    later with no second ad, which is what makes the gap safe to enforce after
    the ad rather than only before it;
  • a spin that has been paid for always has something to land on, even with an
    empty reward table.

The quota rules take a plain stats object; the credit ledger needs a database,
so that half runs against a temporary SQLite file.
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "services.ad_service",
                 "services.quota_service", "services.config_service")


def setUpModule():
    global _PREV_DATABASE_URL, _SAVED_MODULES, _TMP, _ENGINE

    _PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
    _SAVED_MODULES = {name: sys.modules.get(name) for name in _MODULE_NAMES}
    for name in _MODULE_NAMES:
        sys.modules.pop(name, None)

    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"

    from database import Base, engine
    import models  # noqa: F401

    _ENGINE = engine
    Base.metadata.create_all(bind=engine)


def tearDownModule():
    try:
        _ENGINE.dispose()
    except Exception:
        pass
    if _PREV_DATABASE_URL is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _PREV_DATABASE_URL
    for name, module in _SAVED_MODULES.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module
    try:
        os.unlink(_TMP.name)
    except Exception:
        pass


class FakeStats:
    """Just the columns the quota system reads."""

    def __init__(self, **kw):
        self.spin_cycle_started_at = None
        self.spin_free_used = False
        self.spin_ad_count = 0
        self.spin_nofill_used = 0
        self.spin_last_ad_at = None
        self.daily_cycle_started_at = None
        self.daily_free_used = False
        self.daily_ad_count = 0
        self.daily_nofill_used = 0
        self.daily_last_ad_at = None
        for key, value in kw.items():
            setattr(self, key, value)


def _mid_cycle(**kw):
    """A stats row whose free spin is gone and whose cycle is still running."""
    return FakeStats(spin_cycle_started_at=datetime.utcnow(),
                     spin_free_used=True, **kw)


class GapTests(unittest.TestCase):
    """When the next rewarded ad unlocks."""

    def setUp(self):
        from services import quota_service
        self.quota_service = quota_service
        self.gap = quota_service.DEFAULT_AD_GAP_MINUTES * 60

    def test_the_default_gap_is_an_hour(self):
        self.assertEqual(self.quota_service.DEFAULT_AD_GAP_MINUTES, 60)

    def test_a_fresh_user_waits_for_nothing(self):
        status = self.quota_service.get_quota_status(FakeStats(), "spin")
        self.assertEqual(status["ad_ready_in"], 0)
        self.assertTrue(status["ad_available"])

    def test_an_ad_spin_locks_the_next_one_for_the_gap(self):
        stats = _mid_cycle()
        self.quota_service.consume_slot(stats, "spin", "ad")
        self.assertIsNotNone(stats.spin_last_ad_at)

        status = self.quota_service.get_quota_status(stats, "spin")
        self.assertGreater(status["ad_ready_in"], self.gap - 60)
        self.assertLessEqual(status["ad_ready_in"], self.gap)
        self.assertFalse(status["ad_available"])

        allowed, slot, reason = self.quota_service.can_use(
            stats, "spin", ad_provided=True)
        self.assertFalse(allowed)
        self.assertIsNone(slot)
        self.assertEqual(reason, "ad_cooldown")

    def test_the_gap_expires(self):
        stats = _mid_cycle(spin_ad_count=1,
                           spin_last_ad_at=datetime.utcnow()
                           - timedelta(seconds=self.gap + 5))
        status = self.quota_service.get_quota_status(stats, "spin")
        self.assertEqual(status["ad_ready_in"], 0)
        self.assertTrue(status["ad_available"])

        allowed, slot, reason = self.quota_service.can_use(
            stats, "spin", ad_provided=True)
        self.assertTrue(allowed)
        self.assertEqual(slot, "ad")
        self.assertIsNone(reason)

    def test_the_free_spin_is_never_gated_by_the_gap(self):
        """The one that would be a support ticket: a player opening the app to
        their free spin must never be told to come back in an hour."""
        stats = FakeStats(spin_cycle_started_at=datetime.utcnow(),
                          spin_last_ad_at=datetime.utcnow())
        allowed, slot, reason = self.quota_service.can_use(
            stats, "spin", ad_provided=False)
        self.assertTrue(allowed)
        self.assertEqual(slot, "free")
        self.assertIsNone(reason)

    def test_the_free_use_does_not_start_a_gap(self):
        stats = FakeStats()
        self.quota_service.consume_slot(stats, "spin", "free")
        self.assertIsNone(stats.spin_last_ad_at)
        self.assertEqual(
            self.quota_service.get_quota_status(stats, "spin")["ad_ready_in"], 0)

    def test_spin_and_daily_keep_their_own_gaps(self):
        """A spin must not lock the daily claim — they are separate rewards."""
        stats = _mid_cycle(daily_cycle_started_at=datetime.utcnow(),
                           daily_free_used=True)
        self.quota_service.consume_slot(stats, "spin", "ad")
        self.assertGreater(
            self.quota_service.get_quota_status(stats, "spin")["ad_ready_in"], 0)
        self.assertEqual(
            self.quota_service.get_quota_status(stats, "daily")["ad_ready_in"], 0)

    def test_a_future_timestamp_does_not_lock_the_feature_forever(self):
        """Clock skew or a restored backup, not a player who spun tomorrow."""
        stats = _mid_cycle(spin_last_ad_at=datetime.utcnow()
                           + timedelta(days=3))
        status = self.quota_service.get_quota_status(stats, "spin")
        self.assertLessEqual(status["ad_ready_in"], self.gap)

    def test_a_row_without_the_column_has_no_gap(self):
        """A database that predates the migration must not lock anyone out."""
        stats = _mid_cycle()
        del stats.spin_last_ad_at
        status = self.quota_service.get_quota_status(stats, "spin")
        self.assertEqual(status["ad_ready_in"], 0)
        # …and consuming a slot on it still works.
        self.quota_service.consume_slot(stats, "spin", "ad")
        self.assertEqual(stats.spin_ad_count, 1)

    def test_an_exhausted_cycle_still_reports_exhausted(self):
        """The gap must not mask the more useful reason. 'Come back in 40m' when
        the real answer is 'tomorrow' is worse than no message."""
        stats = _mid_cycle(spin_ad_count=99, spin_last_ad_at=datetime.utcnow())
        allowed, _slot, reason = self.quota_service.can_use(
            stats, "spin", ad_provided=True)
        self.assertFalse(allowed)
        self.assertEqual(reason, "cycle_exhausted")


class NoFillPassGapTests(unittest.TestCase):
    """A pass stands in for an ad, so it waits the gap out like one."""

    def setUp(self):
        from services import quota_service
        self.quota_service = quota_service

    def test_no_pass_is_granted_inside_the_gap(self):
        stats = _mid_cycle(spin_ad_count=1,
                           spin_last_ad_at=datetime.utcnow())
        granted, status = self.quota_service.claim_nofill_pass(stats, "spin")
        self.assertFalse(granted)
        self.assertGreater(status["ad_ready_in"], 0)
        # The grace is untouched — a refused request must not cost anything.
        self.assertEqual(stats.spin_nofill_used, 0)

    def test_a_pass_is_granted_once_the_gap_is_up(self):
        gap = self.quota_service.DEFAULT_AD_GAP_MINUTES * 60
        stats = _mid_cycle(spin_ad_count=1,
                           spin_last_ad_at=datetime.utcnow()
                           - timedelta(seconds=gap + 5))
        granted, _status = self.quota_service.claim_nofill_pass(stats, "spin")
        self.assertTrue(granted)
        self.assertEqual(stats.spin_nofill_used, 1)

    def test_a_pass_starts_the_next_gap(self):
        """Otherwise a pass would pay for a spin and leave the next ad unlocked,
        which is a faster loop than watching ads."""
        stats = _mid_cycle()
        granted, _status = self.quota_service.claim_nofill_pass(stats, "spin")
        self.assertTrue(granted)
        # The pass buys an ad slot, and spending that slot stamps the gap
        # exactly as a watched ad does.
        self.quota_service.consume_slot(stats, "spin", "ad")
        self.assertGreater(
            self.quota_service.get_quota_status(stats, "spin")["ad_ready_in"], 0)
        # …so a second pass, straight after, is refused.
        granted_again, _s = self.quota_service.claim_nofill_pass(stats, "spin")
        self.assertFalse(granted_again)


class AdCreditTests(unittest.TestCase):
    """A watched ad that couldn't be spent is kept, not lost."""

    def setUp(self):
        from database import SessionLocal
        from services import ad_service
        self.ad_service = ad_service
        self.db = SessionLocal()
        self.tg_id = 7_700_100_001 + self._counter()

    _n = [0]

    @classmethod
    def _counter(cls):
        cls._n[0] += 1
        return cls._n[0]

    def tearDown(self):
        self.db.close()

    def test_a_banked_ad_can_be_redeemed_once(self):
        self.ad_service.bank_credit(self.db, self.tg_id, "spin blocked: ad_cooldown")
        self.assertEqual(self.ad_service.count_credits(self.db, self.tg_id), 1)

        self.assertTrue(self.ad_service.claim_credit(self.db, self.tg_id))
        self.assertEqual(self.ad_service.count_credits(self.db, self.tg_id), 0)
        # …and only once. A credit that could be spent twice would be a free
        # reward generator.
        self.assertFalse(self.ad_service.claim_credit(self.db, self.tg_id))

    def test_credits_are_per_user(self):
        self.ad_service.bank_credit(self.db, self.tg_id, "test")
        self.assertFalse(self.ad_service.claim_credit(self.db, self.tg_id + 1))
        self.assertEqual(self.ad_service.count_credits(self.db, self.tg_id), 1)

    def test_a_credit_is_not_a_postback(self):
        """claim_postback's five-minute window is about a network callback. A
        credit shares the table but is claimed only by claim_credit, or a spin
        would spend a saved ad while the player is watching a fresh one."""
        self.ad_service.bank_credit(self.db, self.tg_id, "test")
        self.assertFalse(self.ad_service.claim_postback(self.db, self.tg_id))
        self.assertEqual(self.ad_service.count_credits(self.db, self.tg_id), 1)

    def test_a_postback_is_not_a_credit(self):
        self.ad_service.record_postback(self.db, self.tg_id)
        self.assertEqual(self.ad_service.count_credits(self.db, self.tg_id), 0)
        self.assertFalse(self.ad_service.claim_credit(self.db, self.tg_id))
        self.assertTrue(self.ad_service.claim_postback(self.db, self.tg_id))

    def test_a_credit_outlives_the_postback_window(self):
        """The gap is an hour and a cycle is a day — a credit that expired in
        five minutes would be worth nothing in either case."""
        from models import AdReward
        self.ad_service.bank_credit(self.db, self.tg_id, "test")
        row = (self.db.query(AdReward)
               .filter(AdReward.telegram_id == self.tg_id).one())
        row.received_at = datetime.utcnow() - timedelta(hours=25)
        self.db.commit()

        self.assertEqual(self.ad_service.count_credits(self.db, self.tg_id), 1)
        self.assertTrue(self.ad_service.claim_credit(self.db, self.tg_id))

    def test_the_oldest_credit_is_redeemed_first(self):
        """Newest-first would let the oldest credit sit until it expired at day
        7 — a watched ad lost, which is the thing the ledger prevents."""
        from models import AdReward
        for _ in range(3):
            self.ad_service.bank_credit(self.db, self.tg_id, "test")
        rows = (self.db.query(AdReward)
                .filter(AdReward.telegram_id == self.tg_id)
                .order_by(AdReward.id).all())
        # Age them apart; banking three in the same tick gives identical stamps.
        for age_days, row in enumerate(reversed(rows)):
            row.received_at = datetime.utcnow() - timedelta(days=age_days)
        self.db.commit()
        oldest_id = min(rows, key=lambda r: r.received_at).id

        self.assertTrue(self.ad_service.claim_credit(self.db, self.tg_id))
        self.db.expire_all()
        claimed = [r.id for r in rows if r.consumed_at is not None]
        self.assertEqual(claimed, [oldest_id])

    def test_a_stale_credit_expires_eventually(self):
        from models import AdReward
        self.ad_service.bank_credit(self.db, self.tg_id, "test")
        row = (self.db.query(AdReward)
               .filter(AdReward.telegram_id == self.tg_id).one())
        row.received_at = (datetime.utcnow()
                           - timedelta(seconds=self.ad_service.CREDIT_TTL_SECONDS + 60))
        self.db.commit()
        self.assertEqual(self.ad_service.count_credits(self.db, self.tg_id), 0)
        self.assertFalse(self.ad_service.claim_credit(self.db, self.tg_id))


class DurableReceiptTests(unittest.TestCase):
    """A watched ad is a database row, not an entry in process memory.

    This is the class of failure the credit ledger alone did not cover: the
    proof of the ad used to live only in ``_CLIENT_TOKENS``, a dict with a
    two-minute TTL inside one process. A deploy, a phone that took too long to
    come back, a refused reward or a lost response each destroyed a real
    watched ad. The token now only addresses a row, so all four survive.
    """

    def setUp(self):
        from database import SessionLocal
        from services import ad_service
        self.ad_service = ad_service
        self.db = SessionLocal()
        AdCreditTests._n[0] += 1
        self.tg_id = 7_800_200_001 + AdCreditTests._n[0]

    def tearDown(self):
        self.db.close()

    def test_a_watched_ad_is_written_down_before_the_token_is_handed_out(self):
        token = self.ad_service.issue_client_token(self.tg_id, session=self.db)
        self.assertTrue(token.startswith("CT-"))
        self.assertEqual(self.ad_service.count_credits(self.db, self.tg_id), 1)

    def test_a_lost_token_does_not_lose_the_ad(self):
        """The restart / expiry / dropped-response case: the client never comes
        back with the token, and the ad still pays on the next spin."""
        self.ad_service.issue_client_token(self.tg_id, session=self.db)
        self.ad_service._CLIENT_TOKENS.clear()      # a restart, in one line
        self.assertTrue(self.ad_service.claim_credit(self.db, self.tg_id))

    def test_spending_the_token_spends_the_row(self):
        token = self.ad_service.issue_client_token(self.tg_id, session=self.db)
        self.assertTrue(
            self.ad_service.consume_client_token(token, self.tg_id,
                                                 session=self.db))
        # The row went with it — the ad cannot also be redeemed as a saved one.
        self.assertEqual(self.ad_service.count_credits(self.db, self.tg_id), 0)
        self.assertFalse(self.ad_service.claim_credit(self.db, self.tg_id))

    def test_a_token_whose_row_was_already_redeemed_is_refused(self):
        """One ad, one reward — even though two paths can reach it."""
        token = self.ad_service.issue_client_token(self.tg_id, session=self.db)
        self.assertTrue(self.ad_service.claim_credit(self.db, self.tg_id))
        self.assertFalse(
            self.ad_service.consume_client_token(token, self.tg_id,
                                                 session=self.db))

    def test_preserving_an_unneeded_ad_leaves_it_claimable(self):
        token = self.ad_service.issue_client_token(self.tg_id, session=self.db)
        self.assertTrue(
            self.ad_service.preserve_client_ad(self.db, token, self.tg_id))
        self.assertEqual(self.ad_service.count_credits(self.db, self.tg_id), 1)

    def test_a_memory_only_token_is_converted_when_preserved(self):
        """Tokens minted without a session (an older build still in flight)
        have no row yet, so preserving one has to create it."""
        token = self.ad_service.issue_client_token(self.tg_id)
        self.assertEqual(self.ad_service.count_credits(self.db, self.tg_id), 0)
        self.assertTrue(
            self.ad_service.preserve_client_ad(self.db, token, self.tg_id))
        self.assertEqual(self.ad_service.count_credits(self.db, self.tg_id), 1)

    def test_a_postback_for_an_ad_already_registered_pays_nothing_extra(self):
        """Adsgram fires both halves for ONE ad: the SDK resolves in the
        browser and the reward URL is called server-side. Banking both would
        pay twice per ad."""
        self.ad_service.issue_client_token(self.tg_id, session=self.db)
        self.ad_service.record_postback(self.db, self.tg_id)
        self.assertFalse(self.ad_service.claim_postback(self.db, self.tg_id))
        # Exactly one spendable receipt for the one ad.
        self.assertEqual(self.ad_service.count_credits(self.db, self.tg_id), 1)

    def test_a_postback_that_arrives_first_is_the_receipt(self):
        """The other order: no second row, and the token addresses the
        postback that is already there."""
        self.ad_service.record_postback(self.db, self.tg_id)
        token = self.ad_service.issue_client_token(self.tg_id, session=self.db)
        self.assertEqual(self.ad_service.count_credits(self.db, self.tg_id), 0)
        self.assertTrue(
            self.ad_service.consume_client_token(token, self.tg_id,
                                                 session=self.db))
        # …and that one postback is now spent, not still waiting to be claimed.
        self.assertFalse(self.ad_service.claim_postback(self.db, self.tg_id))

    def test_a_client_calling_in_a_loop_cannot_hoard_saved_ads(self):
        """/ad-completed is reachable by any authenticated client, and a
        durable row is worth more than the expiring token it replaced."""
        from models import AdReward
        for _ in range(25):
            self.ad_service.issue_client_token(self.tg_id, session=self.db)
            # Age each receipt past the same-ad window, or the loop only ever
            # re-registers the first one and proves nothing about the cap.
            row = (self.db.query(AdReward)
                   .filter(AdReward.telegram_id == self.tg_id,
                           AdReward.consumed_at.is_(None))
                   .order_by(AdReward.received_at.desc()).first())
            if row:
                row.received_at -= timedelta(
                    seconds=self.ad_service.MIN_CLIENT_AD_INTERVAL_SECONDS + 1)
                self.db.commit()
        self.assertLessEqual(
            self.ad_service.count_credits(self.db, self.tg_id),
            self.ad_service.MAX_OUTSTANDING_CLIENT_ADS)

    def test_registering_the_same_ad_twice_banks_it_once(self):
        """A retried /ad-completed is the same ad — nothing finishes a rewarded
        ad inside the repeat window."""
        first = self.ad_service.issue_client_token(self.tg_id, session=self.db)
        second = self.ad_service.issue_client_token(self.tg_id, session=self.db)
        self.assertEqual(self.ad_service.count_credits(self.db, self.tg_id), 1)
        # Both tokens address the one row, so only one of them can spend it.
        self.assertTrue(
            self.ad_service.consume_client_token(first, self.tg_id,
                                                 session=self.db))
        self.assertFalse(
            self.ad_service.consume_client_token(second, self.tg_id,
                                                 session=self.db))

    def test_a_leftover_saved_ad_does_not_void_a_new_postback(self):
        """The reconciliation has to name the ad it is reconciling.

        Matching any banked credit meant this: an ad banked back from an
        hour-gapped spin sat in the ledger, and the postback for a *later*,
        genuinely new ad was written off as its duplicate — destroying the
        second ad, which is the exact loss this whole change exists to stop.
        """
        self.ad_service.bank_credit(self.db, self.tg_id,
                                    "spin blocked: ad_cooldown")
        self.ad_service.record_postback(self.db, self.tg_id)
        # The postback is for a different ad, so it is still claimable…
        self.assertTrue(self.ad_service.claim_postback(self.db, self.tg_id))
        # …and the banked one is untouched.
        self.assertEqual(self.ad_service.count_credits(self.db, self.tg_id), 1)

    def test_two_ads_in_a_row_get_one_receipt_each(self):
        """A receipt already answered by a postback can't absorb the next one."""
        from models import AdReward
        self.ad_service.issue_client_token(self.tg_id, session=self.db)
        self.ad_service.record_postback(self.db, self.tg_id)      # ad 1
        # Age everything past the same-ad repeat window so the second ad is
        # registered as its own receipt.
        for row in (self.db.query(AdReward)
                    .filter(AdReward.telegram_id == self.tg_id).all()):
            row.received_at -= timedelta(
                seconds=self.ad_service.MIN_CLIENT_AD_INTERVAL_SECONDS + 1)
        self.db.commit()
        self.ad_service.issue_client_token(self.tg_id, session=self.db)
        self.ad_service.record_postback(self.db, self.tg_id)      # ad 2

        # Two ads watched, two receipts — no more, no less.
        self.assertEqual(self.ad_service.count_credits(self.db, self.tg_id), 2)
        self.assertFalse(self.ad_service.claim_postback(self.db, self.tg_id))

    def test_a_postback_with_no_client_ad_still_pays(self):
        """The server-only path has to keep working — a WebView that never
        reached /ad-completed is exactly when the postback matters most."""
        self.ad_service.record_postback(self.db, self.tg_id)
        self.assertTrue(self.ad_service.claim_postback(self.db, self.tg_id))


class GuaranteedRewardTests(unittest.TestCase):
    """A spin that has been paid for always lands on something."""

    def test_an_empty_reward_table_still_pays(self):
        from database import SessionLocal
        from services.gspin_reward_service import guaranteed_reward, pick_reward
        db = SessionLocal()
        try:
            # Nothing configured — the old code path returned None here and the
            # endpoint answered 500 to a user who had just watched an ad.
            self.assertIsNone(pick_reward(db))
            reward = guaranteed_reward(db)
            self.assertIsNotNone(reward)
            self.assertEqual(reward.reward_type, "coins")
            self.assertGreater(reward.amount_max, 0)
            self.assertGreaterEqual(reward.amount_max, reward.amount_min)
        finally:
            db.close()

    def test_a_zero_coin_band_never_becomes_a_worthless_reward(self):
        """(0, 0) is truthy, and apply_reward only rolls when the top of the
        band is positive — so a configured zero band would pay nothing to a
        player who had just watched an ad."""
        import services.gspin_reward_service as svc
        import config as cfg
        saved = cfg.GSPIN_OUTCOMES
        try:
            cfg.GSPIN_OUTCOMES = [(1.0, "red", "coins", (0, 0))]
            reward = svc.fallback_reward()
        finally:
            cfg.GSPIN_OUTCOMES = saved
        self.assertGreater(reward.amount_max, 0)
        self.assertGreaterEqual(reward.amount_min, 1)

    def test_the_fallback_can_be_applied_like_a_real_row(self):
        """It is handed to apply_reward, so it has to answer every attribute
        that reads."""
        from services.gspin_reward_service import fallback_reward
        reward = fallback_reward()
        for attribute in ("reward_type", "label", "emoji", "color",
                          "amount_min", "amount_max", "pack_id",
                          "player_rating_min", "player_rating_max"):
            self.assertTrue(hasattr(reward, attribute), attribute)


class ReplayTests(unittest.TestCase):
    """A retried spin returns the reward it already granted."""

    def test_a_remembered_result_is_replayed_then_expires(self):
        from utils.idempotency import recall, remember
        key = "spin_4242_abc"
        self.assertIsNone(recall(key))
        remember(key, {"ok": True, "reward": {"type": "coins", "amount": 100}})
        self.assertEqual(recall(key)["reward"]["amount"], 100)
        # Same key, still one reward — the retry replays, it does not re-grant.
        self.assertEqual(recall(key)["reward"]["amount"], 100)
        self.assertIsNone(recall(key, ttl=-1))

    def test_results_are_keyed_per_request(self):
        from utils.idempotency import recall, remember
        remember("spin_1_a", {"ok": True})
        self.assertIsNone(recall("spin_1_b"))
        self.assertIsNone(recall("spin_2_a"))


if __name__ == "__main__":
    unittest.main()

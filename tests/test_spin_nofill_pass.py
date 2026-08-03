"""No-fill passes — what happens when the ad network has no ad to give.

Adsgram answering "no banner", or its SDK never loading inside a Telegram
WebView on a phone network, is routine and is not something the player can fix.
The old flow left them tapping a wheel that spun for ninety seconds and then
said "no ad available", so the feature they were promised simply didn't exist
for them that cycle.

A no-fill pass unlocks one ad-gated slot without an ad. The rules that make it
safe to hand out are all here:

  • it is never extra quota — it spends an ad slot exactly as a watched ad does;
  • it has its own, much smaller per-cycle cap, so a client that always claims
    no-fill gains only that cap and still needs real ads for the rest;
  • the token is single-use, bound to one user and to the one quota it was
    debited from.

No Flask and no database — the quota rules take a plain stats object and the
token store is in-process by design.
"""

import time
import unittest
from datetime import datetime, timedelta

from services import adsgram_service, quota_service


class FakeStats:
    """Just the columns the quota system reads."""

    def __init__(self, **kw):
        self.spin_cycle_started_at = None
        self.spin_free_used = False
        self.spin_ad_count = 0
        self.spin_nofill_used = 0
        self.daily_cycle_started_at = None
        self.daily_free_used = False
        self.daily_ad_count = 0
        self.daily_nofill_used = 0
        for key, value in kw.items():
            setattr(self, key, value)


def _mid_cycle(**kw):
    """A stats row whose free spin is gone and whose cycle is still running."""
    return FakeStats(spin_cycle_started_at=datetime.utcnow(),
                     spin_free_used=True, **kw)


class GraceAccountingTests(unittest.TestCase):
    """When a pass is handed out, and when it isn't."""

    def test_status_reports_the_grace(self):
        status = quota_service.get_quota_status(FakeStats(), "spin")
        self.assertEqual(status["nofill_total"],
                         quota_service.DEFAULT_NOFILL_GRACE)
        self.assertEqual(status["nofill_left"],
                         quota_service.DEFAULT_NOFILL_GRACE)

    def test_a_free_spin_needs_no_rescue(self):
        # The free slot never asked for an ad, so there is nothing to rescue and
        # the grace must not be spent on it.
        stats = FakeStats()
        granted, _status = quota_service.claim_nofill_pass(stats, "spin")
        self.assertFalse(granted)
        self.assertEqual(stats.spin_nofill_used, 0)

    def test_a_pass_is_granted_once_the_free_spin_is_gone(self):
        stats = _mid_cycle()
        granted, status = quota_service.claim_nofill_pass(stats, "spin")
        self.assertTrue(granted)
        self.assertEqual(stats.spin_nofill_used, 1)
        self.assertEqual(status["nofill_left"],
                         quota_service.DEFAULT_NOFILL_GRACE - 1)

    def test_the_grace_runs_out(self):
        """The cap is the whole reason this is safe to grant on client say-so."""
        stats = _mid_cycle()
        for _ in range(quota_service.DEFAULT_NOFILL_GRACE):
            self.assertTrue(quota_service.claim_nofill_pass(stats, "spin")[0])
        granted, status = quota_service.claim_nofill_pass(stats, "spin")
        self.assertFalse(granted)
        self.assertEqual(status["nofill_left"], 0)
        self.assertEqual(stats.spin_nofill_used,
                         quota_service.DEFAULT_NOFILL_GRACE)

    def test_a_pass_cannot_exceed_the_ad_quota(self):
        # A pass skips the ad, never the quota: with every ad slot spent there
        # is nothing left for a pass to unlock.
        stats = _mid_cycle(spin_ad_count=99)
        self.assertFalse(quota_service.claim_nofill_pass(stats, "spin")[0])

    def test_the_grace_resets_with_the_cycle(self):
        stats = _mid_cycle()
        for _ in range(quota_service.DEFAULT_NOFILL_GRACE):
            quota_service.claim_nofill_pass(stats, "spin")
        self.assertFalse(quota_service.claim_nofill_pass(stats, "spin")[0])

        # Roll the cycle start back past its length — the next read resets.
        stats.spin_cycle_started_at = datetime.utcnow() - timedelta(days=2)
        status = quota_service.get_quota_status(stats, "spin")
        self.assertEqual(status["nofill_left"],
                         quota_service.DEFAULT_NOFILL_GRACE)
        self.assertEqual(stats.spin_nofill_used, 0)

    def test_spin_and_daily_keep_separate_graces(self):
        stats = FakeStats(spin_cycle_started_at=datetime.utcnow(),
                          spin_free_used=True,
                          daily_cycle_started_at=datetime.utcnow(),
                          daily_free_used=True)
        quota_service.claim_nofill_pass(stats, "spin")
        self.assertEqual(stats.spin_nofill_used, 1)
        self.assertEqual(stats.daily_nofill_used, 0)

    def test_a_row_predating_the_column_still_works(self):
        """Auto-migration is best effort, so the code cannot assume the column."""
        stats = _mid_cycle()
        del stats.spin_nofill_used
        status = quota_service.get_quota_status(stats, "spin")
        self.assertEqual(status["nofill_left"],
                         quota_service.DEFAULT_NOFILL_GRACE)
        # And a reset over a missing column is a no-op rather than a crash.
        stats.spin_cycle_started_at = datetime.utcnow() - timedelta(days=2)
        quota_service.get_quota_status(stats, "spin")


class PassTokenTests(unittest.TestCase):
    """The token that carries a granted pass back to the spin endpoint."""

    TG_ID = 424242

    def test_a_pass_token_is_single_use(self):
        token = adsgram_service.issue_nofill_token(self.TG_ID, "spin")
        self.assertTrue(
            adsgram_service.consume_nofill_token(token, self.TG_ID, "spin"))
        self.assertFalse(
            adsgram_service.consume_nofill_token(token, self.TG_ID, "spin"))

    def test_a_pass_belongs_to_one_user(self):
        token = adsgram_service.issue_nofill_token(self.TG_ID, "spin")
        self.assertFalse(
            adsgram_service.consume_nofill_token(token, self.TG_ID + 1, "spin"))

    def test_a_spin_pass_cannot_be_spent_on_daily(self):
        # Otherwise the spin grace would quietly fund every other ad-gated
        # feature in the app.
        token = adsgram_service.issue_nofill_token(self.TG_ID, "spin")
        self.assertFalse(
            adsgram_service.consume_nofill_token(token, self.TG_ID, "daily"))

    def test_a_watched_ad_token_is_not_a_pass_and_vice_versa(self):
        watched = adsgram_service.issue_client_token(self.TG_ID)
        self.assertFalse(
            adsgram_service.consume_nofill_token(watched, self.TG_ID, "spin"))
        pass_token = adsgram_service.issue_nofill_token(self.TG_ID, "spin")
        self.assertFalse(
            adsgram_service.consume_client_token(pass_token, self.TG_ID))

    def test_the_two_kinds_are_told_apart_by_prefix(self):
        # The spin endpoint dispatches on the prefix, so they must not collide.
        self.assertNotEqual(adsgram_service.CLIENT_TOKEN_PREFIX,
                            adsgram_service.NOFILL_TOKEN_PREFIX)
        self.assertTrue(adsgram_service.issue_nofill_token(self.TG_ID, "spin")
                        .startswith(adsgram_service.NOFILL_TOKEN_PREFIX))
        self.assertTrue(adsgram_service.issue_client_token(self.TG_ID)
                        .startswith(adsgram_service.CLIENT_TOKEN_PREFIX))

    def test_a_rejected_attempt_does_not_destroy_a_valid_pass(self):
        """A wrong guess must not burn a pass the real owner still holds.

        The grace is debited when the token is issued, so a token destroyed by a
        failed lookup takes a spin with it and the player gets nothing for it.
        """
        token = adsgram_service.issue_nofill_token(self.TG_ID, "spin")
        self.assertFalse(
            adsgram_service.consume_nofill_token(token, self.TG_ID + 1, "spin"))
        self.assertFalse(
            adsgram_service.consume_nofill_token(token, self.TG_ID, "daily"))
        # ...and it is still there for the call that is actually entitled to it.
        self.assertTrue(
            adsgram_service.consume_nofill_token(token, self.TG_ID, "spin"))

    def test_an_expired_pass_is_dropped_rather_than_left_to_accumulate(self):
        token = adsgram_service.issue_nofill_token(self.TG_ID, "spin")
        record = adsgram_service._CLIENT_TOKENS[token]
        adsgram_service._CLIENT_TOKENS[token] = record._replace(
            expires_at=time.time() - 1)
        self.assertFalse(
            adsgram_service.consume_nofill_token(token, self.TG_ID, "spin"))
        self.assertNotIn(token, adsgram_service._CLIENT_TOKENS)

    def test_an_expired_pass_is_refused(self):
        token = adsgram_service.issue_nofill_token(self.TG_ID, "spin")
        record = adsgram_service._CLIENT_TOKENS[token]
        adsgram_service._CLIENT_TOKENS[token] = record._replace(
            expires_at=time.time() - 1)
        self.assertFalse(
            adsgram_service.consume_nofill_token(token, self.TG_ID, "spin"))

    def test_junk_is_refused_rather_than_crashing(self):
        for junk in ("", None, "NF-", "not-a-token"):
            self.assertFalse(
                adsgram_service.consume_nofill_token(junk, self.TG_ID, "spin"))


class SlotSpendTests(unittest.TestCase):
    """A pass unlocks the slot; the slot is still spent."""

    def test_a_rescued_spin_still_costs_an_ad_slot(self):
        stats = _mid_cycle()
        quota_service.claim_nofill_pass(stats, "spin")
        allowed, slot_type, _reason = quota_service.can_use(
            stats, "spin", ad_provided=True)
        self.assertTrue(allowed)
        self.assertEqual(slot_type, "ad")

        quota_service.consume_slot(stats, "spin", slot_type)
        self.assertEqual(stats.spin_ad_count, 1)

    def test_without_a_pass_or_an_ad_the_slot_stays_shut(self):
        stats = _mid_cycle()
        allowed, _slot, reason = quota_service.can_use(
            stats, "spin", ad_provided=False)
        self.assertFalse(allowed)
        self.assertEqual(reason, "ad_required")


if __name__ == "__main__":
    unittest.main()

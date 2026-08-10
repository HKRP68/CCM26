"""Tests for /membership — the reply a locked-out player is told to send.

The text is built from ``config.SUBSCRIPTION_TIERS``, so these pin that it
tells the truth about what the reader has and what the next step costs.
"""

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from config import SUBSCRIPTION_TIERS
from handlers.membership import build_membership_text


def _member(tier, days_left=20):
    return SimpleNamespace(
        subscription_tier=tier,
        subscription_expires_at=(None if tier == "none"
                                 else datetime.utcnow()
                                 + timedelta(days=days_left)),
    )


class FreeUserTests(unittest.TestCase):
    def setUp(self):
        self.text = build_membership_text(_member("none"), rookie_mode=False)

    def test_says_they_are_free(self):
        self.assertIn("Free", self.text)

    def test_lists_every_plan_with_its_price(self):
        for cfg in SUBSCRIPTION_TIERS.values():
            self.assertIn(f"₹{cfg['price_inr']}/mo", self.text)

    def test_a_user_with_no_account_at_all_still_gets_the_price_list(self):
        text = build_membership_text(None, rookie_mode=False)
        self.assertIn("₹15/mo", text)


class LockedOutTests(unittest.TestCase):
    def test_rookie_mode_explains_why_the_bot_went_quiet(self):
        text = build_membership_text(_member("none"), rookie_mode=True)
        self.assertIn("members-only", text)
        self.assertIn("Rookie", text)

    def test_a_member_is_not_told_the_bot_is_locked(self):
        text = build_membership_text(_member("rookie"), rookie_mode=True)
        self.assertNotIn("members-only", text)


class MemberTests(unittest.TestCase):
    def test_a_rookie_sees_access_but_no_perks_it_lacks(self):
        text = build_membership_text(_member("rookie"), rookie_mode=True)
        self.assertIn("Rookie", text)
        self.assertIn("Full access", text)
        self.assertNotIn("/cmumysterybox", text)
        self.assertNotIn("Autoplay", text)

    def test_a_diamond_sees_the_perks_it_pays_for(self):
        text = build_membership_text(_member("diamond"), rookie_mode=True)
        for bit in ("/cmumysterybox", "Autoplay", "/cmuweekly", "/cmuchest",
                    "10%"):
            self.assertIn(bit, text)

    def test_only_higher_tiers_are_offered_as_upgrades(self):
        text = build_membership_text(_member("platinum"), rookie_mode=False)
        self.assertIn("Upgrade to", text)
        self.assertIn("💎 Diamond", text)
        self.assertNotIn("🥉 Bronze", text)

    def test_the_top_tier_is_not_asked_to_upgrade(self):
        text = build_membership_text(_member("diamond"), rookie_mode=False)
        self.assertNotIn("Upgrade to", text)

    def test_days_left_are_shown(self):
        text = build_membership_text(_member("silver", days_left=9),
                                     rookie_mode=False)
        self.assertIn("days left", text)

    def test_a_lapsed_membership_says_expired_not_free(self):
        text = build_membership_text(_member("diamond", days_left=-1),
                                     rookie_mode=False)
        self.assertIn("Expired", text)
        self.assertIn("Diamond", text)


if __name__ == "__main__":
    unittest.main()

"""Who receives team-logo / CMU News review DMs, and who may decide them.

  • the environment's bot admins and owners always review
  • the added reviewers in ``approval_reviewer_ids`` review too
  • the maintenance-bypass testers do NOT — that leak is the reason this exists
  • only an environment admin/owner may change the reviewer list
  • a deployment with admins only in the admin config still gets reviews
"""

import os
import unittest
from unittest import mock

from services import admin_ids

_ENV_KEYS = admin_ids.ADMIN_ID_ENV_VARS + admin_ids.OWNER_ID_ENV_VARS


class ReviewerTests(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {k: "" for k in _ENV_KEYS})
        self._env.start()
        self.cfg = {"maintenance_bypass_ids": "900, 901",
                    "approval_reviewer_ids": "500"}
        self._cfg = mock.patch.object(admin_ids, "get_config",
                                      lambda *a, **k: self.cfg)
        self._cfg.start()

    def tearDown(self):
        self._cfg.stop()
        self._env.stop()

    def test_admins_owners_and_added_reviewers_review(self):
        os.environ["BOT_ADMIN_IDS"] = "100"
        os.environ["OWNER_IDS"] = "200"
        self.assertEqual(admin_ids.configured_reviewer_ids(), {100, 200, 500})
        for uid in (100, 200, 500):
            self.assertTrue(admin_ids.is_reviewer(uid))

    def test_maintenance_testers_are_not_reviewers(self):
        os.environ["BOT_ADMIN_IDS"] = "100"
        # Still "admins" for the maintenance gate...
        self.assertTrue(admin_ids.is_admin(900))
        # ...but they never see a review card nor may press one.
        self.assertNotIn(900, admin_ids.configured_reviewer_ids())
        self.assertFalse(admin_ids.is_reviewer(901))

    def test_only_environment_admins_manage_the_list(self):
        os.environ["BOT_ADMIN_IDS"] = "100"
        self.assertTrue(admin_ids.can_manage_reviewers(100))
        self.assertFalse(admin_ids.can_manage_reviewers(500))  # added reviewer
        self.assertFalse(admin_ids.can_manage_reviewers(900))  # tester
        self.assertFalse(admin_ids.can_manage_reviewers(None))

    def test_config_only_deployment_still_has_reviewers(self):
        self.cfg["approval_reviewer_ids"] = None
        # No env admins and no added reviewers: fall back rather than leave
        # every submission without anybody to approve it.
        self.assertEqual(admin_ids.configured_reviewer_ids(), {900, 901})
        self.assertTrue(admin_ids.can_manage_reviewers(900))

    def test_storage_form(self):
        self.assertEqual(admin_ids.format_id_list([3, 1, 3]), "1, 3")
        self.assertIsNone(admin_ids.format_id_list([]))


if __name__ == "__main__":
    unittest.main()

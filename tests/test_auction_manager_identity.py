"""Auction authority uses explicit bot identities, never maintenance exemptions."""

import os
import unittest
from unittest.mock import patch

from services import admin_ids


class AuctionManagerIdentityTests(unittest.TestCase):
    def setUp(self):
        keys = (*admin_ids.ADMIN_ID_ENV_VARS, *admin_ids.OWNER_ID_ENV_VARS)
        env = patch.dict(os.environ, {key:'' for key in keys})
        env.start()
        self.addCleanup(env.stop)
        self.config = {'maintenance_bypass_ids':'900,901'}
        config = patch.object(admin_ids, 'get_config', return_value=self.config)
        config.start()
        self.addCleanup(config.stop)

    def test_maintenance_bypass_alone_never_grants_bot_authority(self):
        self.assertTrue(admin_ids.is_admin(900))
        self.assertFalse(admin_ids.is_bot_admin(900))
        self.assertFalse(admin_ids.is_bot_admin(901))

    def test_each_explicit_admin_or_owner_source_is_recognized(self):
        keys = set(admin_ids.ADMIN_ID_ENV_VARS) | set(admin_ids.OWNER_ID_ENV_VARS)
        for key in keys:
            with self.subTest(source=key), patch.dict(os.environ, {key:'100'}):
                self.assertTrue(admin_ids.is_bot_admin(100))
                self.assertTrue(admin_ids.is_bot_admin('100'))
                self.assertFalse(admin_ids.is_bot_admin(900))

    def test_malformed_ids_do_not_receive_permissions(self):
        os.environ['BOT_ADMIN_IDS'] = '100'
        for value in (None, True, False, 100.0, '100.0', '100,900', -100, 0, '@100', '١٠٠'):
            with self.subTest(user=value):
                self.assertFalse(admin_ids.is_bot_admin(value))

    def test_maintenance_config_failure_cannot_promote_anybody(self):
        os.environ['BOT_OWNER_IDS'] = '100'
        with patch.object(admin_ids, 'get_config', side_effect=RuntimeError('Unavailable')):
            self.assertTrue(admin_ids.is_bot_admin(100))
            self.assertFalse(admin_ids.is_bot_admin(900))

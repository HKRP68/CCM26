"""Telegram commands and old buttons enforce the same auction roles as the API."""

import asyncio
import os
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_auction_bidding import ALICE, BOB, CAROL, setUpModule, tearDownModule
from test_auction_room_ux import RoomCase


class AuctionPermissionTests(RoomCase):
    def test_maintenance_bypass_users_cannot_manage_or_appoint_managers_in_chat(self):
        from handlers import auction as H, auction_trade as T
        from services import admin_ids
        env = {key:'' for key in (*admin_ids.ADMIN_ID_ENV_VARS, *admin_ids.OWNER_ID_ENV_VARS)}
        config = {'maintenance_bypass_ids':f'{ALICE},{BOB},{CAROL}'}
        with patch.dict(os.environ, env), patch.object(admin_ids, 'get_config', return_value=config):
            for uid in (ALICE, BOB, CAROL):
                self.assertTrue(admin_ids.is_admin(uid))
                self.assertFalse(H._is_auction_admin(uid))
                for handler in (H.astart_handler, H.apause_handler, H.anext_handler,
                                H.asold_handler, H.agrant_handler, H.aco_handler,
                                H.aretainforce_handler, T.atrade_handler):
                    self.replies.clear()
                    self.run_handler(handler, uid)
                    self.assertIn('Auction Managers', self.replies[-1])
                for handler in (H.aadminadd_handler, H.aadminremove_handler):
                    self.replies.clear()
                    self.run_handler(handler, uid, (str(987654),))
                    self.assertIn('Only bot admins', self.replies[-1])
                self.answers.clear()
                self.run_handler(H.release_callback, uid, data='au_rel_y_123')
                self.assertIn('Auction Managers', self.answers[-1][0])
            self.session.expire_all()
            self.assertEqual(self.season.status, self.A.STATUS_LIVE)
            self.assertEqual(self.mumbai.purse_remaining_lakh, self.purse_lakh)

    def test_lifecycle_and_purse_services_recheck_the_actor(self):
        from services import admin_ids
        env = {key:'' for key in (*admin_ids.ADMIN_ID_ENV_VARS, *admin_ids.OWNER_ID_ENV_VARS)}
        with patch.dict(os.environ, env), patch.object(admin_ids, 'get_config', return_value={
                'maintenance_bypass_ids':str(ALICE)}):
            deadline = self.lot.deadline_at
            with self.assertRaisesRegex(self.A.AuctionError, 'Only Auction Managers'):
                self.A.pause(self.session, self.season, by_tg_id=ALICE)
            self.assertEqual(self.season.status, self.A.STATUS_LIVE)
            self.assertEqual(self.lot.deadline_at, deadline)
            with self.assertRaisesRegex(self.A.AuctionError, 'Only Auction Managers'):
                self.A.correct_purse(self.session, self.season, self.mumbai, 100, by_tg_id=ALICE)
            self.assertEqual(self.mumbai.purse_remaining_lakh, self.purse_lakh)
            with self.assertRaisesRegex(self.A.AuctionError, 'Only bot admins'):
                self.A.add_auction_admin(self.session, ALICE, by_tg_id=ALICE)
            self.A.pause(self.session, self.season)
            with self.assertRaisesRegex(self.A.AuctionError, 'Only Auction Managers'):
                self.A.start(self.session, self.season, by_tg_id=ALICE)
            self.assertEqual(self.season.status, self.A.STATUS_PAUSED)
            self.assertIsNone(self.lot.deadline_at)

    def test_team_users_and_spectators_cannot_use_management_commands(self):
        from handlers import auction as H, auction_trade as T
        self.A.set_co_owners(self.session, self.mumbai, [CAROL])
        self.session.commit()
        commands = (H.astart_handler, H.apause_handler, H.anext_handler,
                    H.asold_handler, H.aunsold_handler, H.aextend_handler,
                    H.agrant_handler, H.aco_handler, H.aretainforce_handler,
                    H.retain_handler, H.aunretain_handler, H.retainlock_handler,
                    H.auction_pick_handler, T.atrade_handler,
                    T.atradecash_handler, T.atradecancel_handler)
        for uid in (ALICE, CAROL, 987654):
            for command in commands:
                with self.subTest(uid=uid, command=command.__name__):
                    self.replies.clear()
                    self.run_handler(command, uid)
                    self.assertTrue(any('Auction Managers' in text for text in self.replies), self.replies)
        self.session.expire_all()
        self.assertEqual(self.season.status, self.A.STATUS_LIVE)
        self.assertEqual(self.mumbai.purse_remaining_lakh, self.purse_lakh)
        self.assert_ledger_agrees()

    def test_old_squad_and_trade_buttons_are_denied_without_mutations(self):
        from handlers import auction as H, auction_trade as T
        for callback in (H.retention_offer_callback, H.retention_talk_callback,
                         H.retention_pick_callback, H.retention_final_callback,
                         H.release_callback, T.trade_callback):
            for uid in (ALICE, BOB, 987654):
                with self.subTest(callback=callback.__name__, uid=uid):
                    self.answers.clear()
                    self.run_handler(callback, uid, data='old-management-button')
                    self.assertEqual(len(self.answers), 1)
                    self.assertIn('Auction Managers', self.answers[0][0])
                    self.assertTrue(self.answers[0][1])
        self.assert_ledger_agrees()

    def test_delegated_manager_can_pause_resume_and_loses_access_when_removed(self):
        from handlers import auction as H
        manager = 987650
        self.A.add_auction_admin(self.session, manager)
        self.session.commit()
        try:
            self.run_handler(H.apause_handler, manager)
            self.session.expire_all()
            self.assertEqual(self.season.status, self.A.STATUS_PAUSED)
            self.run_handler(H.astart_handler, manager)
            self.session.expire_all()
            self.assertEqual(self.season.status, self.A.STATUS_LIVE)
            self.A.remove_auction_admin(self.session, manager)
            self.session.commit()
            self.run_handler(H.apause_handler, manager)
            self.session.expire_all()
            self.assertEqual(self.season.status, self.A.STATUS_LIVE)
            self.assertIn('Auction Managers', self.replies[-1])
        finally:
            from models import AuctionAdmin
            self.session.query(AuctionAdmin).filter_by(tg_id=manager).delete()
            self.session.commit()

    def test_paused_and_completed_boards_refresh_when_the_purse_changes(self):
        from services import auction_scheduler as S
        from models import AuctionEvent
        self.A.pause(self.session, self.season)
        self.season.board_message_id = 123
        self.session.commit()
        self.season.announced_event_id = self.session.query(AuctionEvent.id).filter_by(
            season_id=self.season.id).order_by(AuctionEvent.id.desc()).first()[0]
        self.session.commit()
        for status in (self.A.STATUS_PAUSED, self.A.STATUS_COMPLETED):
            self.season.status = status
            self.A.correct_purse(self.session, self.season, self.mumbai, 125)
            self.session.commit()
            before = (self.lot.status, self.lot.deadline_at, self.mumbai.purse_remaining_lakh)
            board = AsyncMock()
            with patch.object(S, '_send', AsyncMock(return_value=SimpleNamespace(message_id=1))), \
                 patch.object(S, 'refresh_board', board):
                asyncio.run(S._refresh_idle_boards(SimpleNamespace(bot=object()), self.session, datetime.utcnow()))
                board.assert_awaited_once()
                asyncio.run(S._refresh_idle_boards(SimpleNamespace(bot=object()), self.session, datetime.utcnow()))
                board.assert_awaited_once()
            self.assertEqual(before, (self.lot.status, self.lot.deadline_at, self.mumbai.purse_remaining_lakh))
            self.assert_ledger_agrees()

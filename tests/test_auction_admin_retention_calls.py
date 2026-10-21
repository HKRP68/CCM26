"""Admin reopening keeps other teams final; calls deliver media to group and DMs."""
import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests import test_auction_features as F

setUpModule = F.setUpModule
tearDownModule = F.tearDownModule


class RetentionAdminTests(F.FeatureCase):
    def setUp(self):
        super().setUp()
        self.season.max_retentions = 3
        self.session.commit()

    def test_unlock_all_clears_global_and_team_locks(self):
        self.A.finalize_retention(self.session, self.season, self.mumbai)
        self.A.set_retention_lock(self.session, self.season, True)
        self.A.set_retention_lock(self.session, self.season, False)
        self.assertTrue(self.A.retention_open(self.season))
        self.assertEqual(3, len(self.A.pending_retention_teams(self.session, self.season)))
        self.assertIsNone(self.mumbai.retention_finalized_at)

    def test_unlock_one_preserves_other_teams_effective_locks(self):
        self.A.set_retention_lock(self.session, self.season, True)
        self.A.set_retention_lock(self.session, self.season, False, self.mumbai)
        self.assertTrue(self.A.retention_open(self.season))
        self.assertIsNone(self.mumbai.retention_finalized_at)
        self.assertIsNotNone(self.chennai.retention_finalized_at)
        self.assertIsNotNone(self.delhi.retention_finalized_at)
        self.assertEqual([self.mumbai.id], [f.id for f in
            self.A.pending_retention_teams(self.session, self.season)])
        self.A.set_retention_lock(self.session, self.season, True, self.mumbai)
        self.assertTrue(self.A.retention_locked(self.season))

    def test_lock_one_leaves_others_open_and_blocks_its_signings(self):
        self.A.set_retention_lock(self.session, self.season, True, self.mumbai)
        self.assertFalse(self.A.retention_locked(self.season))
        self.assertIsNone(self.chennai.retention_finalized_at)
        with self.assertRaisesRegex(self.A.AuctionError, 'finalized retention'):
            self.A.retain(self.session, self.season, self.mumbai, self.players[0])
        self.session.rollback()

    def test_reopening_after_draft_picks_is_refused(self):
        self.delhi.draft_picks_used = 1
        self.A.set_retention_lock(self.session, self.season, True)
        with self.assertRaisesRegex(self.A.AuctionError, 'draft picks have started'):
            self.A.set_retention_lock(self.session, self.season, False, self.mumbai)
        self.assertTrue(self.A.retention_locked(self.season))

    def test_reopening_expired_deadline_actually_opens_retention(self):
        now = datetime.utcnow()
        self.season.retention_deadline_at = now - timedelta(minutes=1)
        self.A.set_retention_lock(self.session, self.season, False, now=now)
        self.assertIsNone(self.season.retention_deadline_at)
        self.assertTrue(self.A.retention_open(self.season, now))

    def test_future_deadline_is_preserved(self):
        deadline = datetime.utcnow() + timedelta(hours=1)
        self.season.retention_deadline_at = deadline
        self.A.set_retention_lock(self.session, self.season, False)
        self.assertEqual(deadline, self.season.retention_deadline_at)

    def test_auction_already_started_refuses_both_changes(self):
        self.build_sets()
        self.start()
        for locked in (True, False):
            with self.assertRaisesRegex(self.A.AuctionError, 'before the auction opens'):
                self.A.set_retention_lock(self.session, self.season, locked)
            self.session.rollback()

    def test_team_lock_requires_resolving_pending_offers(self):
        self.A.offer_retention(self.session, self.season, self.mumbai, self.players[0])
        with self.assertRaisesRegex(self.A.AuctionError, 'pending retention offers'):
            self.A.set_retention_lock(self.session, self.season, True, self.mumbai)
        self.assertIsNone(self.mumbai.retention_finalized_at)

    def test_commands_support_all_and_named_team_and_enforce_admin(self):
        from handlers import auction as H
        with F.AdminEnv(F.CAROL):
            self.run_handler(H.aretlock_handler, F.CAROL, ('on', 'all'))
            self.assertTrue(self.A.retention_locked(self.season))
            denied = self.run_handler(H.aretlock_handler, F.ALICE, ('off', 'Mumbai'))
            self.assertIn('Only auction admins', denied.replies[-1])
            self.assertTrue(self.A.retention_locked(self.season))
            self.run_handler(H.aretlock_handler, F.CAROL, ('off', 'Mumbai'))
            self.assertIsNone(self.mumbai.retention_finalized_at)
            self.assertIsNotNone(self.chennai.retention_finalized_at)
            self.run_handler(H.aretlock_handler, F.CAROL, ('on', 'Mumbai'))
            self.assertTrue(self.A.retention_locked(self.season))
            self.run_handler(H.aretlock_handler, F.CAROL, ('off', 'all'))
            self.assertIsNone(self.chennai.retention_finalized_at)


class AuctionCallTests(F.FeatureCase):
    def call(self, *, actor=F.CAROL, text='Auction in 5 minutes', caption=None,
             photo=None, reply_to=None, bot=None):
        from handlers import auction as H
        bot = bot or SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock(),
                                     send_document=AsyncMock(), username='ourbot')
        reply = AsyncMock()
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=self.season.chat_id, type='supergroup'),
            effective_user=SimpleNamespace(id=actor),
            effective_message=SimpleNamespace(reply_text=reply, photo=photo,
                caption=caption, reply_to_message=reply_to, document=None))
        self.session.commit()
        with F.AdminEnv(F.CAROL):
            asyncio.run(H.acall_handler(update, SimpleNamespace(args=text.split(), bot=bot)))
        self.session.expire_all()
        return bot, reply

    def test_text_is_sent_to_group_and_each_unique_owner_or_coowner(self):
        self.chennai.co_owner_ids_json = json.dumps([F.ALICE, F.ERIN])
        bot, reply = self.call(text='Please join <now>')
        calls = bot.send_message.call_args_list
        self.assertEqual({self.season.chat_id, F.ALICE, F.BOB, F.DAVE, F.ERIN},
                         {c.kwargs['chat_id'] for c in calls})
        self.assertEqual(5, len(calls))
        group = next(c for c in calls if c.kwargs['chat_id'] == self.season.chat_id)
        for uid in (F.ALICE, F.BOB, F.DAVE, F.ERIN):
            self.assertIn(f'tg://user?id={uid}', group.kwargs['text'])
        self.assertIn('Please join &lt;now&gt;', group.kwargs['text'])
        self.assertIn('4 delivered, 0 unavailable', reply.call_args.args[0])

    def test_image_caption_command_sends_image_and_message_everywhere(self):
        bot, _ = self.call(caption='/acall@ourbot Retention closes soon', text='',
                           photo=[SimpleNamespace(file_id='small'), SimpleNamespace(file_id='large')])
        self.assertEqual(4, bot.send_photo.await_count)
        for call in bot.send_photo.call_args_list:
            self.assertEqual('large', call.kwargs['photo'])
            self.assertIn('Retention closes soon', call.kwargs['caption'])
            self.assertLessEqual(len(call.kwargs['caption']), 1024)
        bot.send_message.assert_not_awaited()

    def test_reply_to_image_uses_custom_message(self):
        source = SimpleNamespace(photo=[SimpleNamespace(file_id='replied')],
                                 caption='Original caption')
        bot, _ = self.call(reply_to=source, text='Use this announcement')
        self.assertEqual(4, bot.send_photo.await_count)
        for call in bot.send_photo.call_args_list:
            self.assertEqual('replied', call.kwargs['photo'])
            self.assertIn('Use this announcement', call.kwargs['caption'])

    def test_image_document_and_replied_caption_are_preserved(self):
        source = SimpleNamespace(photo=[], caption='Details from image',
            document=SimpleNamespace(file_id='image-doc', mime_type='image/png'))
        bot, _ = self.call(reply_to=source, text='')
        self.assertEqual(4, bot.send_document.await_count)
        for call in bot.send_document.call_args_list:
            self.assertEqual('image-doc', call.kwargs['document'])
            self.assertIn('Details from image', call.kwargs['caption'])

    def test_failed_dm_does_not_stop_other_deliveries(self):
        from telegram.error import Forbidden
        async def send(**kwargs):
            if kwargs['chat_id'] == F.BOB:
                raise Forbidden('Bot blocked')
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=send))
        bot, reply = self.call(bot=bot)
        self.assertEqual(4, bot.send_message.await_count)
        self.assertIn('2 delivered, 1 unavailable', reply.call_args.args[0])

    def test_long_message_with_photo_uses_text_parts_without_losing_tags(self):
        bot, _ = self.call(text='Long & message ' * 400,
                           photo=[SimpleNamespace(file_id='image')])
        self.assertEqual(4, bot.send_photo.await_count)
        group_texts = [c.kwargs['text'] for c in bot.send_message.call_args_list
                       if c.kwargs['chat_id'] == self.season.chat_id]
        self.assertTrue(all(len(part) <= 4096 for part in group_texts))
        self.assertIn(f'tg://user?id={F.ALICE}', ''.join(group_texts))
        self.assertEqual(400, ''.join(group_texts).count('&amp;'))

    def test_non_admin_cannot_broadcast(self):
        bot, reply = self.call(actor=F.ALICE)
        bot.send_message.assert_not_awaited()
        bot.send_photo.assert_not_awaited()
        self.assertIn('Only auction admins', reply.call_args.args[0])

    def test_caption_addressed_to_another_bot_is_ignored(self):
        bot, reply = self.call(caption='/acall@anotherbot A different bot',
                               photo=[SimpleNamespace(file_id='image')])
        bot.send_message.assert_not_awaited()
        bot.send_photo.assert_not_awaited()
        reply.assert_not_awaited()

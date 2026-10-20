"""Multi-channel photo captions, Super Over albums, retries and ceremony mirrors."""
import asyncio
import io
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _tour_fixture as F


def setUpModule(): F.setup_module_db()
def tearDownModule(): F.teardown_module_db()


class ChannelTests(F.FourTeamCase):
    def setUp(self):
        super().setUp()
        from services import tournament_channels as C
        self.C = C
        self.image = C.fallback_card('MATCH SUMMARY')

    def finish(self):
        self.finish_league()
        for fx in self.fixtures(stage='semifinal'):
            self.ts.simulate_fixture(self.session, fx.id, '1')
        final = self.fixtures(stage='final')[0]
        self.ts.simulate_fixture(self.session, final.id, '1')
        self.session.commit()
        return final

    def bot(self):
        return SimpleNamespace(send_photo=AsyncMock(return_value=SimpleNamespace(message_id=1, photo=[])),
            send_media_group=AsyncMock(return_value=[SimpleNamespace(message_id=2), SimpleNamespace(message_id=3)]), send_message=AsyncMock())

    def test_channels_validate_and_deduplicate(self):
        self.assertEqual(self.C.set_channels(self.tour, '@MyChannel\n-10012345, @mychannel'), ['@mychannel', '-10012345'])
        for invalid in ('12345', '@bad', 'https://evil.example', '@channel;private'):
            with self.assertRaises(ValueError): self.C.set_channels(self.tour, invalid)

    def test_two_destinations_get_one_photo_caption_and_no_duplicate(self):
        from models import TournamentChannelPost as Post
        self.C.set_channels(self.tour, '@channel_one\n@channel_two')
        rows = self.C.queue(self.session, self.tour, 'match:100', 'match', '🏏 Result summary', self.image)
        ids = [r.id for r in rows]; self.session.commit()
        bot = self.bot(); context = SimpleNamespace(bot=bot)
        asyncio.run(self.C.dispatch(context, ids=ids)); asyncio.run(self.C.dispatch(context, ids=ids))
        self.assertEqual(bot.send_photo.await_count, 2)
        self.assertEqual({c.kwargs['chat_id'] for c in bot.send_photo.call_args_list}, {'@channel_one', '@channel_two'})
        self.assertTrue(all(c.kwargs['caption'] == '🏏 Result summary' for c in bot.send_photo.call_args_list))
        bot.send_message.assert_not_awaited()
        self.session.expire_all()
        self.assertEqual(self.session.query(Post).filter_by(tournament_id=self.tour.id, status='sent').count(), 2)

    def test_failure_in_one_channel_retries_without_reposting_successful_channel(self):
        from models import TournamentChannelPost as Post
        self.C.set_channels(self.tour, '@channel_one @channel_two')
        ids = [r.id for r in self.C.queue(self.session, self.tour, 'match:retry', 'match', 'Result', self.image)]
        self.session.commit()
        async def send(**kwargs):
            if kwargs['chat_id'] == '@channel_two': raise RuntimeError('missing permission')
            return SimpleNamespace(message_id=1)
        bot = self.bot(); bot.send_photo.side_effect = send
        asyncio.run(self.C.dispatch(SimpleNamespace(bot=bot), ids=ids))
        self.session.expire_all()
        pending = self.session.query(Post).filter_by(tournament_id=self.tour.id, status='pending').one()
        self.assertIn('missing permission', pending.last_error)
        pending.next_attempt_at = None; self.session.commit()
        bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=4))
        asyncio.run(self.C.dispatch(SimpleNamespace(bot=bot), ids=ids))
        self.assertEqual(bot.send_photo.await_count, 1)
        self.assertEqual(bot.send_photo.call_args.kwargs['chat_id'], '@channel_two')

    def test_super_over_is_one_media_group_with_summary_as_image_caption(self):
        final = self.finish()
        final.match_id = 90001; final.inn1_runs = final.inn2_runs = 172
        final.inn1_wickets, final.inn2_wickets = 6, 1
        final.inn1_balls = final.inn2_balls = 120
        self.C.set_channels(self.tour, '@channel_one @channel_two'); self.session.commit()
        state = dict(tournament_id=self.tour.id, match_id=90001, inn1_bat_team='Delhi Demons', bat_team_name='Atlético de Kolkata')
        result = dict(tie=False, winner='Atlético de Kolkata', margin=2, margin_type='wickets')
        so = dict(so_innings=[dict(team='Delhi Demons', runs=0, wickets=2, overs='0.2'),
                             dict(team='Atlético de Kolkata', runs=1, wickets=0, overs='0.1')])
        star = dict(name='Sanju Samson', bat=dict(runs=82, balls=55, out=False), bowl={})
        bot = self.bot()
        with patch('handlers.cipl_play._build_cipl_summary_image', return_value=self.image), \
                patch('handlers.cipl_play._cipl_potm_select', return_value=star), \
                patch('handlers.super_over._build_super_over_card', return_value=self.image):
            asyncio.run(self.C.publish_match(SimpleNamespace(bot=bot), state, result, super_over=so))
            asyncio.run(self.C.publish_match(SimpleNamespace(bot=bot), state, result, super_over=so))
        self.assertEqual(bot.send_media_group.await_count, 2)
        media = bot.send_media_group.call_args.kwargs['media']
        self.assertEqual(len(media), 2)
        caption = media[0].caption
        for text in ('FINAL MATCH', 'MATCH TIED', '172/6', '172/1', '0/2 (0.2', '1/0 (0.1', 'Sanju Samson', '149.1', 'CHAMPIONS', '2 WICKETS'):
            self.assertIn(text, caption)
        self.assertLessEqual(len(caption.encode('utf-16-le'))//2, 1024)
        self.assertIsNone(media[1].caption)
        bot.send_photo.assert_not_awaited(); bot.send_message.assert_not_awaited()

    def test_unfinished_match_never_publishes(self):
        fx = self.fixtures(1)[0]; fx.match_id = 90002
        self.C.set_channels(self.tour, '@channel_one'); self.session.commit()
        bot = self.bot()
        asyncio.run(self.C.publish_match(SimpleNamespace(bot=bot), {'tournament_id':self.tour.id,'match_id':90002}, {'tie':True}))
        bot.send_photo.assert_not_awaited(); bot.send_media_group.assert_not_awaited()

    def test_ceremony_mirrors_exactly_eleven_awards_once_to_each_channel(self):
        from services import tournament_awards as A, tournament_ceremony as C
        from models import TournamentChannelPost as Post
        self.finish()
        self.C.set_channels(self.tour, '@channel_one @channel_two'); self.session.commit()
        bot = self.bot()
        with patch.object(C, 'award_photo', return_value=self.image), patch.object(C.asyncio, 'sleep', new=AsyncMock()):
            asyncio.run(C.present(SimpleNamespace(bot=bot), self.tour.id, -10055))
        self.assertEqual(bot.send_photo.await_count, 33)
        for destination in (-10055, '@channel_one', '@channel_two'):
            captions = [c.kwargs['caption'] for c in bot.send_photo.call_args_list if c.kwargs['chat_id'] == destination]
            self.assertEqual(len(captions), 11)
            for award, caption in zip(A.CEREMONY_AWARDS, captions): self.assertIn(A.AWARD_LABEL[award], caption)
            self.assertFalse(any('<b>🏆 Champion</b>' in c or '<b>🥈 Runner-up</b>' in c for c in captions))
        self.session.expire_all()
        self.assertEqual(self.session.query(Post).filter_by(tournament_id=self.tour.id, status='sent').count(), 22)
        with self.assertRaises(ValueError): asyncio.run(C.present(SimpleNamespace(bot=bot), self.tour.id, -10055))
        self.assertEqual(bot.send_photo.await_count, 33)

    def test_manual_result_is_queued_inside_its_transaction(self):
        from models import TournamentChannelPost as Post
        self.C.set_channels(self.tour, '@channel_one'); self.session.commit()
        fixture = self.fixtures(1)[0]
        with patch('services.scorecard_delivery.render_card', return_value=self.image):
            self.ts.simulate_fixture(self.session, fixture.id, '1')
        self.assertEqual(self.session.query(Post).filter_by(tournament_id=self.tour.id).count(), 1)
        self.session.rollback()
        self.assertEqual(self.session.query(Post).filter_by(tournament_id=self.tour.id).count(), 0)

    def test_overs_and_long_caption_do_not_become_separate_text_messages(self):
        from PIL import Image
        self.C.set_channels(self.tour, '@channel_one')
        full = 'Super Over histories\n' + ('Very long match summary line.\n' * 100)
        row = self.C.queue(self.session, self.tour, 'long', 'match', full, self.image)[0]
        self.session.commit()
        self.assertLessEqual(len(row.caption.encode('utf-16-le'))//2, 1024)
        self.assertGreater(Image.open(io.BytesIO(row.photo)).height, Image.open(io.BytesIO(self.image)).height)
        bot = self.bot(); asyncio.run(self.C.dispatch(SimpleNamespace(bot=bot), ids=[row.id]))
        bot.send_photo.assert_awaited_once(); bot.send_message.assert_not_awaited()

    def test_two_super_overs_include_both_rounds_and_the_deciding_scores(self):
        final = self.finish()
        state = dict(inn1_bat_team='Delhi', bat_team_name='Kolkata')
        innings = [dict(team=team, runs=runs, wickets=wickets, overs=overs)
                   for team,runs,wickets,overs in [('Delhi',10,1,'1.0'),('Kolkata',10,2,'1.0'),
                                                   ('Kolkata',12,1,'1.0'),('Delhi',13,0,'0.5')]]
        so = dict(so_innings=innings, so_number=2, _winner_uid=1, _margin_text='won by 2 wickets')
        with patch('handlers.super_over._build_super_over_card', return_value=self.image) as render:
            photo = self.C.super_over_image(so)
        self.assertEqual(render.call_count, 2)
        self.assertEqual(render.call_args_list[0].args[0]['so_innings'], innings[:2])
        self.assertEqual(render.call_args_list[0].args[0]['_margin_text'], 'Super Over tied')
        self.assertEqual(render.call_args_list[1].args[0]['so_innings'], innings[2:])
        self.assertEqual(render.call_args_list[1].args[0]['_winner_uid'], 1)
        from PIL import Image
        self.assertEqual(Image.open(io.BytesIO(photo)).height, 2*Image.open(io.BytesIO(self.image)).height)
        with patch('handlers.cipl_play._cipl_potm_select', return_value=None):
            text = self.C.match_caption(self.tour, final, state, dict(winner='Delhi',margin=2,margin_type='wickets'), super_over=so)
        self.assertIn('SUPER OVER 2 RESULT', text)
        self.assertIn('13/0 (0.5', text)

    def test_channel_command_requires_admin_and_saves_multiple_destinations(self):
        from handlers import tournament_admin as H
        from database import get_session
        update = SimpleNamespace()
        context = SimpleNamespace(args=[f'#{self.tour.id}', 'add', '@first_channel', '@second_channel'])
        with patch.object(H, '_require_admin', new=AsyncMock(return_value=True)), patch.object(H, 'get_session', get_session), patch.object(H, '_reply', new=AsyncMock()):
            asyncio.run(H.tchannels_handler(update, context))
        self.session.expire_all()
        self.assertEqual(self.C.channels(self.tour), ['@first_channel', '@second_channel'])
        with patch.object(H, '_require_admin', new=AsyncMock(return_value=False)), patch.object(H, 'get_session') as access:
            asyncio.run(H.tchannels_handler(update, context))
            access.assert_not_called()

    def test_deciding_super_over_card_uses_the_last_round(self):
        from handlers.super_over import _build_super_over_card
        innings = [dict(team=team, runs=runs, wickets=0, overs='1.0', batters=[], bowlers=[])
                   for team, runs in [('Delhi', 10), ('Kolkata', 10), ('Kolkata', 12), ('Delhi', 13)]]
        so = dict(so_innings=innings, teams={1: {'name': 'Delhi'}}, _winner_uid=1, so_number=2)
        with patch('services.match_summary_card.generate_match_summary', return_value=self.image) as render, \
                patch('services.config_service.get_config', return_value={}), \
                patch('services.card_identity.open_session'), \
                patch('services.card_identity.summary_visuals', return_value={}):
            self.assertEqual(_build_super_over_card(so), self.image)
        self.assertEqual(render.call_args.kwargs['inn1_runs'], 12)
        self.assertEqual(render.call_args.kwargs['inn2_runs'], 13)
        self.assertEqual(render.call_args.kwargs['header_right'], 'OVER 2')

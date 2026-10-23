"""Standalone invitations, gameplay, cleanup and unranked-result isolation."""
import asyncio
import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _tour_fixture as F


def setUpModule(): F.setup_module_db()
def tearDownModule(): F.teardown_module_db()


class PracticeSuperOverTests(unittest.TestCase):
    def setUp(self):
        from database import get_session
        from models import User, Match, MatchScorecard
        from handlers import play_super_over as H, super_over as SO
        from handlers import match as match_handler
        from services import match_state_store
        self.H, self.SO = H, SO
        self.Match, self.Card = Match, MatchScorecard
        self.db = get_session()
        self.users = [User(telegram_id=F.next_tg(), username=f'so{F.next_tg()}',
                           first_name=f'Captain {i}', total_coins=4000, total_gems=50,
                           matches_played=0, matches_won=0, win_streak=0) for i in range(3)]
        self.db.add_all(self.users)
        self.db.commit()
        self.ctx = SimpleNamespace(bot_data={}, job_queue=None, args=[],
            bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=10)),
                                edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock(),
                                send_photo=AsyncMock()))
        self.patches = [patch.object(H, 'get_session', get_session), patch.object(SO, 'get_session', get_session),
                        patch.object(match_handler, 'get_session', get_session),
                        patch.object(match_state_store, 'get_session', get_session),
                        patch.object(H, '_xi', side_effect=self.xi), patch.object(SO, '_BALL_PAUSE', 0),
                        patch.object(SO, '_build_super_over_card', return_value=None)]
        for p in self.patches: p.start()

    def tearDown(self):
        self.db.query(self.Match).filter(self.Match.user1_id.in_([u.id for u in self.users]),
            self.Match.status.in_(self.H.ACTIVE)).update(dict(status='cancelled'), synchronize_session=False)
        self.db.commit()
        for p in reversed(self.patches): p.stop()
        self.db.close()

    def xi(self, session, user):
        return [dict(roster_id=user.id * 100 + i, player_id=user.id * 100 + i,
                     name=f'Player {user.id}-{i}', rating=75, category='All-rounder',
                     bat_rating=75, bowl_rating=70, bowl_style='Fast', bowl_hand='Right', bat_hand='Right')
                for i in range(1, 12)]

    def update(self, user=0, chat=-10055):
        u = self.users[user]
        tg = SimpleNamespace(id=u.telegram_id, username=u.username, first_name=u.first_name)
        return SimpleNamespace(effective_user=tg, effective_chat=SimpleNamespace(id=chat),
                               message=SimpleNamespace(reply_text=AsyncMock(return_value=SimpleNamespace(message_id=5))))

    def query(self, data, tg):
        q = SimpleNamespace(data=data, from_user=SimpleNamespace(id=tg),
                            message=SimpleNamespace(chat_id=-10055), answer=AsyncMock(),
                            edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock())
        return SimpleNamespace(callback_query=q)

    def invite(self):
        update = self.update()
        with patch.object(self.H, 'resolve_command_target', return_value=(self.users[1], 'username')):
            asyncio.run(self.H.superover_handler(update, self.ctx))
        self.db.expire_all()
        row = self.db.query(self.Match).filter_by(user1_id=self.users[0].id).order_by(self.Match.id.desc()).first()
        return row, update

    def accept(self, row, who=1):
        update = self.query(f'sop_accept_{row.id}', self.users[who].telegram_id)
        from services import button_access
        update.callback_query.message.message_id = 5
        button_access.register_button_owner(row.chat_id, 5, self.users[0].telegram_id)
        try:
            self.assertTrue(button_access.check_callback_owner(update))
            asyncio.run(self.H.superover_invite_callback(update, self.ctx))
        finally:
            button_access._OWNER_BY_MESSAGE.clear()
        self.db.expire_all()
        return update

    def test_invitation_is_unranked_and_only_the_guest_can_accept(self):
        row, update = self.invite()
        self.assertEqual(row.match_type, 'super_over_practice')
        self.assertEqual((row.status, row.overs), ('pending', 1))
        self.assertIn('UNRANKED SUPER OVER', update.message.reply_text.call_args.args[0])
        rejected = self.accept(row, who=2)
        self.assertEqual(row.status, 'pending')
        self.assertIn('Only the invited', rejected.callback_query.answer.call_args.args[0])
        self.assertNotIn(f'so_{row.id}', self.ctx.bot_data)

    def test_league_launch_honours_toss_and_skips_regular_innings(self):
        from handlers import cipl_play as cp
        from database import get_session
        from models import MatchState
        for decision, first in (("bat", 0), ("bowl", 1)):
            with self.subTest(decision=decision):
                draft = dict(standalone_super_over=True, chat_id=-10055,
                    host_user_id=self.users[0].id, target_user_id=self.users[1].id,
                    host_tg_id=self.users[0].telegram_id, target_tg_id=self.users[1].telegram_id,
                    host_team="Mumbai Indians", target_team="Chennai Super Kings",
                    host=dict(name="Host"), target=dict(name="Guest"),
                    league_key="ipl", league_name="IPL", ball_format="The100",
                    toss_decision=decision, toss_winner_side="host")
                query = self.query("cl_start_123", self.users[0].telegram_id).callback_query
                query.message.message_id = 5
                with patch.object(cp, 'get_session', get_session), \
                     patch.object(cp, 'build_xi_from_draft', side_effect=lambda s, d, side: self.xi(s, self.users[0 if side == 'host' else 1])[:3]), \
                     patch.object(cp, 'build_bench_from_draft', return_value=[]), \
                     patch.object(cp, '_resolve_team_identity', return_value=('XI', '🏏')), \
                     patch.object(cp, '_ss', new=AsyncMock()) as save_main, \
                     patch.object(cp, '_prompt_bowler', new=AsyncMock()) as main_prompt:
                    asyncio.run(cp._launch_after_toss(self.ctx, query, draft, 123, decision, 'host'))
                self.assertTrue(draft.get('match_launched'), query.answer.call_args_list)
                mid = draft['match_id']
                row = self.db.get(self.Match, mid)
                self.assertEqual((row.match_type, row.overs), ('super_over_practice', 1))
                self.assertEqual(row.batting_first_id, self.users[first].id)
                self.assertIsNone(row.tournament_id)
                self.assertEqual(self.db.query(MatchState).filter_by(match_id=mid).count(), 0)
                so = self.ctx.bot_data[f'so_{mid}']
                self.assertEqual(so['bat_uid'], self.users[first].id)
                self.assertTrue(so['main_state']['unranked'])
                self.assertTrue(so['bat_confirmed'])
                self.assertEqual(len(so['sel_batters']), 3)
                for team in so['teams'].values():
                    self.assertEqual(len(team['xi']), 3)
                text, keyboard = self.SO._render_selection(so)
                self.assertNotIn('Confirm Batters', text)
                self.assertFalse(any(button.callback_data.startswith('so_bat') for row in keyboard for button in row))
                self.assertEqual(so['main_state']['ball_format'], 'T20')
                heading = next(call.args[1] for call in reversed(self.ctx.bot.send_message.call_args_list)
                               if 'UNRANKED SUPER OVER</b>' in str(call.args[1]))
                self.assertIn('Toss decision:', heading)
                self.assertNotIn('Random toss:', heading)
                save_main.assert_not_awaited()
                main_prompt.assert_not_awaited()
                # Old Start buttons must neither cancel nor relaunch this game.
                with patch.object(cp, '_launch_after_toss', new=AsyncMock()) as launch, \
                     patch.object(self.SO, 'resume_super_over', new=AsyncMock(return_value=True)):
                    asyncio.run(cp.start_match_launch(self.ctx, query, draft, 123))
                    launch.assert_not_awaited()
                    self.ctx.bot_data.pop(f'so_{mid}')
                    asyncio.run(cp.start_match_launch(self.ctx, query, draft, 123))
                    launch.assert_not_awaited()
                asyncio.run(self.H.cancel_super_over(self.update(), self.ctx))
                self.db.expire_all()
                self.assertEqual(self.db.get(self.Match, mid).status, 'cancelled')

    def test_accept_starts_only_once_and_does_not_claim_a_main_match_tied(self):
        row, _ = self.invite()
        self.accept(row)
        self.assertEqual(row.status, 'playing')
        self.assertTrue(self.ctx.bot_data[f'so_{row.id}']['main_state']['unranked'])
        before = self.ctx.bot.send_message.await_count
        self.accept(row)
        self.assertEqual(self.ctx.bot.send_message.await_count, before)
        text = '\n'.join(str(call.args[1]) for call in self.ctx.bot.send_message.call_args_list)
        self.assertIn('UNRANKED SUPER OVER', text)
        self.assertNotIn('MATCH TIED', text)

    def test_decline_and_expiry_release_the_invitation(self):
        row, _ = self.invite()
        update = self.query(f'sop_deny_{row.id}', self.users[1].telegram_id)
        asyncio.run(self.H.superover_invite_callback(update, self.ctx))
        self.db.expire_all()
        self.assertEqual(row.status, 'cancelled')
        row, _ = self.invite()
        row.expires_at = datetime.utcnow() - timedelta(seconds=1)
        self.db.commit()
        self.accept(row)
        self.assertEqual(row.status, 'expired')

    def test_xi_is_revalidated_on_accept_and_failure_frees_the_match(self):
        row, _ = self.invite()
        with patch.object(self.H, '_xi', side_effect=ValueError('Fix your Playing XI.')):
            self.accept(row)
        self.assertEqual(row.status, 'cancelled')
        self.assertNotIn(f'so_{row.id}', self.ctx.bot_data)

    def test_invalid_playing_xi_does_not_reserve_a_match(self):
        update = self.update()
        with patch.object(self.H, 'resolve_command_target', return_value=(self.users[1], 'username')), \
             patch.object(self.H, '_xi', side_effect=ValueError('Fix your Playing XI.')):
            asyncio.run(self.H.superover_handler(update, self.ctx))
        self.assertEqual(self.db.query(self.Match).filter_by(user1_id=self.users[0].id).count(), 0)

    def test_expiry_job_cleans_up_the_game_and_cannot_expire_a_finished_result(self):
        row, _ = self.invite()
        self.accept(row)
        row.expires_at = datetime.utcnow() - timedelta(seconds=1)
        self.db.commit()
        self.ctx.job = SimpleNamespace(data=dict(mid=row.id, chat_id=row.chat_id, status='playing'))
        asyncio.run(self.H.expire_super_over(self.ctx))
        self.db.expire_all()
        self.assertEqual(row.status, 'expired')
        self.assertNotIn(f'so_{row.id}', self.ctx.bot_data)
        row.status = 'completed'
        self.db.commit()
        asyncio.run(self.H.expire_super_over(self.ctx))
        self.db.expire_all()
        self.assertEqual(row.status, 'completed')

    def test_an_existing_match_blocks_another_invitation(self):
        row, _ = self.invite()
        other = self.update(user=2)
        with patch.object(self.H, 'resolve_command_target', return_value=(self.users[0], 'username')):
            asyncio.run(self.H.superover_handler(other, self.ctx))
        self.assertIn('already open', other.message.reply_text.call_args.args[0])
        self.assertEqual(self.db.query(self.Match).filter_by(chat_id=-10055, status='pending').count(), 1)

    def test_only_participants_can_cancel_and_endmatch_never_charges_them(self):
        row, _ = self.invite()
        self.accept(row)
        asyncio.run(self.H.cancel_super_over(self.update(user=2), self.ctx))
        self.db.expire_all()
        self.assertEqual(row.status, 'playing')
        from handlers.match import endmatch_handler
        update = self.update()
        asyncio.run(endmatch_handler(update, self.ctx))
        self.db.expire_all()
        self.assertEqual(row.status, 'cancelled')
        self.assertEqual((self.users[0].total_coins, self.users[0].total_gems), (4000, 50))
        self.assertNotIn(f'so_{row.id}', self.ctx.bot_data)

    def test_full_game_saves_only_real_super_over_innings_without_competitive_side_effects(self):
        self._check_full_game()

    def test_three_player_game_finishes_without_selecting_batters_again(self):
        self._check_full_game(three_players=True)

    def _check_full_game(self, three_players=False):
        row, _ = self.invite()
        forbidden = [patch('services.match_rewards.award_match_rewards_core'),
                     patch('services.post_match.process_completed_match'),
                     patch('services.player_stats_service.persist_player_game_stats'),
                     patch('services.quest_service.track_user_match_quests'),
                     patch.object(self.SO, '_persist_main_stats')]
        spies = [p.start() for p in forbidden]
        try:
            self.accept(row)
            if three_players:
                so = self.ctx.bot_data[f'so_{row.id}']
                so['main_state']['three_player_squads'] = True
                for team in so['teams'].values():
                    team['xi'] = team['xi'][:3]
                self.SO._reset_selection(so)
            async def play():
                for _ in range(30):
                    so = self.ctx.bot_data.get(f'so_{row.id}')
                    if so is None:
                        return
                    bat, bowl = so['teams'][so['bat_uid']]['tg'], so['teams'][so['bowl_uid']]['tg']
                    if not so['bat_confirmed']:
                        for p in self.SO._eligible_batters(so, so['bat_uid'])[:3]:
                            await self.SO.so_bat_callback(self.query(f"so_bat_{row.id}_{p['roster_id']}", bat), self.ctx)
                        await self.SO.so_batok_callback(self.query(f'so_batok_{row.id}', bat), self.ctx)
                    if not so['bowl_confirmed']:
                        rid = self.SO._eligible_bowlers(so, so['bowl_uid'])[0]['roster_id']
                        await self.SO.so_bowl_callback(self.query(f'so_bowl_{row.id}_{rid}', bowl), self.ctx)
                        await self.SO.so_bowlok_callback(self.query(f'so_bowlok_{row.id}', bowl), self.ctx)
                    await self.SO.so_deliv_callback(self.query(f'so_dv_{row.id}_0', bowl), self.ctx)
                    if so['inn']['stage'] == 'LEN':
                        await self.SO.so_len_callback(self.query(f'so_ln_{row.id}_0', bowl), self.ctx)
                    await self.SO.so_shot_callback(self.query(f'so_sh_{row.id}_0', bat), self.ctx)
                self.fail('Standalone Super Over did not finish')
            with patch.object(self.SO, '_super_over_ball', side_effect=lambda *a, **k: dict(type='run', runs=int(k.get('target') is not None))):
                asyncio.run(play())
            self.db.expire_all()
            self.assertEqual(row.status, 'completed')
            self.assertIn(row.winner_id, [u.id for u in self.users[:2]])
            saved = self.db.query(self.Card).filter_by(match_id=row.id).one()
            payload = json.loads(saved.scorecard_json)
            self.assertTrue(payload['unranked'])
            self.assertEqual([i['runs'] for i in payload['innings']], [0, 1])
            self.assertEqual(len(payload['innings']), 2)
            for user in self.users[:2]:
                self.assertEqual((user.total_coins, user.total_gems, user.matches_played, user.matches_won, user.win_streak),
                                 (4000, 50, 0, 0, 0))
            for spy in spies: spy.assert_not_called()
        finally:
            for p in reversed(forbidden): p.stop()

"""League practice needs exactly three players, without normal XI rules."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class ThreePlayerSquadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from handlers import challenge, super_over_squad
        self.C, self.S = challenge, super_over_squad
        self.draft = dict(draft_id=42, chat_id=-10055, standalone_super_over=True,
            turn='complete', host_tg_id=111, target_tg_id=222,
            host=dict(tg_id=111, name='Host'), target=dict(tg_id=222, name='Guest'),
            host_team='Home', target_team='Away', toss_decision='bat', toss_winner_side='host')
        self.ctx = SimpleNamespace(bot_data={challenge._challenge_team_draft_key(42): self.draft},
            job_queue=None, bot=SimpleNamespace(send_message=AsyncMock(
                return_value=SimpleNamespace(message_id=10)), delete_message=AsyncMock()))
        self.players = [SimpleNamespace(id=i, name=f'Player {i}') for i in range(1, 5)]
        p = patch.object(challenge, '_load_team_players_with_retry', return_value=(self.players, 1, {}))
        p.start()
        self.addCleanup(p.stop)

    async def tap(self, action, side='host', pid=None, tg=None):
        q = SimpleNamespace(data=f'sos_{action}_42_{side}' + (f'_{pid}' if pid else ''),
            from_user=SimpleNamespace(id=tg or self.draft[f'{side}_tg_id']),
            message=SimpleNamespace(chat_id=-10055), answer=AsyncMock(), edit_message_text=AsyncMock())
        await self.S.super_over_squad_callback(SimpleNamespace(callback_query=q), self.ctx)
        return q

    async def test_toss_opens_three_player_pickers_without_xi(self):
        with patch.object(self.C, '_send_challenge_xi_prompt', new=AsyncMock()) as xi:
            await self.C._after_toss_decided(self.ctx, self.draft, 42, None, 'bat')
        xi.assert_not_awaited()
        self.assertEqual(self.ctx.bot.send_message.await_count, 2)
        self.assertEqual(self.draft['sel_phase'], 'players')
        for call in self.ctx.bot.send_message.call_args_list:
            self.assertIn('SELECT 3 PLAYERS', call.args[1])

    async def test_only_three_allowed_and_both_confirm_before_launch(self):
        from handlers import cipl_play
        with patch.object(cipl_play, 'start_match_launch', new=AsyncMock()) as launch:
            rejected = await self.tap('ok')
            self.assertIn('exactly three', rejected.answer.call_args.args[0])
            rejected = await self.tap('pick', pid=1, tg=222)
            self.assertIn('captain', rejected.answer.call_args.args[0])
            for side in ('host', 'target'):
                for pid in (3, 1, 2):
                    await self.tap('pick', side, pid)
                rejected = await self.tap('pick', side, 4)
                self.assertIn('only three', rejected.answer.call_args.args[0])
                await self.tap('ok', side)
                self.assertEqual(self.draft['xi_selections'][side]['player_ids'], [3, 1, 2])
                if side == 'host':
                    launch.assert_not_awaited()
            launch.assert_awaited_once()
            self.assertEqual(self.draft['sel_awaiting'], [])

    async def test_unavailable_players_cannot_be_confirmed(self):
        for pid in (1, 2, 3):
            await self.tap('pick', pid=pid)
        self.players.pop(2)
        rejected = await self.tap('ok')
        self.assertIn('exactly three', rejected.answer.call_args.args[0])
        self.assertFalse(self.draft['xi_selections']['host']['confirmed'])

    async def test_both_captains_can_cancel_without_launch(self):
        for side in ('host', 'target'):
            self.ctx.bot_data[self.C._challenge_team_draft_key(42)] = self.draft
            await self.tap('cancel', side)
            self.assertNotIn(self.C._challenge_team_draft_key(42), self.ctx.bot_data)

    async def test_three_player_teams_can_start_and_smaller_teams_are_refused(self):
        self.players.pop()
        await self.S.send_squad_pickers(self.ctx, self.draft, None)
        self.assertEqual(self.ctx.bot.send_message.await_count, 2)
        self.players.pop()
        await self.S.send_squad_pickers(self.ctx, self.draft, None)
        self.assertNotIn(self.C._challenge_team_draft_key(42), self.ctx.bot_data)
        self.assertIn('at least three', self.ctx.bot.send_message.call_args.args[1])

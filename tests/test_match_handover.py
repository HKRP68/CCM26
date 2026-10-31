"""Tournament handovers keep the game intact and change only its controller."""
import asyncio
import copy
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _tour_fixture as F


def setUpModule(): F.setup_module_db()
def tearDownModule(): F.teardown_module_db()


class HandoverTests(F.FourTeamCase):
    def setUp(self):
        super().setUp()
        from models import Match, MatchState, User
        from services import match_handover as svc
        self.svc = svc
        self.owner = self.owners['Alpha']
        self.opponent = self.owners['Bravo']
        self.receiver = User(telegram_id=F.next_tg(), first_name='<Co-owner>')
        self.session.add(self.receiver)
        self.session.flush()
        self.ts.set_co_owners(self.session, self.teams['Alpha'].id, [self.receiver.telegram_id])
        self.match = Match(user1_id=self.owner.id, user2_id=self.opponent.id,
                           tournament_id=self.tour.id, status='active', chat_id=-100)
        self.session.add(self.match)
        self.session.flush()
        self.state = dict(mode='cipl_approach', match_id=self.match.id,
                          chat_id=-100, tournament_id=self.tour.id,
                          bat_team_id=self.owner.id, bowl_team_id=self.opponent.id,
                          bat_user_tg=self.owner.telegram_id,
                          bowl_user_tg=self.opponent.telegram_id,
                          bat_team_name='Alpha', bowl_team_name='Bravo',
                          innings=1, current_over=7, total_runs=65, total_wickets=2,
                          bat_xi=[{'roster_id': 11}], bowl_xi=[{'roster_id': 22}],
                          batting_approach='balanced', bowling_approach='mixed',
                          bat_stats={'11': {'runs': 36, 'balls': 20}},
                          xi_rules={'by_user': {str(self.owner.telegram_id): {'max_overseas': 4}}},
                          user_names={str(self.owner.telegram_id): 'Owner'}, _rev=1)
        self.ms = MatchState(match_id=self.match.id, state_json=json.dumps(self.state),
                             next_action='PICK_BAT_APPROACH', version=1, ball_seq=38)
        self.session.add(self.ms)
        self.session.commit()

    def offer(self, state=None, **kw):
        return self.svc.offer(self.session, state if state is not None else self.state,
                              self.owner.telegram_id, self.receiver.telegram_id, **kw)

    def accept(self, req, state=None, **kw):
        return self.svc.answer(self.session, state if state is not None else self.state,
                               self.receiver.telegram_id, req['token'], 'accept', **kw)

    def test_accept_preserves_score_squad_stats_and_match_identities(self):
        original = copy.deepcopy(self.state)
        req = self.offer()
        self.assertEqual(self.state['bat_user_tg'], self.owner.telegram_id)
        self.accept(req)
        changed = {'captain_offers', 'captain_controllers', 'captain_handovers',
                   'bat_user_tg', 'user_names', 'xi_rules'}
        self.assertEqual({k: v for k, v in self.state.items() if k not in changed},
                         {k: v for k, v in original.items() if k not in changed})
        self.assertEqual(self.state['bat_user_tg'], self.receiver.telegram_id)
        self.assertEqual(self.svc.seat_for_user(self.state, self.receiver.id), self.owner.id)
        self.assertIsNone(self.svc.seat_for_user(self.state, self.owner.id))
        self.assertEqual(self.match.user1_id, self.owner.id)
        self.assertEqual(self.state['captain_handovers'][0]['over'], 7)
        self.assertEqual(self.state['xi_rules']['by_user'][str(self.receiver.telegram_id)],
                         {'max_overseas': 4})

    def test_only_current_owner_can_offer_only_same_team_manager_can_receive(self):
        for sender, target in ((self.opponent.telegram_id, self.receiver.telegram_id),
                               (self.receiver.telegram_id, self.owner.telegram_id),
                               (self.owner.telegram_id, self.opponent.telegram_id),
                               (self.owner.telegram_id, self.owner.telegram_id)):
            with self.subTest(sender=sender, target=target), self.assertRaises(ValueError):
                self.svc.offer(self.session, self.state, sender, target)

    def test_unauthorised_accept_leaves_request_and_control_untouched(self):
        req = self.offer()
        before = copy.deepcopy(self.state)
        with self.assertRaises(ValueError):
            self.svc.answer(self.session, self.state, self.opponent.telegram_id,
                            req['token'], 'accept')
        self.assertEqual(self.state, before)

    def test_expiry_decline_cancel_and_duplicate_accept(self):
        req = self.offer(now=10)
        with self.assertRaises(ValueError): self.accept(req, now=70)
        req = self.offer(now=71)
        with self.assertRaises(ValueError): self.offer(now=72)
        self.svc.answer(self.session, self.state, self.receiver.telegram_id,
                        req['token'], 'decline', now=73)
        self.assertEqual(self.state['bat_user_tg'], self.owner.telegram_id)
        req = self.offer(now=74)
        self.svc.answer(self.session, self.state, self.owner.telegram_id,
                        req['token'], 'cancel', now=75)
        req = self.offer(now=76)
        self.accept(req, now=77)
        with self.assertRaises(ValueError): self.accept(req, now=78)

    def test_membership_is_rechecked_when_receiver_accepts(self):
        req = self.offer()
        self.ts.set_co_owners(self.session, self.teams['Alpha'].id, [])
        with self.assertRaises(ValueError): self.accept(req)
        self.assertEqual(self.state['bat_user_tg'], self.owner.telegram_id)

    def test_busy_and_banned_receiver_cannot_accept(self):
        from models import Match
        req = self.offer()
        busy = Match(user1_id=self.receiver.id, user2_id=self.opponent.id, status='active')
        self.session.add(busy)
        self.session.flush()
        with self.assertRaises(ValueError): self.accept(req)
        self.session.delete(busy)
        self.session.flush()
        self.receiver.is_banned = True
        with self.assertRaises(ValueError): self.accept(req)

    def test_pending_offer_follows_team_when_innings_swap_and_can_transfer_back(self):
        req = self.offer()
        for suffix in ('team_id', 'user_tg', 'team_name'):
            a, b = f'bat_{suffix}', f'bowl_{suffix}'
            self.state[a], self.state[b] = self.state[b], self.state[a]
        self.state['innings'] = 2
        self.accept(req)
        self.assertEqual(self.state['bowl_user_tg'], self.receiver.telegram_id)
        req2 = self.svc.offer(self.session, self.state, self.receiver.telegram_id,
                              self.owner.telegram_id)
        self.svc.answer(self.session, self.state, self.owner.telegram_id,
                        req2['token'], 'accept')
        self.assertEqual(self.svc.seat_for_user(self.state, self.owner.id), self.owner.id)
        self.assertIsNone(self.svc.seat_for_user(self.state, self.receiver.id))

    def test_webapp_roles_and_turns_follow_current_captain(self):
        from services.match_webapp_service import role_for, whose_turn
        req = self.offer()
        self.accept(req)
        self.assertEqual(role_for(self.state, self.receiver.id), 'batsman')
        self.assertEqual(role_for(self.state, self.owner.id), 'spectator')
        self.assertEqual(whose_turn(self.state, 'PICK_BAT_APPROACH', self.receiver.id),
                         ('batsman', True))

    def test_handlers_persist_restart_safe_handover_without_resetting_clock(self):
        from handlers import match_handover as H, cipl_play as cp
        from services.match_state_store import read_row_snapshot, _deserialize
        ctx = SimpleNamespace(args=[str(self.receiver.telegram_id)],
                              bot_data={}, job_queue=MagicMock())
        msg = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=msg, message=msg,
                                 effective_user=SimpleNamespace(id=self.owner.telegram_id),
                                 effective_chat=SimpleNamespace(id=-100))

        async def workflow():
            with patch.object(cp, '_find_cipl_match_in_chat', AsyncMock(
                    return_value=(self.match.id, self.state))):
                await H.handover_handler(update, ctx)
            self.assertIn('&lt;Co-owner&gt;', msg.reply_text.call_args.args[0])
            data = msg.reply_text.call_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data
            # A restart clears every per-context cache; the request is in the DB.
            ctx.bot_data.clear()
            q = SimpleNamespace(data=data, from_user=SimpleNamespace(id=self.receiver.telegram_id),
                                message=SimpleNamespace(chat=SimpleNamespace(id=-100)),
                                answer=AsyncMock(), edit_message_text=AsyncMock())
            await H.handover_callback(SimpleNamespace(callback_query=q), ctx)
            self.assertEqual(q.answer.call_args.args[0], 'Control transferred.')
            # A duplicate click cannot reassign control or change the audit.
            await H.handover_callback(SimpleNamespace(callback_query=q), ctx)
            self.assertIn('already answered', q.answer.call_args.args[0])

        asyncio.run(workflow())
        stored = _deserialize(read_row_snapshot(self.match.id)['state_json'])
        self.assertEqual(stored['bat_user_tg'], self.receiver.telegram_id)
        self.assertEqual(stored['total_runs'], 65)
        self.assertEqual(len(stored['captain_handovers']), 1)
        self.assertEqual(cp._user_id_for_tg(stored, self.receiver.telegram_id), self.owner.id)
        self.assertIsNone(cp._user_id_for_tg(stored, self.owner.telegram_id))
        ctx.job_queue.run_once.assert_not_called()
        ctx.job_queue.get_jobs_by_name.assert_not_called()
        self.session.expire_all()
        self.assertEqual(self.ms.ball_seq, 38)
        self.assertEqual(self.ms.next_action, 'PICK_BAT_APPROACH')
        self.assertEqual(self.svc.controlled_match(self.session, self.receiver.id).id, self.match.id)

    def test_friendly_and_finished_matches_refuse_handover(self):
        friendly = dict(self.state, tournament_id=None)
        with self.assertRaises(ValueError): self.offer(state=friendly)
        self.match.status = 'completed'
        with self.assertRaises(ValueError): self.offer()

    def test_paused_save_belongs_to_new_captain(self):
        from services import saved_match_service as saved
        req = self.offer()
        self.accept(req)
        row = saved.save_snapshot(self.match.id, self.state, 'PICK_BAT_APPROACH',
                                   saved.REASON_AUTO)
        self.assertEqual(row['user1_id'], self.receiver.id)
        self.assertEqual(row['user1_tg'], self.receiver.telegram_id)
        self.assertIn(self.match.id, [r['match_id'] for r in saved.list_saved(self.receiver.telegram_id)])
        self.assertEqual(set(saved.human_user_ids(self.state)),
                         {self.owner.id, self.receiver.id, self.opponent.id})

    def test_current_buttons_reject_previous_captain_after_acceptance(self):
        from handlers import cipl_play as cp
        req = self.offer()
        self.accept(req)
        for source in ('chat', 'app'):
            state = dict(self.state, play_mode=source, overs=20, current_ball=0)
            ctx = SimpleNamespace(bot_data={}, job_queue=None)

            async def picks():
                with patch.object(cp, '_gs', AsyncMock(return_value=state)), \
                     patch.object(cp, '_get_next_action', AsyncMock(return_value='PICK_BAT_APPROACH')), \
                     patch.object(cp, '_ss', AsyncMock()), \
                     patch.object(cp, '_run_over', AsyncMock()) as play:
                    old = await cp.submit_pick(ctx, self.match.id, self.owner.telegram_id,
                                                cp.PICK_BAT_APPROACH, 0, source=source)
                    self.assertFalse(old[0])
                    accepted = await cp.submit_pick(ctx, self.match.id, self.receiver.telegram_id,
                                                     cp.PICK_BAT_APPROACH, 0, source=source)
                    self.assertTrue(accepted[0])
                    play.assert_awaited_once()
            asyncio.run(picks())

    def test_simultaneous_acceptances_do_not_put_receiver_in_two_matches(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from models import Match, MatchState
        from handlers.match_handover import _mutate
        req1 = self.offer()
        other_owner, other_opponent = self.owners['Charlie'], self.owners['Delta']
        self.ts.set_co_owners(self.session, self.teams['Charlie'].id,
                              [self.receiver.telegram_id])
        other = Match(user1_id=other_owner.id, user2_id=other_opponent.id,
                       tournament_id=self.tour.id, status='active', chat_id=-101)
        self.session.add(other)
        self.session.flush()
        state2 = dict(self.state, match_id=other.id, chat_id=-101, captain_offers={},
                       bat_team_id=other_owner.id, bowl_team_id=other_opponent.id,
                       bat_user_tg=other_owner.telegram_id,
                       bowl_user_tg=other_opponent.telegram_id,
                       bat_team_name='Charlie', bowl_team_name='Delta')
        req2 = self.svc.offer(self.session, state2, other_owner.telegram_id,
                              self.receiver.telegram_id)
        self.ms.state_json = json.dumps(self.state)
        self.session.add(MatchState(match_id=other.id, state_json=json.dumps(state2),
                                     next_action='PICK_BAT_APPROACH', version=1))
        self.session.commit()
        tg, barrier = self.receiver.telegram_id, Barrier(2)

        def accept(mid, token):
            barrier.wait(timeout=5)
            return _mutate(SimpleNamespace(bot_data={}), mid,
                           lambda db, s: self.svc.answer(db, s, tg, token, 'accept'))

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(accept, self.match.id, req1['token'])
            b = pool.submit(accept, other.id, req2['token'])
            results = [a.result(timeout=10), b.result(timeout=10)]
        self.assertEqual(sum(r.get('action') == 'accept' for r in results), 1)
        errors = [r.get('error', '') for r in results]
        self.assertTrue(any('another match' in e for e in errors), results)

    def test_rest_actions_accept_receiver_and_reject_previous_captain(self):
        from handlers.match_handover import _mutate
        import admin
        ctx = SimpleNamespace(bot_data={})
        request = _mutate(ctx, self.match.id,
                          lambda db, s: self.svc.offer(db, s, self.owner.telegram_id,
                                                       self.receiver.telegram_id))
        result = _mutate(ctx, self.match.id,
                         lambda db, s: self.svc.answer(db, s, self.receiver.telegram_id,
                                                       request['token'], 'accept'))
        self.assertEqual(result['action'], 'accept')
        user, match, error = admin._match_rest_user_and_match(
            self.session, self.receiver.id, self.match.id)
        self.assertIsNone(error)
        self.assertEqual(user.id, self.receiver.id)
        self.assertEqual(match.id, self.match.id)
        self.assertEqual(admin._match_rest_user_and_match(
            self.session, self.owner.id, self.match.id)[2][1], 403)
        self.assertIsNone(admin._match_rest_user_and_match(
            self.session, self.opponent.id, self.match.id)[2])

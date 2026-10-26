"""Real purse/ledger, two-team snake draft, preview and persisted clock coverage."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_auction_expansion as F

setUpModule = F.setUpModule
tearDownModule = F.tearDownModule


class PreseasonTests(F.ExpansionCase):
    def setUp(self):
        super().setUp()
        from services import auction_preseason as P
        self.P = P

    def draft(self, *, action='pause', seconds=30, minimum=0):
        two = self.season_two()
        two.retention_locked_at = None
        self.session.flush()
        self.A.set_range_rules(self.session, two, [dict(min_rating=0, max_rating=999,
                               min_count=minimum, max_count=3, price_lakh=650)], draft=True)
        self.A.lock_retention(self.session, two, now=F.NOW)
        self.A.bind_chat(self.session, two, -90000-self.tag)
        self.P.set_clock(self.session, two, seconds=seconds, action=action, now=F.NOW)
        self.session.commit()
        return two, self.A.pick_order(self.session, two)

    def test_preview_reads_without_changing_the_purse_or_finalizing(self):
        before = self.mumbai.purse_remaining_lakh
        lot = self.A.retain(self.session, self.one, self.mumbai, self.players[1], 700)
        self.session.commit()
        preview = self.P.retention_preview(self.session, self.one, self.mumbai)
        self.assertEqual([lot.name], [p['name'] for p in preview['players']])
        self.assertEqual(700, preview['spent_lakh'])
        self.assertEqual(before-700, preview['purse_lakh'])
        self.assertFalse(preview['finalized'])
        self.assertFalse(self.session.dirty)

    def test_admin_reopening_one_team_stops_draft_until_it_is_locked_again(self):
        two, _order = self.draft()
        team = next(f for f in self.A.franchises(self.session, two.id) if f.name == 'Mumbai')
        self.assertIsNotNone(two.draft_pick_deadline_at)
        self.A.set_retention_lock(self.session, two, False, team, now=F.NOW)
        self.assertIsNone(two.draft_pick_deadline_at)
        with self.assertRaisesRegex(self.A.AuctionError, 'Mumbai'):
            self.A._pick_window(self.session, two)
        self.A.set_retention_lock(self.session, two, True, team, now=F.NOW)
        self.assertTrue(self.A.retention_locked(two))
        self.assertIsNotNone(two.draft_pick_deadline_at)

    def test_admin_cannot_unlock_retention_for_an_expansion_team(self):
        two, order = self.draft()
        with self.assertRaisesRegex(self.A.AuctionError, 'New teams draft'):
            self.A.set_retention_lock(self.session, two, False, order[0])

    def test_dynamic_retention_can_finalize_empty_despite_minimum(self):
        from services import retention_negotiation as RN
        RN.set_mode(self.session, self.one, 'dynamic')
        self.one.min_retentions = 2
        self.session.flush()
        purse = self.mumbai.purse_remaining_lakh
        preview = self.P.retention_preview(self.session, self.one, self.mumbai)
        self.P.confirm_retention(self.session, self.one, self.mumbai, preview['token'])
        self.session.commit()
        self.assertIsNotNone(self.mumbai.retention_finalized_at)
        self.assertEqual(purse, self.mumbai.purse_remaining_lakh)
        self.assertEqual(0, self.mumbai.retained_count)
        self.assert_ledger_agrees(self.one)

    def test_changed_roster_rejects_preview_token(self):
        preview = self.P.retention_preview(self.session, self.one, self.mumbai)
        self.A.retain(self.session, self.one, self.mumbai, self.players[1], 700)
        self.session.commit()
        with self.assertRaisesRegex(self.A.AuctionError, 'fresh retention preview'):
            self.P.confirm_retention(self.session, self.one, self.mumbai, preview['token'])
        self.session.rollback()
        self.assertIsNone(self.mumbai.retention_finalized_at)

    def test_current_preview_finalizes_and_board_names_only_pending_team(self):
        preview = self.P.retention_preview(self.session, self.one, self.mumbai)
        self.P.confirm_retention(self.session, self.one, self.mumbai, preview['token'])
        self.session.commit()
        board = self.P.readiness(self.session, self.one)
        self.assertEqual(['Chennai'], board['pending'])
        self.assertTrue(next(t for t in board['teams'] if t['name']=='Mumbai')['final'])

    def test_filtered_eligibility_is_read_only_and_uses_fixed_range_price(self):
        two, teams = self.draft()
        revision = two.draft_clock_revision
        purse = teams[0].purse_remaining_lakh
        data = self.P.eligible_players(self.session, two, teams[0], rating='91-96',
                    role='Bowler', country='India', max_cost_lakh=650)
        self.assertEqual(['Jasprit Bumrah'], [p['name'] for p in data['players']])
        self.assertEqual(650, data['players'][0]['price_lakh'])
        self.assertFalse(self.P.eligible_players(self.session, two, teams[0], max_cost_lakh=649)['players'])
        self.assertEqual(revision, two.draft_clock_revision)
        self.assertEqual(purse, teams[0].purse_remaining_lakh)
        self.assertFalse(self.session.dirty)

    def test_eligibility_excludes_taken_overseas_and_unaffordable_players(self):
        two, teams = self.draft()
        taken = self.A.draft_pick(self.session, two, teams[0], self.players[0], now=F.NOW)
        two.max_overseas = 0
        self.session.commit()
        data = self.P.eligible_players(self.session, two, teams[1])
        self.assertNotIn(taken.player_id, [p['player_id'] for p in data['players']])
        self.assertTrue(all(p['country']=='India' for p in data['players']))
        teams[1].purse_remaining_lakh = 1
        self.session.flush()
        self.assertEqual([], self.P.eligible_players(self.session, two, teams[1])['players'])

    def test_pagination_and_bad_filter(self):
        two, teams = self.draft()
        first = self.P.eligible_players(self.session, two, teams[0], page=1, limit=2)
        second = self.P.eligible_players(self.session, two, teams[0], page=2, limit=2)
        self.assertFalse({p['id'] for p in first['players']} & {p['id'] for p in second['players']})
        with self.assertRaisesRegex(self.A.AuctionError, 'rating range'):
            self.P.eligible_players(self.session, two, teams[0], rating='wrong')

    def test_clock_does_not_start_until_retention_closes(self):
        two, teams = self.draft()
        two.retention_locked_at = None
        self.session.flush()
        self.P.sync_clock(self.session, two, now=F.NOW)
        self.assertIsNone(two.draft_pick_deadline_at)
        self.A.lock_retention(self.session, two, now=F.NOW)
        self.assertEqual(F.NOW+timedelta(seconds=30), two.draft_pick_deadline_at)

    def test_restart_does_not_extend_deadline(self):
        from database import get_session
        from models import AuctionSeason
        two, teams = self.draft()
        sid, deadline = two.id, two.draft_pick_deadline_at
        self.session.close()
        self.session = get_session()
        two = self.session.get(AuctionSeason, sid)
        self.P.resolve_timeout(self.session, two, now=F.NOW+timedelta(seconds=10))
        self.assertEqual(deadline, two.draft_pick_deadline_at)
        self.assertFalse(two.draft_clock_paused)

    def test_shortlist_skips_a_taken_player_then_charges_once(self):
        two, teams = self.draft(action='shortlist')
        for player in self.players[:2]:
            self.P.edit_shortlist(self.session, two, teams[1], 'add', player)
        self.A.draft_pick(self.session, two, teams[0], self.players[0], now=F.NOW)
        self.session.commit()
        lot = self.P.resolve_timeout(self.session, two, now=F.NOW+timedelta(seconds=31))
        self.assertEqual(self.players[1].id, lot.player_id)
        self.assertEqual(teams[1].id, lot.sold_to_id)
        self.assertEqual(650, lot.sold_price_lakh)
        self.session.commit()
        self.assertIsNone(self.P.resolve_timeout(self.session, two, now=F.NOW+timedelta(seconds=31)))
        self.session.commit()
        self.assertEqual(1, teams[1].draft_picks_used)
        self.assert_ledger_agrees(two)

    def test_empty_shortlist_pauses_once_without_spending_pick(self):
        two, teams = self.draft(action='shortlist')
        self.assertEqual('paused', self.P.resolve_timeout(self.session, two, now=F.NOW+timedelta(seconds=31)))
        self.session.commit()
        self.assertEqual(0, teams[0].draft_picks_used)
        self.assertIsNone(self.P.resolve_timeout(self.session, two, now=F.NOW+timedelta(seconds=60)))
        from models import AuctionEvent
        self.assertEqual(1, self.session.query(AuctionEvent).filter_by(season_id=two.id, kind='draft_clock_paused').count())

    def test_skip_action_advances_exactly_one_turn(self):
        two, teams = self.draft(action='skip')
        self.P.resolve_timeout(self.session, two, now=F.NOW+timedelta(seconds=31))
        self.session.commit()
        self.assertEqual(1, teams[0].draft_picks_used)
        self.assertEqual(teams[1].id, self.A.pick_turn(self.session, two).id)
        self.assertEqual(F.NOW+timedelta(seconds=61), two.draft_pick_deadline_at)
        self.assertEqual([], self.A.drafted(self.session, teams[0].id))

    def test_minimum_blocks_timeout_skip_and_pauses(self):
        two, teams = self.draft(action='skip', minimum=3)
        self.assertEqual('paused', self.P.resolve_timeout(self.session, two, now=F.NOW+timedelta(seconds=31)))
        self.assertEqual(0, teams[0].draft_picks_used)
        self.assertTrue(two.draft_clock_paused)

    def test_pause_resume_and_off(self):
        two, teams = self.draft()
        self.P.set_clock(self.session, two, operation='pause', now=F.NOW)
        self.assertIsNone(self.P.resolve_timeout(self.session, two, now=F.NOW+timedelta(seconds=60)))
        self.P.set_clock(self.session, two, operation='resume', now=F.NOW+timedelta(seconds=61))
        self.assertEqual(F.NOW+timedelta(seconds=91), two.draft_pick_deadline_at)
        self.P.set_clock(self.session, two, operation='off')
        self.assertIsNone(two.draft_pick_deadline_at)
        self.assertEqual(0, teams[0].draft_picks_used)

    def test_stale_worker_cannot_claim_a_manually_spent_turn(self):
        from database import get_session
        from models import AuctionSeason
        two, teams = self.draft(action='shortlist')
        other = get_session()
        try:
            stale = other.get(AuctionSeason, two.id)
            old_revision = stale.draft_clock_revision
            self.A.draft_pick(self.session, two, teams[0], self.players[0], now=F.NOW)
            self.session.commit()
            self.assertNotEqual(old_revision, two.draft_clock_revision)
            with self.assertRaisesRegex(self.A.AuctionError, 'turn changed'):
                self.P.resolve_timeout(other, stale, now=F.NOW+timedelta(seconds=31))
            other.rollback()
            self.assertEqual(1, teams[0].draft_picks_used)
        finally:
            other.close()

    def test_scheduler_resolves_setup_clock_and_announces_only_once(self):
        from services import auction_scheduler as S
        two, teams = self.draft(action='shortlist')
        self.P.edit_shortlist(self.session, two, teams[0], 'add', self.players[0])
        # Clock uses real UTC; give it an already expired persisted deadline.
        two.draft_pick_deadline_at = F.NOW-timedelta(seconds=1)
        self.session.commit()
        with patch.object(S, '_send', new=AsyncMock(return_value=True)) as send, patch.object(S, '_trade_tick', new=AsyncMock()):
            asyncio.run(S._auction_tick(SimpleNamespace(bot=SimpleNamespace())))
            asyncio.run(S._auction_tick(SimpleNamespace(bot=SimpleNamespace())))
            self.assertEqual(1, sum(call.args[1] == two.chat_id for call in send.call_args_list))
        self.session.expire_all()
        self.assertEqual(1, teams[0].draft_picks_used)
        self.assertTrue(two.draft_announced_event_id)
        self.assert_ledger_agrees(two)
    def test_shortlist_rechecks_changed_role_rules_before_automatic_pick(self):
        two, teams = self.draft(action='shortlist')
        for player in self.players[:2]:
            self.P.edit_shortlist(self.session, two, teams[0], 'add', player)
        two.role_maximums_json = '{"Batsman": 0}'
        self.session.commit()
        lot = self.P.resolve_timeout(self.session, two, now=F.NOW+timedelta(seconds=31))
        self.assertEqual(self.players[1].id, lot.player_id)
        self.session.commit()
        self.assert_ledger_agrees(two)

    def test_rating_reachability_filters_match_real_pick_rules(self):
        two, teams = self.draft()
        two.max_squad_size = 2
        two.rating_rules_json = '[{"max_rating":80,"min_players":2}]'
        self.session.flush()
        players = self.P.eligible_players(self.session, two, teams[0])['players']
        self.assertEqual(['Mukesh Kumar'], [p['name'] for p in players])
        lot = self.A.draft_pick(self.session, two, teams[0], self.players[7], now=F.NOW)
        self.assertEqual('Mukesh Kumar', lot.name)

    def test_stale_pick_rechecks_phase_after_claiming_turn(self):
        from database import get_session
        from models import AuctionSeason, AuctionFranchise, Player
        two, teams = self.draft()
        other = get_session()
        try:
            stale = other.get(AuctionSeason, two.id)
            team = other.get(AuctionFranchise, teams[0].id)
            player = other.get(Player, self.players[0].id)
            self.assertEqual(self.A.STATUS_SETUP, stale.status)
            two.status = self.A.STATUS_LIVE
            self.session.commit()
            with self.assertRaisesRegex(self.A.AuctionError, 'before the auction opens'):
                self.A.draft_pick(other, stale, team, player)
            other.rollback()
            self.session.expire_all()
            self.assertEqual(0, teams[0].draft_picks_used)
            self.assert_ledger_agrees(two)
        finally:
            other.close()

    def test_setup_notice_is_not_replayed_by_live_event_drain(self):
        from services import auction_scheduler as S
        event = self.A.log_event(self.session, self.one, 'drafted', 'Drafted once')
        self.session.flush()
        self.one.draft_announced_event_id = event.id
        self.one.announced_event_id = event.id-1
        self.session.commit()
        with patch.object(S, '_send', new=AsyncMock(return_value=True)) as send:
            asyncio.run(S.drain_events(SimpleNamespace(), self.session, self.one))
            send.assert_not_awaited()
        self.assertEqual(event.id, self.one.announced_event_id)


class PreseasonCommandTests(F.ExpansionCase):
    def setUp(self):
        super().setUp()
        from handlers import auction as H
        manager_gate = patch.object(H, '_is_auction_admin', return_value=True)
        manager_gate.start()
        self.addCleanup(manager_gate.stop)

    def callback(self, user_id, token):
        from handlers import auction as H
        self.session.commit()
        query = SimpleNamespace(data=f'au_rfinal_{self.one.id}_{self.mumbai.id}_{token}',
                                answer=AsyncMock(), edit_message_text=AsyncMock())
        update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=self.one.chat_id, type='supergroup'),
                                 effective_user=SimpleNamespace(id=user_id))
        asyncio.run(H.retention_final_callback(update, SimpleNamespace()))
        self.session.expire_all()
        return query

    def test_preview_command_does_not_finalize_and_supplies_confirmation(self):
        from handlers import auction as H
        self.session.commit()
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=self.one.chat_id, type='supergroup'),
                                 effective_user=SimpleNamespace(id=F.ALICE))
        with patch.object(H, '_reply', new=AsyncMock()) as reply:
            asyncio.run(H.retainlock_handler(update, SimpleNamespace(args=[])))
            self.assertIn('retention preview', reply.call_args.args[1])
            self.assertIn('Remaining purse', reply.call_args.args[1])
            self.assertTrue(reply.call_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data.startswith('au_rfinal_'))
            self.assertEqual('Enter auction without retentions', reply.call_args.kwargs['reply_markup'].inline_keyboard[0][0].text)
        self.session.expire_all()
        self.assertIsNone(self.mumbai.retention_finalized_at)

    def test_stranger_cannot_confirm_another_teams_preview(self):
        from services import auction_preseason as P
        token = P.retention_preview(self.session, self.one, self.mumbai)['token']
        query = self.callback(F.CAROL, token)
        self.assertIn("Only Mumbai's owner or a co-owner", query.answer.call_args.args[0])
        self.assertIsNone(self.mumbai.retention_finalized_at)
        query.edit_message_text.assert_not_awaited()

    def test_owner_can_confirm_current_preview(self):
        from services import auction_preseason as P
        token = P.retention_preview(self.session, self.one, self.mumbai)['token']
        query = self.callback(F.ALICE, token)
        self.assertIsNotNone(self.mumbai.retention_finalized_at)
        query.edit_message_text.assert_awaited_once()

    def test_owner_is_told_to_refresh_stale_preview(self):
        from services import auction_preseason as P
        token = P.retention_preview(self.session, self.one, self.mumbai)['token']
        self.A.retain(self.session, self.one, self.mumbai, self.players[1], 700)
        query = self.callback(F.ALICE, token)
        self.assertIn('fresh retention preview', query.answer.call_args.args[0])
        self.assertIsNone(self.mumbai.retention_finalized_at)

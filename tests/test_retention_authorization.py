"""Retention ownership survives league rollover without granting cross-team access."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests import test_auction_expansion as F

setUpModule = F.setUpModule
tearDownModule = F.tearDownModule


class RetentionAuthorizationTests(F.ExpansionCase):
    def setUp(self):
        super().setUp()
        self.mumbai.co_owner_ids_json = json.dumps([501])
        self.session.commit()
        league = self.publish_one()
        self.two = self.A.season_from_league(self.session, league,
                                            f"Next {self.tag}", template=self.one)
        self.A.bind_chat(self.session, self.two, -90000 - self.tag)
        self.team = next(f for f in self.A.franchises(self.session, self.two.id)
                         if f.name == 'Mumbai')
        squad = self.A.previous_squad_map(self.session, self.two)
        self.player = next(p for p in self.players if squad[p.id].id == self.team.id)
        self.other_player = next(p for p in self.players if squad[p.id].id != self.team.id)
        self.session.commit()

    def offer(self, player=None):
        # Reproduce a pending card created before the ownership fix.
        from models import AuctionRetentionOffer
        row = AuctionRetentionOffer(season_id=self.two.id, franchise_id=self.team.id,
                                    player_id=(player or self.player).id,
                                    player_name=(player or self.player).name,
                                    price_lakh=300, status=self.A.OFFER_PENDING,
                                    chat_id=self.two.chat_id)
        self.session.add(row)
        self.session.commit()
        return row

    def callback(self, row, actor, answer='yes', *, effective_actor=None, wrong_chat=False):
        from handlers import auction as H
        query = SimpleNamespace(data=f'au_ret_{row.id}_{answer}',
                                from_user=SimpleNamespace(id=actor),
                                answer=AsyncMock(), edit_message_text=AsyncMock())
        update = SimpleNamespace(callback_query=query,
            effective_user=SimpleNamespace(id=effective_actor or actor),
            effective_chat=SimpleNamespace(id=self.two.chat_id + int(wrong_chat),
                                           type='supergroup'))
        asyncio.run(H.retention_offer_callback(update, SimpleNamespace()))
        self.session.expire_all()
        return query

    def test_inherited_owner_can_accept_existing_card(self):
        row = self.offer()
        self.assertIsNone(self.team.owner_tg_id)
        query = self.callback(row, F.ALICE, effective_actor=F.BOB)
        self.assertEqual(self.A.OFFER_ACCEPTED, row.status)
        self.assertEqual(F.ALICE, self.team.owner_tg_id)
        self.assertEqual(1, self.team.retained_count)
        query.edit_message_text.assert_awaited_once()

    def test_inherited_coowner_can_decline_existing_card(self):
        row = self.offer()
        self.callback(row, 501, 'no')
        self.assertEqual(self.A.OFFER_DECLINED, row.status)
        self.assertEqual(501, row.answered_by_tg_id)
        self.assertEqual([], self.A.retained(self.session, self.team.id))

    def test_inherited_coowner_can_accept_existing_card(self):
        row = self.offer()
        self.callback(row, 501)
        self.assertEqual(self.A.OFFER_ACCEPTED, row.status)

    def test_inherited_owner_can_find_team_with_retain_command(self):
        from handlers import auction as H
        self.offer()
        reply = AsyncMock()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=F.ALICE),
            effective_chat=SimpleNamespace(id=self.two.chat_id, type='supergroup'),
            effective_message=SimpleNamespace(reply_text=reply))
        asyncio.run(H.retain_handler(update, SimpleNamespace(args=[])))
        texts = [call.args[0] for call in reply.call_args_list]
        self.assertTrue(any('Retention offer' in text for text in texts), texts)
        self.assertTrue(any("last season's squad" in text for text in texts), texts)

    def test_other_team_owner_and_admin_cannot_answer_either_button(self):
        row = self.offer()
        for actor in (F.BOB, F.CAROL):
            for answer in ('yes', 'no'):
                with self.subTest(actor=actor, answer=answer):
                    query = self.callback(row, actor, answer, effective_actor=F.ALICE)
                    self.assertIn("Only Mumbai's owner", query.answer.call_args.args[0])
                    self.assertEqual(self.A.OFFER_PENDING, row.status)
                    query.edit_message_text.assert_not_awaited()
        self.assertEqual([], self.A.retained(self.session, self.team.id))

    def test_assigned_new_owner_prevents_previous_owner_or_coowner_inheritance(self):
        self.team.owner_tg_id = 777
        self.session.commit()
        row = self.offer()
        for actor in (F.ALICE, 501):
            self.callback(row, actor)
            self.assertEqual(self.A.OFFER_PENDING, row.status)
        self.callback(row, 777)
        self.assertEqual(self.A.OFFER_ACCEPTED, row.status)
        self.assertEqual(777, self.team.owner_tg_id)
        self.assertNotIn(501, self.A.co_owner_ids(self.team))

    def test_explicit_empty_coowners_does_not_restore_revoked_access(self):
        self.team.owner_tg_id = F.ALICE
        self.team.co_owner_ids_json = '[]'
        self.session.commit()
        row = self.offer()
        self.callback(row, 501)
        self.assertEqual(self.A.OFFER_PENDING, row.status)
        self.assertEqual([], self.A.co_owner_ids(self.team))
        self.callback(row, F.ALICE)
        self.assertEqual(self.A.OFFER_ACCEPTED, row.status)

    def test_latest_linked_tournament_ownership_takes_precedence(self):
        from models import Tournament, TournamentTeam
        tournament = Tournament(name=f'Played {self.tag}',
                                league_id=self.two.previous_league_id)
        self.session.add(tournament)
        self.session.flush()
        self.session.add(TournamentTeam(tournament_id=tournament.id,
            challenge_team_id=self.team.previous_team_id, name='Mumbai',
            owner_tg_id=777, co_owner_ids_json='[888]'))
        self.session.commit()
        row = self.offer()
        self.callback(row, F.ALICE)
        self.assertEqual(self.A.OFFER_PENDING, row.status)
        self.callback(row, 888)
        self.assertEqual(self.A.OFFER_ACCEPTED, row.status)
        self.assertEqual(777, self.team.owner_tg_id)

    def test_unconfigured_tournament_does_not_hide_published_owners(self):
        from models import Tournament, TournamentTeam
        tournament = Tournament(name=f'Unassigned {self.tag}',
                                league_id=self.two.previous_league_id)
        self.session.add(tournament)
        self.session.flush()
        self.session.add(TournamentTeam(tournament_id=tournament.id,
            challenge_team_id=self.team.previous_team_id, name='Mumbai'))
        self.session.commit()
        row = self.offer()
        self.callback(row, F.ALICE)
        self.assertEqual(self.A.OFFER_ACCEPTED, row.status)

    def test_wrong_team_player_in_old_offer_cannot_be_signed(self):
        row = self.offer(self.other_player)
        query = self.callback(row, F.ALICE)
        self.assertIn("wasn't on Mumbai", query.answer.call_args.args[0])
        self.assertEqual(self.A.OFFER_PENDING, row.status)
        self.assertEqual(0, self.team.retained_count)
        self.assertEqual([], self.A.retained(self.session, self.team.id))

    def test_second_acceptance_does_not_spend_again(self):
        row = self.offer()
        self.callback(row, F.ALICE)
        purse = self.team.purse_remaining_lakh
        query = self.callback(row, 501)
        self.assertIn('already been accepted', query.answer.call_args.args[0])
        self.assertEqual(purse, self.team.purse_remaining_lakh)
        self.assertEqual(1, self.team.retained_count)

    def test_wrong_group_or_invalid_answer_cannot_change_offer(self):
        row = self.offer()
        for answer, wrong in (('yes', True), ('unexpected', False)):
            query = self.callback(row, F.ALICE, answer, wrong_chat=wrong)
            self.assertIn('not valid', query.answer.call_args.args[0])
            self.assertEqual(self.A.OFFER_PENDING, row.status)

    def test_id_normalization_accepts_strings_and_rejects_malformed_values(self):
        franchise = SimpleNamespace(owner_tg_id=' 111 ',
            co_owner_ids_json='[" 501 ", "501", true, 222.5, null, "bad", -333]')
        self.assertTrue(self.A.may_bid_for(franchise, F.ALICE))
        self.assertTrue(self.A.may_bid_for(franchise, 501))
        self.assertEqual([501], self.A.co_owner_ids(franchise))
        self.assertEqual([111, 501], self.A.owner_ids(franchise))
        for actor in (True, 222, 222.5, -333, None, 'bad'):
            self.assertFalse(self.A.may_bid_for(franchise, actor))
        for malformed in ('{"501": true}', 'null', 'broken', '"501"'):
            franchise.co_owner_ids_json = malformed
            self.assertFalse(self.A.may_bid_for(franchise, 501))

"""Tournament-only access survives alternate commands, buttons and text input."""

import asyncio
import sys
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from telegram.ext import ApplicationHandlerStop

from database import Base
from models import Bowlout, CLTour, Match, Tour, User
from services import tournament_only_guard as gate


def update(text=None, data=None, tg=1001, reply=None):
    message = NS(text=text, entities=[], web_app_data=None,
                 reply_to_message=NS(from_user=NS(id=reply)) if reply else None,
                 reply_text=AsyncMock())
    return NS(effective_message=message, effective_user=NS(id=tg),
              effective_chat=NS(id=-10),
              callback_query=NS(data=data, answer=AsyncMock()) if data else None)


class ActionClassificationTests(unittest.TestCase):
    def test_shared_lp_buttons_use_server_draft(self):
        official = {"lpt": {"tournament_id": 9}}
        context = NS(bot_data={"lp_7": official, "lp_8": {"lpt": None}})
        for data in ("lp_accept_7", "lp_pitch_7_flat", "lp_coin_heads_7",
                     "lp_toss_bat_7_host", "lp_startmatch_7"):
            self.assertTrue(gate._callback_action(data, context, -10).tournament, data)
        self.assertFalse(gate._callback_action("lp_accept_8", context, -10).tournament)
        self.assertFalse(gate._callback_action("lp_accept_9", context, -10).tournament)

    def test_shared_challenge_buttons_use_server_draft(self):
        context = NS(bot_data={"challenge_team_draft_7": {
            "is_tournament": True, "tournament_id": 9}})
        for data in ("cl_team_7_0", "cl_start_7", "cipl_coin_heads_7",
                     "cipl_toss_bat_7_host"):
            self.assertTrue(gate._callback_action(data, context, -10).tournament, data)
        self.assertFalse(gate._callback_action("cl_team_9_0", context, -10).tournament)

    def test_impact_owner_tag_cannot_be_used_as_match_id(self):
        action = gate._callback_action("cipl_impo_u11_7_9", NS(bot_data={}), -10)
        self.assertEqual(action.match_id, 7)

    def test_bowlout_ids_are_not_match_ids(self):
        context = NS(bot_data={})
        invite = gate._callback_action("pboacc_7_8_-10", context, -10)
        self.assertIsNone(invite.match_id)
        self.assertEqual(invite.record, {"user1_id": 7, "user2_id": 8})
        pick = gate._callback_action("bopick_7_20", context, -10)
        self.assertIsNone(pick.match_id)
        self.assertEqual(pick.bowlout_id, 7)

    def test_draft_and_standalone_superover_buttons_resolve_other_participant(self):
        draft = {"host_user_id": 17, "target_user_id": 18}
        context = NS(bot_data={"challenge_team_draft_7": draft})
        for data in ("cdj_7", "cdp_7_1_20", "sos_pick_7_host_20", "sos_ok_7_host"):
            self.assertEqual(gate._callback_action(data, context, -10).record, draft, data)
        self.assertEqual(gate._callback_action("sop_accept_7", context, -10).match_id, 7)

    def test_read_only_and_cleanup_callbacks_are_open(self):
        for data in ("svm_list", "svm_delok_7", "ho_decline_7_abcd", "lp_deny_7",
                     "cric_cancel_lobby", "cl_cancel_7", "ct_cancel_-10",
                     "market_buy_7", "ctv_table_9", "al:table:7"):
            self.assertIsNone(gate._callback_action(data, NS(bot_data={}), -10), data)

    def test_ipl_start_link_is_gameplay(self):
        self.assertEqual(gate._command(update("/start ipl160")), "ipl160")
        self.assertEqual(gate._command(update("/s ipl160")), "ipl160")
        self.assertIsNone(gate._command(update("/wpm@OtherBot"), "ThisBot"))

    def test_fantasy_telegram_webapp_submission_is_gameplay(self):
        incoming = update()
        incoming.effective_message.web_app_data = NS(data='{"type":"fantasy_picks"}')
        self.assertIsNotNone(gate._text_action(incoming, NS(bot_data={})))

    def test_plain_text_only_classifies_live_gameplay(self):
        context = NS(bot_data={"awaiting_overs_1001": 7})
        self.assertEqual(gate._text_action(update("10"), context).match_id, 7)
        self.assertEqual(gate._text_action(update("10 overs"), context).match_id, 7)
        self.assertIsNone(gate._text_action(update("hello"), context))
        self.assertIsNone(gate._text_action(update("99"), context))

    def test_typed_xi_in_official_tournament_stays_open(self):
        context = NS(bot_data={"cl_draft_chat_-10": 7,
                               "challenge_team_draft_7": {
                                   "host_tg_id": 1001, "is_tournament": True,
                                   "turn": "complete", "toss_decision": "bat",
                                   "xi_started": {"host": True},
                                   "tournament_id": 9}})
        self.assertTrue(gate._text_action(update("1, 2, 3"), context).tournament)

    def test_social_game_guesses_are_classified(self):
        wc = NS(GAMES={-10: {"host_id": 99, "guessing_enabled": True}})
        with patch.dict(sys.modules, {"handlers.wordchase": wc}):
            self.assertIsNotNone(gate._text_action(update("guess"), NS(bot_data={})))


class StoredAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(cls.engine)
        cls.factory = sessionmaker(bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.session = self.factory()
        self.actor = User(telegram_id=1001, username="actor", tournament_only=False)
        self.other = User(telegram_id=1002, username="opponent", tournament_only=True)
        self.session.add_all((self.actor, self.other))
        self.session.flush()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def match(self, official=False):
        match = Match(user1_id=self.actor.id, user2_id=self.other.id,
                      tournament_id=9 if official else None, chat_id=-10,
                      status="active")
        self.session.add(match)
        self.session.flush()
        return match

    def test_inviting_restricted_reply_target_is_refused(self):
        denied = gate._check_action(self.session, gate.Action(), update("/sim", reply=1002))
        self.assertIn("participant", denied)

    def test_inviting_restricted_username_target_is_refused(self):
        denied = gate._check_action(self.session, gate.Action(), update("/wpm @OPPONENT"))
        self.assertIn("participant", denied)

    def test_unrestricted_actor_cannot_advance_restricted_opponent(self):
        match = self.match()
        self.assertIsNotNone(gate._check_action(
            self.session, gate.Action(match_id=match.id), update(data=f"bshot_{match.id}_0")))

    def test_restricted_player_can_advance_official_match(self):
        match = self.match(official=True)
        self.assertIsNone(gate._check_action(
            self.session, gate.Action(match_id=match.id), update(data=f"cipl_bowler_{match.id}_0", tg=1002)))

    def test_bowlout_other_participant_checked_without_match_collision(self):
        official = self.match(official=True)
        bo = Bowlout(id=official.id, user1_id=self.actor.id, user2_id=self.other.id)
        self.session.add(bo)
        self.session.flush()
        self.assertIsNotNone(gate._check_action(
            self.session, gate.Action(bowlout_id=bo.id), update(data=f"bopick_{bo.id}_1")))
        bo.match_id = official.id
        self.assertIsNone(gate._check_action(
            self.session, gate.Action(bowlout_id=bo.id), update(data=f"bopick_{bo.id}_1")))

    def test_both_users_in_server_lobby_are_checked(self):
        action = gate.Action(record={"host_user_id": self.other.id})
        self.assertIsNotNone(gate._check_action(self.session, action, update(data="cric_join")))

    def test_old_tour_play_buttons_check_both_users(self):
        old = Tour(user1_id=self.actor.id, user2_id=self.other.id,
                   match_count=3, overs_per_match=5)
        league = CLTour(user1_id=self.actor.id, user2_id=self.other.id, match_count=3)
        self.session.add_all((old, league))
        self.session.flush()
        for data in (f"mtp_{old.id}_1_1001", f"tac_{old.id}_1",
                     f"cltplay_{league.id}_1_1001", f"clt_acc_{league.id}"):
            action = gate._callback_action(data, NS(bot_data={}), -10)
            self.assertIsNotNone(gate._check_action(self.session, action, update(data=data)), data)

    def test_change_roster_number_does_not_grant_unrelated_match_exemption(self):
        official = self.match(official=True)
        context = NS(bot_data={"lp_1": {"chat_id": -10,
                                       "status": "showxi",
                                       "host": {"tg_id": 1001}, "lpt": None}})
        action = gate._continuation_action(self.session, update(f"/change {official.id} 12"), context, "change")
        self.assertIsNone(action.match_id)
        self.assertFalse(action.tournament)

    def test_impact_ignores_numeric_argument_and_resolves_live_match(self):
        official = self.match(official=True)
        casual = self.match()
        context = NS(bot_data={f"ms_{casual.id}": {
            "mode": "cipl_approach", "chat_id": -10}})
        action = gate._continuation_action(self.session, update(f"/impact {official.id}"), context, "impact")
        self.assertEqual(action.match_id, casual.id)


class MiddlewareTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        return NS(bot_data={}, bot=NS(username="TestBot"),
                  application=NS(handlers={0: [NS(commands={"claim", "start"})]}))

    async def test_denial_stops_all_handler_groups(self):
        incoming = update("/wpmbot")
        with patch.object(gate, "_decision", return_value="Tournament-only access"):
            with self.assertRaises(ApplicationHandlerStop):
                await gate.tournament_only_guard(incoming, self.context())
        incoming.effective_message.reply_text.assert_awaited_once()

    async def test_callback_denial_is_alert(self):
        incoming = update(data="cric_join")
        with patch.object(gate, "_decision", return_value="Tournament-only access"):
            with self.assertRaises(ApplicationHandlerStop):
                await gate.tournament_only_guard(incoming, self.context())
        self.assertTrue(incoming.callback_query.answer.call_args.kwargs["show_alert"])

    async def test_unrelated_commands_do_no_database_work(self):
        with patch.object(gate, "_decision") as decision:
            await gate.tournament_only_guard(update("/claim"), self.context())
            await gate.tournament_only_guard(update("hello"), self.context())
            await gate.tournament_only_guard(update(data="svm_del_7"), self.context())
        decision.assert_not_called()

    async def test_database_failure_does_not_allow_gameplay(self):
        with patch.object(gate, "_decision", side_effect=RuntimeError("offline")):
            with self.assertRaises(ApplicationHandlerStop):
                await gate.tournament_only_guard(update("/sim"), self.context())


if __name__ == "__main__":
    unittest.main()

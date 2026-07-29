import unittest

from services import button_access


class DummyUser:
    def __init__(self, user_id):
        self.id = user_id


class DummyChat:
    def __init__(self, chat_id):
        self.id = chat_id


class DummyMessage:
    def __init__(self, chat_id, message_id):
        self.chat_id = chat_id
        self.chat = DummyChat(chat_id)
        self.message_id = message_id


class DummyQuery:
    def __init__(self, user_id, data, chat_id=100, message_id=200):
        self.from_user = DummyUser(user_id)
        self.data = data
        self.message = DummyMessage(chat_id, message_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class DummyUpdate:
    def __init__(self, query):
        self.callback_query = query


class ButtonAccessTests(unittest.TestCase):
    def tearDown(self):
        button_access._OWNER_BY_MESSAGE.clear()

    def test_registered_owner_can_use_personal_button(self):
        button_access.register_button_owner(100, 200, 111)
        update = DummyUpdate(DummyQuery(111, "roster_page_111_2"))

        self.assertTrue(button_access.check_callback_owner(update))

    def test_other_user_is_blocked_from_personal_button(self):
        button_access.register_button_owner(100, 200, 111)
        update = DummyUpdate(DummyQuery(222, "roster_page_111_2"))

        self.assertFalse(button_access.check_callback_owner(update))

    def test_shared_button_ignores_registered_owner(self):
        button_access.register_button_owner(100, 200, 111)
        update = DummyUpdate(DummyQuery(222, "cric_join_123"))

        self.assertTrue(button_access.check_callback_owner(update))

    def test_wpm_join_button_is_shared_for_exact_callback(self):
        button_access.register_button_owner(100, 200, 111)
        update = DummyUpdate(DummyQuery(222, "cric_join"))

        self.assertTrue(button_access.check_callback_owner(update))

    def test_wpm_lobby_controls_are_shared_for_state_validation(self):
        button_access.register_button_owner(100, 200, 111)

        cancel_update = DummyUpdate(DummyQuery(222, "cric_cancel_lobby"))
        decision_update = DummyUpdate(DummyQuery(222, "cric_decision:bat"))

        self.assertTrue(button_access.check_callback_owner(cancel_update))
        self.assertTrue(button_access.check_callback_owner(decision_update))

    def test_giveaway_participate_button_is_shared_for_everyone(self):
        # The giveaway "Participate" button is broadcast to whole groups; anyone
        # may tap it (the callback validates GC membership + one-entry itself),
        # so it must never be owner-locked — the reported "not for you" bug.
        button_access.register_button_owner(100, 200, 111)
        update = DummyUpdate(DummyQuery(222, "gwjoin_42"))

        self.assertTrue(button_access.check_callback_owner(update))
        # A shared-prefix keyboard must also not be *registered* as owner-locked.
        self.assertFalse(button_access.is_shared_callback_data("roster_page_1"))
        self.assertTrue(button_access.is_shared_callback_data("gwjoin_42"))

    def test_pbo_invite_buttons_are_shared_for_invitee_validation(self):
        button_access.register_button_owner(100, 200, 111)

        accept_update = DummyUpdate(DummyQuery(222, "pboacc_1_2_100"))
        decline_update = DummyUpdate(DummyQuery(222, "pbodec_1_2"))

        self.assertTrue(button_access.check_callback_owner(accept_update))
        self.assertTrue(button_access.check_callback_owner(decline_update))

    def test_challenge_league_buttons_are_shared_for_host_and_guest_validation(self):
        button_access.register_button_owner(100, 200, 111)

        callbacks = [
            "cl_team_123456_0",
            "cl_xi_123456_host",
            "cl_xi_123456_target",
            "cl_pick_123456_host_42",
            "cl_pick_123456_target_84",
            "cl_confirm_123456_host",
            "cl_confirm_123456_target",
            "cl_start_123456",
            # Pitch selection + the guest's Deny Match button share one prompt;
            # the handler decides who may press each (host picks, guest denies).
            "cl_pitch_123456_0",
            "cl_denymatch_123456",
        ]
        for callback_data in callbacks:
            with self.subTest(callback_data=callback_data):
                update = DummyUpdate(DummyQuery(222, callback_data))
                self.assertTrue(button_access.check_callback_owner(update))

    def test_cltour_guest_buttons_are_shared_for_guest_validation(self):
        # The /cltour setup + invite message is first sent while handling the
        # HOST's command, so it's owned by the host. The guest's team pick and
        # the Accept/Decline invite responses are pressed by the GUEST, so the
        # owner guard must let them through (the reported "not for you" bug);
        # handlers/cl_tour.py validates the clicker by host_tg/guest_tg/user2_id.
        button_access.register_button_owner(100, 200, 111)
        guest_callbacks = [
            "cltset_gt_111_45",  # guest picks team
            "clt_acc_7",         # guest accepts invite
            "clt_dec_7",         # guest declines invite
        ]
        for callback_data in guest_callbacks:
            with self.subTest(callback_data=callback_data):
                update = DummyUpdate(DummyQuery(222, callback_data))
                self.assertTrue(button_access.check_callback_owner(update))

    def test_cltour_host_only_buttons_stay_owner_locked(self):
        # Host-driven setup buttons live on the host-owned message, so they keep
        # the owner guard as defense-in-depth (a non-host press is blocked here
        # before the handler's own host_tg gate even runs).
        button_access.register_button_owner(100, 200, 111)
        for callback_data in ("cltset_lg_111_2", "cltset_ht_111_45",
                              "cltset_n_111_5", "cltset_x_111"):
            with self.subTest(callback_data=callback_data):
                update = DummyUpdate(DummyQuery(222, callback_data))
                self.assertFalse(button_access.check_callback_owner(update))

    def test_playmatch_flow_buttons_are_shared_for_both_players(self):
        # The invited player (and, downstream, the other player on alternating
        # turns) clicks buttons on a message "owned" by the command sender, so
        # the whole /playmatch handshake must bypass the owner guard.
        button_access.register_button_owner(100, 200, 111)
        callbacks = [
            "matchacc_5_42", "matchdeny_5_42",
            "oversset_5_42_10", "overscustom_5_42",
            "toss_bat_5_42", "toss_bowl_5_42",
            "op1_5_42", "op2_5_42", "selbowl_5_42",
            "bvar_5_3", "blen_5_3", "bspin_5_3", "bshot_5_3",
            "nbowl_5_42", "newbat_5_42",
        ]
        for callback_data in callbacks:
            with self.subTest(callback_data=callback_data):
                update = DummyUpdate(DummyQuery(222, callback_data))
                self.assertTrue(button_access.check_callback_owner(update))

    def test_cipl_flow_buttons_are_shared_for_both_captains(self):
        # /cipl over-by-over buttons are validated against bowl_user_tg /
        # bat_user_tg inside the handler, so the owner guard must let the
        # non-owner captain through (the reported "not for you" bug).
        button_access.register_button_owner(100, 200, 111)
        callbacks = [
            "cipl_coin_heads_99", "cipl_toss_bat_99_target",
            "cipl_bowler_5_42", "cipl_bowlapp_5_2", "cipl_batapp_5_3",
        ]
        for callback_data in callbacks:
            with self.subTest(callback_data=callback_data):
                update = DummyUpdate(DummyQuery(222, callback_data))
                self.assertTrue(button_access.check_callback_owner(update))

    def test_trade_flow_buttons_are_shared_for_both_captains(self):
        # /trade and /tradetrait both run one message through "user1 picks" →
        # "user2 picks" → "both confirm", first sent while handling user1's
        # command. The second captain's tap must reach the handler (which
        # validates by telegram_id) instead of being owner-blocked with
        # "This button is not for you" the moment it becomes their turn.
        button_access.register_button_owner(100, 200, 111)
        callbacks = [
            "t1p_abc123_42", "t2p_abc123_84",
            "tcfrm_abc123_7", "tcancel_abc123",
            "tt1_abc123_42", "tt2_abc123_84",
            "ttcfrm_abc123_7", "ttcancel_abc123",
        ]
        for callback_data in callbacks:
            with self.subTest(callback_data=callback_data):
                update = DummyUpdate(DummyQuery(222, callback_data))
                self.assertTrue(button_access.check_callback_owner(update))

    def test_unregistered_legacy_buttons_remain_usable(self):
        update = DummyUpdate(DummyQuery(222, "roster_page_2"))

        self.assertTrue(button_access.check_callback_owner(update))


if __name__ == "__main__":
    unittest.main()

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

    def test_cdraft_buttons_are_shared_for_two_captains(self):
        """The reported "/cdraft user can't join" bug.

        The lobby is posted while handling the HOST's /cdraft, so it registers to
        the host — and the person who has to press Join is the guest. Each slot
        card is likewise sent while handling the previous picker's tap, while the
        snake order hands the next pick to the other captain. handlers/cdraft.py
        authorises every press against the draft state, so the guard must let
        both captains through and let the handler decide.
        """
        button_access.register_button_owner(100, 200, 111)
        for data in ("cdj_123456", "cdc_123456", "cdp_123456_0_77"):
            with self.subTest(data=data):
                self.assertTrue(button_access.is_shared_callback_data(data))
                self.assertTrue(button_access.check_callback_owner(
                    DummyUpdate(DummyQuery(222, data))))

    def test_challenge_xi_controls_are_shared_so_a_restart_cannot_brick_them(self):
        """These worked only while the in-process registry happened to name the
        right captain; after a restart it is empty and they fell through to
        "anyone may press". Each already checks the clicker's telegram id, which
        is the stronger, restart-proof check."""
        button_access.register_button_owner(100, 200, 111)
        for data in ("cl_useprev_1_host", "cl_clear_1_target", "cl_edit_1_host"):
            with self.subTest(data=data):
                self.assertTrue(button_access.is_shared_callback_data(data))
                self.assertTrue(button_access.check_callback_owner(
                    DummyUpdate(DummyQuery(222, data))))

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


class OwnerTagTests(unittest.TestCase):
    """Self-describing buttons: the callback data names its own owner."""

    def tearDown(self):
        button_access._OWNER_BY_MESSAGE.clear()

    def test_tag_and_split_round_trip(self):
        tagged = button_access.tag_owner("dr_view_", 1234567890)

        self.assertEqual(tagged, "dr_view_u1234567890")
        self.assertEqual(button_access.split_owner("dr_view_", tagged + "_order", "_"),
                         (1234567890, "order"))

    def test_untagged_data_reads_back_byte_for_byte(self):
        # Buttons sent before this shipped carry no tag. They must decode with
        # the same call the tagged ones use, separator untouched — an empty
        # leading field (dr_srch_~~a~~0~) is data, not a separator.
        self.assertEqual(button_access.split_owner("dr_srch_", "dr_srch_~~a~~0~", "~"),
                         (None, "~~a~~0~"))
        self.assertEqual(button_access.split_owner("dr_view_", "dr_view_order", "_"),
                         (None, "order"))

    def test_separator_is_consumed_only_once_after_a_tag(self):
        # The tagged twin of the case above: exactly one separator belongs to
        # the tag, and the empty first field behind it has to survive.
        self.assertEqual(button_access.split_owner("dr_srch_", "dr_srch_u7~~~a~~0~", "~"),
                         (7, "~~a~~0~"))

    def test_tag_owner_without_an_owner_leaves_data_untagged(self):
        self.assertEqual(button_access.tag_owner("dr_srch_", None), "dr_srch_")
        self.assertIsNone(
            button_access.owner_from_callback_data("dr_srch_~~a~~0~"))

    def test_non_owner_is_blocked_without_any_registration(self):
        # The point of the tag: no register_button_owner call, no surviving
        # process state — the button alone decides. This is what a restart
        # used to throw away.
        owner = DummyUpdate(DummyQuery(111, "dr_view_u111_pool"))
        stranger = DummyUpdate(DummyQuery(222, "dr_view_u111_pool"))

        self.assertTrue(button_access.check_callback_owner(owner))
        self.assertFalse(button_access.check_callback_owner(stranger))

    def test_tag_wins_over_a_stale_registration(self):
        # If the two ever disagree, the button's own claim is authoritative.
        button_access.register_button_owner(100, 200, 999)
        update = DummyUpdate(DummyQuery(111, "dr_pick_u111_57_42"))

        self.assertTrue(button_access.check_callback_owner(update))

    def test_draft_buttons_are_no_longer_shared(self):
        # They used to be in SHARED_CALLBACK_PREFIXES, which made every board
        # and pool browser drivable by the whole room.
        for callback_data in ("dr_view_u111_order", "dr_srch_u111~~~a~~0~",
                              "dr_pick_u111_57_42"):
            with self.subTest(callback_data=callback_data):
                self.assertFalse(
                    button_access.is_shared_callback_data(callback_data))

    def test_every_draft_prefix_explains_what_to_do_instead(self):
        messages = {
            "dr_view_u111_order": "/dboard",
            "dr_srch_u111~~~a~~0~": "/dsearch",
            "dr_pick_u111_57_42": "/pick",
        }
        for callback_data, command in messages.items():
            with self.subTest(callback_data=callback_data):
                message = button_access.blocked_message_for(callback_data)
                self.assertIn(command, message)
                self.assertNotEqual(message, button_access.BLOCKED_BUTTON_MESSAGE)
                # Telegram caps a callback answer at 200 characters and renders
                # it as plain text, not HTML.
                self.assertLessEqual(len(message), 200)
                self.assertNotIn("<", message.replace("<player>", ""))

    def test_unknown_prefix_keeps_the_generic_message(self):
        self.assertEqual(button_access.blocked_message_for("roster_page_2"),
                         button_access.BLOCKED_BUTTON_MESSAGE)

    def test_legacy_untagged_draft_buttons_are_not_bricked(self):
        # A board posted before the deploy has no tag, so it falls back to the
        # registry — and an unregistered one stays usable, as before.
        self.assertTrue(button_access.check_callback_owner(
            DummyUpdate(DummyQuery(222, "dr_view_order"))))

        button_access.register_button_owner(100, 200, 111)
        self.assertFalse(button_access.check_callback_owner(
            DummyUpdate(DummyQuery(222, "dr_view_order"))))

    def test_unusable_owner_ids_degrade_to_no_tag(self):
        # A tag that could not be read back (a minus sign, a non-number) must
        # not be written at all — an untagged button still has the registry.
        for bad in (None, 0, -5, "nope", object()):
            with self.subTest(owner=bad):
                self.assertEqual(button_access.tag_owner("dr_view_", bad),
                                 "dr_view_")

    def test_owner_digits_are_bounded(self):
        # A hand-crafted callback can't make the parser chew an unbounded run
        # of digits, and a nonsense tag degrades to "no owner" rather than
        # locking the button to somebody who does not exist.
        long_tag = "dr_view_u" + "9" * 40 + "_order"
        owner, _rest = button_access.split_owner("dr_view_", long_tag, "_")
        self.assertEqual(len(str(owner)), button_access._MAX_OWNER_DIGITS)
        self.assertEqual(button_access.split_owner("dr_view_", "dr_view_uu_order", "_"),
                         (None, "uu_order"))


if __name__ == "__main__":
    unittest.main()

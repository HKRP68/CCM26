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

    def test_pbo_invite_buttons_are_shared_for_invitee_validation(self):
        button_access.register_button_owner(100, 200, 111)

        accept_update = DummyUpdate(DummyQuery(222, "pboacc_1_2_100"))
        decline_update = DummyUpdate(DummyQuery(222, "pbodec_1_2"))

        self.assertTrue(button_access.check_callback_owner(accept_update))
        self.assertTrue(button_access.check_callback_owner(decline_update))

    def test_unregistered_legacy_buttons_remain_usable(self):
        update = DummyUpdate(DummyQuery(222, "roster_page_2"))

        self.assertTrue(button_access.check_callback_owner(update))


if __name__ == "__main__":
    unittest.main()

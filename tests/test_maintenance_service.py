import unittest

from services.maintenance_service import (
    is_command_update,
    should_block_update,
    should_reply_with_maintenance,
)


MAINTENANCE_ON = {"is_maintenance": True, "maintenance_bypass_ids": ""}


class DummyUser:
    def __init__(self, user_id):
        self.id = user_id


class DummyMessage:
    def __init__(self, text):
        self.text = text


class DummyUpdate:
    def __init__(self, text=None, user_id=123, callback_query=None):
        self.message = DummyMessage(text) if text is not None else None
        self.effective_user = DummyUser(user_id) if user_id is not None else None
        self.callback_query = callback_query


class MaintenanceServiceTests(unittest.TestCase):
    def test_command_messages_are_blocked_with_maintenance_reply(self):
        update = DummyUpdate("/playmatch")

        self.assertTrue(should_block_update(update, MAINTENANCE_ON))
        self.assertTrue(is_command_update(update))
        self.assertTrue(should_reply_with_maintenance(update))

    def test_group_chatter_is_blocked_silently_during_maintenance(self):
        update = DummyUpdate("hello everyone")

        self.assertTrue(should_block_update(update, MAINTENANCE_ON))
        self.assertFalse(is_command_update(update))
        self.assertFalse(should_reply_with_maintenance(update))

    def test_commands_addressed_to_bot_are_treated_as_commands(self):
        update = DummyUpdate("/playmatch@CricketCardBot")

        self.assertTrue(should_block_update(update, MAINTENANCE_ON))
        self.assertTrue(should_reply_with_maintenance(update))

    def test_bypassed_user_is_not_blocked(self):
        update = DummyUpdate("/playmatch", user_id=999)
        cfg = {"is_maintenance": True, "maintenance_bypass_ids": "999"}

        self.assertFalse(should_block_update(update, cfg))

    def test_callback_queries_are_not_blocked(self):
        update = DummyUpdate(text=None, callback_query=object())

        self.assertFalse(should_block_update(update, MAINTENANCE_ON))


if __name__ == "__main__":
    unittest.main()

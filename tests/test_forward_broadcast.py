import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from handlers import forward_broadcast


class DummyStatus:
    def __init__(self, text):
        self.text = text
        self.edits = []

    async def edit_text(self, text):
        self.edits.append(text)


class DummyMessage:
    def __init__(self, chat_id=10, message_id=20, reply_to_message=None):
        self.chat_id = chat_id
        self.message_id = message_id
        self.reply_to_message = reply_to_message
        self.replies = []

    async def reply_text(self, text):
        status = DummyStatus(text)
        self.replies.append(status)
        return status


class DummyBot:
    def __init__(self):
        self.forwarded = []

    async def forward_message(self, chat_id, from_chat_id, message_id):
        self.forwarded.append((chat_id, from_chat_id, message_id))


class ForwardBroadcastTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._old_env = {name: os.environ.get(name) for name in forward_broadcast.ADMIN_ID_ENV_VARS}
        for name in forward_broadcast.ADMIN_ID_ENV_VARS:
            os.environ.pop(name, None)
        os.environ["FORWARD_BROADCAST_DELAY_SECONDS"] = "0"
        self._config_patch = patch.object(forward_broadcast, "get_config", return_value={})
        self._config_patch.start()

    def tearDown(self):
        for name, value in self._old_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._config_patch.stop()
        os.environ.pop("FORWARD_BROADCAST_DELAY_SECONDS", None)

    def test_configured_admin_ids_accepts_multiple_env_names(self):
        os.environ["BOT_ADMIN_IDS"] = "111, 222"
        os.environ["ADMIN_CHAT_ID"] = "-100123,333"

        self.assertEqual(forward_broadcast.configured_admin_ids(), {111, 222, 333})
        self.assertTrue(forward_broadcast.is_forward_admin(222))
        self.assertFalse(forward_broadcast.is_forward_admin(444))

    async def test_forward_replied_message_sends_to_every_target(self):
        bot = DummyBot()
        context = SimpleNamespace(bot=bot)

        result = await forward_broadcast.forward_replied_message(
            context,
            chat_ids=[-1001, -1002],
            from_chat_id=777,
            message_id=55,
        )

        self.assertEqual(result.total, 2)
        self.assertEqual(result.sent, 2)
        self.assertEqual(result.failed, 0)
        self.assertEqual(bot.forwarded, [(-1001, 777, 55), (-1002, 777, 55)])

    async def test_group_command_requires_admin(self):
        source = DummyMessage(chat_id=999, message_id=50)
        command = DummyMessage(chat_id=123, message_id=51, reply_to_message=source)
        update = SimpleNamespace(
            effective_message=command,
            effective_user=SimpleNamespace(id=999),
            effective_chat=SimpleNamespace(type="private"),
        )
        context = SimpleNamespace(bot=DummyBot())

        await forward_broadcast.frwd_grp_handler(update, context)

        self.assertEqual(command.replies[0].text, "⛔ Only bot admins can use this command.")
        self.assertEqual(context.bot.forwarded, [])

    async def test_group_command_forwards_replied_dm_message_to_active_groups(self):
        os.environ["BOT_ADMIN_IDS"] = "999"
        source = DummyMessage(chat_id=999, message_id=50)
        command = DummyMessage(chat_id=999, message_id=51, reply_to_message=source)
        update = SimpleNamespace(
            effective_message=command,
            effective_user=SimpleNamespace(id=999),
            effective_chat=SimpleNamespace(type="private"),
        )
        context = SimpleNamespace(bot=DummyBot())

        with patch.object(forward_broadcast, "_target_chat_ids", return_value=[-1001, -1002]) as targets:
            await forward_broadcast.frwd_grp_handler(update, context)

        targets.assert_called_once_with(forward_broadcast.GROUP_CHAT_TYPES)
        self.assertEqual(context.bot.forwarded, [(-1001, 999, 50), (-1002, 999, 50)])
        self.assertIn("Sent: 2/2", command.replies[0].edits[0])

    async def test_private_command_uses_private_targets(self):
        os.environ["BOT_ADMIN_IDS"] = "999"
        source = DummyMessage(chat_id=999, message_id=50)
        command = DummyMessage(chat_id=999, message_id=51, reply_to_message=source)
        update = SimpleNamespace(
            effective_message=command,
            effective_user=SimpleNamespace(id=999),
            effective_chat=SimpleNamespace(type="private"),
        )
        context = SimpleNamespace(bot=DummyBot())

        with patch.object(forward_broadcast, "_target_chat_ids", return_value=[1234]) as targets:
            await forward_broadcast.frwd_prvt_handler(update, context)

        targets.assert_called_once_with(forward_broadcast.PRIVATE_CHAT_TYPES)
        self.assertEqual(context.bot.forwarded, [(1234, 999, 50)])


if __name__ == "__main__":
    unittest.main()

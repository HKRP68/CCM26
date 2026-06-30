import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from handlers import forward_broadcast
from services import admin_ids


class DummyStatus:
    def __init__(self, text):
        self.text = text
        self.edits = []

    async def edit_text(self, text):
        self.edits.append(text)


class DummyMessage:
    def __init__(self, chat_id=10, message_id=20, reply_to_message=None,
                 media_group_id=None):
        self.chat_id = chat_id
        self.message_id = message_id
        self.reply_to_message = reply_to_message
        self.media_group_id = media_group_id
        self.replies = []

    async def reply_text(self, text):
        status = DummyStatus(text)
        self.replies.append(status)
        return status


class DummyBot:
    def __init__(self):
        # copied: single copy_message calls; copied_groups: copy_messages calls.
        self.copied = []
        self.copied_groups = []

    async def copy_message(self, chat_id, from_chat_id, message_id):
        self.copied.append((chat_id, from_chat_id, message_id))

    async def copy_messages(self, chat_id, from_chat_id, message_ids):
        self.copied_groups.append((chat_id, from_chat_id, tuple(message_ids)))


class ForwardBroadcastTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._old_env = {name: os.environ.get(name) for name in forward_broadcast.ADMIN_ID_ENV_VARS}
        for name in forward_broadcast.ADMIN_ID_ENV_VARS:
            os.environ.pop(name, None)
        os.environ["FORWARD_BROADCAST_DELAY_SECONDS"] = "0"
        # Admin-ID aggregation now lives in services.admin_ids; patch get_config
        # there so the env-var-driven assertions stay isolated from real config.
        self._config_patch = patch.object(admin_ids, "get_config", return_value={})
        self._config_patch.start()
        forward_broadcast._album_cache.clear()

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
        # copy_message (not forward_message) → no "Forwarded from" attribution.
        self.assertEqual(bot.copied, [(-1001, 777, 55), (-1002, 777, 55)])
        self.assertEqual(bot.copied_groups, [])

    async def test_album_is_copied_as_a_group(self):
        bot = DummyBot()
        context = SimpleNamespace(bot=bot)

        result = await forward_broadcast.forward_replied_message(
            context,
            chat_ids=[-1001, -1002],
            from_chat_id=777,
            message_id=55,
            message_ids=[55, 56, 57],
        )

        self.assertEqual(result.sent, 2)
        # Whole album copied per target; no single-message copies.
        self.assertEqual(bot.copied, [])
        self.assertEqual(bot.copied_groups, [
            (-1001, 777, (55, 56, 57)),
            (-1002, 777, (55, 56, 57)),
        ])

    def test_album_buffer_collects_and_sorts_ids(self):
        forward_broadcast.remember_album_message(999, "mg1", 57)
        forward_broadcast.remember_album_message(999, "mg1", 55)
        forward_broadcast.remember_album_message(999, "mg1", 56)
        forward_broadcast.remember_album_message(999, "mg1", 55)  # dup ignored
        self.assertEqual(forward_broadcast.get_album_message_ids(999, "mg1"),
                         [55, 56, 57])
        # Unknown group → empty.
        self.assertEqual(forward_broadcast.get_album_message_ids(999, "nope"), [])

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
        self.assertEqual(context.bot.copied, [])
        self.assertEqual(context.bot.copied_groups, [])

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
        self.assertEqual(context.bot.copied, [(-1001, 999, 50), (-1002, 999, 50)])
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
        self.assertEqual(context.bot.copied, [(1234, 999, 50)])


if __name__ == "__main__":
    unittest.main()

"""Official GC membership check used by the giveaway Participate button and draw."""

import asyncio
import unittest
from types import SimpleNamespace

from telegram.error import BadRequest

from services.giveaway_service import is_official_member


class _Bot:
    """get_chat_member stub: ``chats`` maps a chat id/handle to a status, or to
    an exception to raise; unknown chats raise 'Chat not found'."""

    def __init__(self, chats):
        self.chats = chats
        self.calls = []

    async def get_chat_member(self, chat, uid):
        self.calls.append(chat)
        r = self.chats.get(chat, BadRequest("Chat not found"))
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(status=r, is_member=(r == "restricted"))


def _run(bot, gid, cfg=None):
    return asyncio.run(is_official_member(bot, gid, 42, cfg))


class IsOfficialMemberTest(unittest.TestCase):
    def test_member_by_id(self):
        self.assertTrue(_run(_Bot({-1001234567890: "member"}), -1001234567890))

    def test_left_is_not_member(self):
        self.assertFalse(_run(_Bot({-1001234567890: "left"}), -1001234567890))

    def test_user_not_found_is_not_member(self):
        bot = _Bot({-1001234567890: BadRequest("User not found")})
        self.assertFalse(_run(bot, -1001234567890))

    def test_id_missing_100_prefix_is_repaired(self):
        bot = _Bot({-1001234567890: "member"})
        self.assertTrue(_run(bot, -1234567890))
        self.assertEqual(bot.calls, [-1234567890, -1001234567890])

    def test_falls_back_to_handle_when_id_is_wrong(self):
        cfg = SimpleNamespace(branding_group_username="cmugames",
                              official_group_link=None)
        bot = _Bot({"@cmugames": "administrator"})
        self.assertTrue(_run(bot, -1009999999999, cfg))

    def test_handle_says_left(self):
        cfg = SimpleNamespace(branding_group_username="cmugames",
                              official_group_link=None)
        self.assertFalse(_run(_Bot({"@cmugames": "kicked"}), -1009999999999, cfg))

    def test_unreachable_group_fails_open(self):
        self.assertTrue(_run(_Bot({}), -1009999999999))


if __name__ == "__main__":
    unittest.main()

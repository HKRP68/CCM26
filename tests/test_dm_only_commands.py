"""Stat commands answer in DM, and the group redirect is a one-tap replay.

Three things have to hold or the redirect is worse than the spam it replaces:
the guard must never block a DM, the deep link must survive Telegram's 64-char
/start payload with the player's name intact, and one impatient manager must
not be able to turn the notice itself into the new flood.
"""

import ast
import asyncio
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

from services import dm_only as mod


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Message:
    def __init__(self, sent):
        self.sent = sent
        self.chat_id = -100
        self.message_id = 7

    async def reply_text(self, text, **kwargs):
        self.sent.append((text, kwargs.get("reply_markup")))
        return self


class _Bot:
    def __init__(self):
        self.deleted = []

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class DmOnlyTestBase(unittest.TestCase):
    def setUp(self):
        mod._last_notice.clear()
        self.sent = []
        self.bot = _Bot()

    def _update(self, chat_type="supergroup", user_id=1):
        message = _Message(self.sent)
        return SimpleNamespace(
            effective_chat=SimpleNamespace(id=-100, type=chat_type),
            effective_user=SimpleNamespace(id=user_id),
            effective_message=message,
            message=message)

    def _context(self, args=None):
        return SimpleNamespace(args=list(args or []), bot=self.bot, bot_data={})

    def _guard(self, command="gstats", args=("Virat", "Kohli"), **kwargs):
        return _run(mod.require_dm(self._update(**kwargs), self._context(args),
                                   command))

    @property
    def last_text(self):
        return self.sent[-1][0] if self.sent else ""

    @property
    def last_url(self):
        markup = self.sent[-1][1] if self.sent else None
        if markup is None:
            return None
        return markup.inline_keyboard[0][0].url


# ════════════════════════════════════════════════════════════════════
# The payload the deep link carries
# ════════════════════════════════════════════════════════════════════

class PayloadTests(unittest.TestCase):
    def test_a_command_and_its_arguments_survive_the_round_trip(self):
        payload = mod.build_start_payload("gstats", ["Virat", "Kohli"])
        self.assertEqual(mod.parse_start_payload(payload),
                         ("gstats", ["Virat", "Kohli"]))

    def test_a_bare_command_round_trips_too(self):
        self.assertEqual(mod.parse_start_payload(
            mod.build_start_payload("traits")), ("traits", []))

    def test_the_payload_stays_inside_telegrams_limits(self):
        payload = mod.build_start_payload("recentmatches", ["Ravindra Jadeja"])
        self.assertLessEqual(len(payload), mod.START_PAYLOAD_LIMIT)
        self.assertRegex(payload, r"^[A-Za-z0-9_-]+$")

    def test_an_over_long_argument_drops_to_the_bare_command(self):
        """Opening the command empty beats a link Telegram refuses to follow."""
        payload = mod.build_start_payload("gstats", ["x" * 200])
        self.assertEqual(payload, "cmd_gstats")
        self.assertEqual(mod.parse_start_payload(payload), ("gstats", []))

    def test_a_non_ascii_name_still_round_trips(self):
        payload = mod.build_start_payload("stats", ["Müller"])
        self.assertEqual(mod.parse_start_payload(payload), ("stats", ["Müller"]))

    def test_payloads_for_other_things_are_left_alone(self):
        for payload in ("", "debut", "ref12345", "cricket_9_-100"):
            self.assertEqual(mod.parse_start_payload(payload), (None, []))

    def test_a_command_that_is_not_dm_only_is_refused(self):
        """The payload is user-supplied — it must not name arbitrary handlers."""
        self.assertEqual(mod.parse_start_payload("cmd_grant"), (None, []))

    def test_a_corrupt_argument_blob_degrades_to_no_arguments(self):
        self.assertEqual(mod.parse_start_payload("cmd_stats-!!!!"),
                         ("stats", []))


# ════════════════════════════════════════════════════════════════════
# The guard
# ════════════════════════════════════════════════════════════════════

class GuardTests(DmOnlyTestBase):
    def test_a_dm_runs_the_command(self):
        self.assertTrue(self._guard(chat_type="private"))
        self.assertEqual(self.sent, [])

    def test_a_group_gets_the_notice_instead_of_the_answer(self):
        with _bot_username("CricMasterBot"):
            self.assertFalse(self._guard())
        self.assertIn("answers in DM", self.last_text)

    def test_the_notice_says_what_it_will_run(self):
        with _bot_username("CricMasterBot"):
            self._guard(args=["Virat", "Kohli"])
        self.assertIn("/gstats Virat Kohli", self.last_text)

    def test_the_button_deep_links_to_the_same_command(self):
        with _bot_username("CricMasterBot"):
            self._guard(args=["Virat Kohli"])
        url = self.last_url
        self.assertTrue(url.startswith("https://t.me/CricMasterBot?start="))
        payload = url.split("start=", 1)[1]
        self.assertEqual(mod.parse_start_payload(payload),
                         ("gstats", ["Virat", "Kohli"]))

    def test_without_a_configured_username_it_says_where_to_go(self):
        with _bot_username(""):
            self._guard()
        self.assertIsNone(self.sent[-1][1])
        self.assertIn("my DM", self.last_text)

    def test_a_second_try_within_the_window_posts_nothing(self):
        """Holding down /stats must not paste twelve notices into the group."""
        self._guard()
        self._guard()
        self._guard()
        self.assertEqual(len(self.sent), 1)

    def test_another_manager_still_gets_their_own_notice(self):
        self._guard(user_id=1)
        self._guard(user_id=2)
        self.assertEqual(len(self.sent), 2)

    def test_a_different_command_is_throttled_separately(self):
        self._guard(command="stats")
        self._guard(command="traits")
        self.assertEqual(len(self.sent), 2)

    def test_the_throttle_cache_does_not_grow_without_bound(self):
        for i in range(50):
            self._guard(user_id=i)
        self.assertEqual(len(mod._last_notice), 50)
        mod.THROTTLE_SECONDS, real = 0, mod.THROTTLE_SECONDS
        try:
            self._guard(user_id=999)
        finally:
            mod.THROTTLE_SECONDS = real
        self.assertEqual(len(mod._last_notice), 1)

    def test_an_update_with_no_chat_is_never_blocked(self):
        update = SimpleNamespace(effective_chat=None, effective_user=None,
                                 effective_message=None, message=None)
        self.assertTrue(_run(mod.require_dm(update, self._context(), "stats")))


# ════════════════════════════════════════════════════════════════════
# The wrapper bot.py registers
# ════════════════════════════════════════════════════════════════════

class WrapperTests(DmOnlyTestBase):
    def setUp(self):
        super().setUp()
        self.ran = []

        async def handler(update, context):
            self.ran.append(context.args)
            return "answered"

        self.wrapped = mod.dm_only("stats", handler)

    def test_a_dm_reaches_the_real_handler(self):
        result = _run(self.wrapped(self._update(chat_type="private"),
                                   self._context(["Kohli"])))
        self.assertEqual(result, "answered")
        self.assertEqual(self.ran, [["Kohli"]])

    def test_a_group_never_reaches_it(self):
        _run(self.wrapped(self._update(), self._context(["Kohli"])))
        self.assertEqual(self.ran, [])
        self.assertEqual(len(self.sent), 1)


# ════════════════════════════════════════════════════════════════════
# bot.py wiring — read from source, so this needs no bot token
# ════════════════════════════════════════════════════════════════════

BOT_SRC = (Path(__file__).resolve().parent.parent / "bot.py").read_text()


class WiringTests(unittest.TestCase):
    def test_every_dm_only_command_is_wrapped_at_registration(self):
        wrapped = set(re.findall(r'dm_only\("([a-z0-9_]+)"', BOT_SRC))
        missing = sorted(set(mod.DM_ONLY_COMMANDS) - wrapped)
        self.assertFalse(missing, f"registered without the DM guard: {missing}")

    def test_nothing_else_is_wrapped_by_accident(self):
        wrapped = set(re.findall(r'dm_only\("([a-z0-9_]+)"', BOT_SRC))
        extra = sorted(wrapped - set(mod.DM_ONLY_COMMANDS))
        self.assertFalse(extra, f"guarded but not declared DM-only: {extra}")

    def test_every_dm_only_command_can_be_replayed_from_its_deep_link(self):
        """A link that opens the DM and then does nothing is a dead end."""
        tree = ast.parse(BOT_SRC)
        replay = next(n for n in tree.body
                      if isinstance(n, ast.FunctionDef)
                      and n.name == "_dm_only_handlers")
        mapping = next(n for n in ast.walk(replay) if isinstance(n, ast.Dict))
        keys = {k.value for k in mapping.keys}
        self.assertEqual(keys, set(mod.DM_ONLY_COMMANDS))

    def test_the_trait_trade_stays_usable_in_a_group(self):
        """Trading needs both managers in the room — that is what groups are."""
        self.assertNotIn("tradetrait", mod.DM_ONLY_COMMANDS)
        self.assertNotIn('dm_only("tradetrait"', BOT_SRC)

    def test_owners_stays_usable_in_a_group(self):
        """Naming the people around you is the entire point of /owners."""
        self.assertNotIn("owners", mod.DM_ONLY_COMMANDS)


class _bot_username:
    """Set BOT_USERNAME for the block, restoring whatever was there."""

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        import os
        self.previous = os.environ.get("BOT_USERNAME")
        os.environ["BOT_USERNAME"] = self.value

    def __exit__(self, *exc):
        import os
        if self.previous is None:
            os.environ.pop("BOT_USERNAME", None)
        else:
            os.environ["BOT_USERNAME"] = self.previous
        return False


if __name__ == "__main__":
    unittest.main()

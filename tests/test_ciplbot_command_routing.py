"""/ciplbot must start exactly one draft, and league bot aliases must still work.

python-telegram-bot runs one handler PER GROUP, so a command with its own
CommandHandler in the default group is ALSO seen by the catch-all league-command
regex handler in group 1. That matters here because stripping "bot" off
"ciplbot" leaves "cipl" — a real league command. Without a guard the command
would open two drafts and the user would see a spurious "a team selection is
already in progress" error.
"""

import unittest
from unittest.mock import MagicMock

from handlers import ciplbot


class BotCommandAliasTests(unittest.TestCase):
    def setUp(self):
        # The resolver only reaches the DB for commands that survive the guards,
        # so a recording stub is enough to tell "resolved" from "skipped".
        self.calls = []

        def fake_resolver(command, session):
            self.calls.append(command)
            return ("bbl", "BBL") if command in ("cbbl", "challengebbl") else (None, None)

        import handlers.challenge as challenge
        self._real = challenge.is_challenge_league_command
        challenge.is_challenge_league_command = fake_resolver

    def tearDown(self):
        import handlers.challenge as challenge
        challenge.is_challenge_league_command = self._real

    def _resolve(self, command):
        return ciplbot.league_key_from_bot_command(command, MagicMock())

    def test_directly_registered_commands_are_not_re_resolved(self):
        # These already ran in the default handler group; resolving them again
        # would start a second draft for the same command.
        for command in ("/ciplbot", "ciplbot", "/ciplbot@MyBot",
                        "challengeiplbot", "lpbot", "vsbot", "wpmbot"):
            with self.subTest(command=command):
                self.assertEqual(self._resolve(command), (None, None))
        self.assertEqual(self.calls, [], "no DB lookup should have been attempted")

    def test_an_admin_league_gains_a_bot_alias_for_free(self):
        self.assertEqual(self._resolve("/cbblbot"), ("bbl", "BBL"))
        self.assertEqual(self._resolve("/challengebblbot"), ("bbl", "BBL"))

    def test_a_non_bot_command_is_ignored_without_a_lookup(self):
        for command in ("/cipl", "/cbbl", "/letsplay", "/start", "/bot"):
            with self.subTest(command=command):
                self.assertEqual(self._resolve(command), (None, None))
        self.assertEqual(self.calls, [])

    def test_an_unknown_league_bot_alias_resolves_to_nothing(self):
        self.assertEqual(self._resolve("/cnopebot"), (None, None))
        self.assertEqual(self.calls, ["cnope"])


if __name__ == "__main__":
    unittest.main()

import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


def _load_challenge_with_stubs():
    telegram = types.ModuleType("telegram")

    class InlineKeyboardButton:
        def __init__(self, text, callback_data=None, **kwargs):
            self.text = text
            self.callback_data = callback_data

    class InlineKeyboardMarkup:
        def __init__(self, inline_keyboard):
            self.inline_keyboard = inline_keyboard

    telegram.InlineKeyboardButton = InlineKeyboardButton
    telegram.InlineKeyboardMarkup = InlineKeyboardMarkup
    telegram.Update = type("Update", (), {})
    sys.modules["telegram"] = telegram

    telegram_ext = types.ModuleType("telegram.ext")
    telegram_ext.ContextTypes = SimpleNamespace(DEFAULT_TYPE=object)
    sys.modules["telegram.ext"] = telegram_ext

    database = types.ModuleType("database")
    database.get_session = lambda: DummySession()
    sys.modules["database"] = database

    models = types.ModuleType("models")
    models.Match = type("Match", (), {})
    models.User = type("User", (), {})
    class ChallengeLeague:
        is_active = True
    models.ChallengeLeague = ChallengeLeague
    models.FantasyLeague = type("FantasyLeague", (), {"name": "name"})
    sys.modules["models"] = models

    match_constants = types.ModuleType("services.match_constants")
    match_constants.MATCH_EXPIRE = 60
    match_constants.random_match_settings = lambda: {}
    sys.modules["services.match_constants"] = match_constants

    telegram_user_service = types.ModuleType("services.telegram_user_service")
    telegram_user_service.resolve_command_target = lambda *args, **kwargs: (None, "missing")
    telegram_user_service.sync_telegram_user = lambda session, tg_user: None
    sys.modules["services.telegram_user_service"] = telegram_user_service

    handlers_match = types.ModuleType("handlers.match")
    handlers_match._active_cric_match_for_user = lambda *args, **kwargs: None
    handlers_match._active_cric_match_in_chat = lambda *args, **kwargs: None
    handlers_match._active_match_in_chat = lambda *args, **kwargs: None
    handlers_match._chat_busy_message = lambda *args, **kwargs: "busy"
    handlers_match._cric_lobby_for_user = lambda *args, **kwargs: None
    handlers_match._mention = lambda user: "@user"
    handlers_match._user_label = lambda user: "User"
    sys.modules["handlers.match"] = handlers_match

    sys.modules.pop("handlers.challenge", None)
    from handlers import challenge
    return challenge


class DummyQuery:
    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return []


class DummySession:
    def query(self, *args, **kwargs):
        return DummyQuery()

    def close(self):
        pass


challenge = _load_challenge_with_stubs()


class DummyMessage:
    def __init__(self, text, reply_user=None):
        self.text = text
        self.caption = None
        self.reply_to_message = (
            SimpleNamespace(from_user=reply_user) if reply_user is not None else None
        )
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


class ChallengeLeagueCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_builtin_long_and_short_commands_resolve(self):
        session = DummySession()

        self.assertEqual(challenge.is_challenge_league_command("challengeIPL", session), ("ipl", "IPL"))
        self.assertEqual(challenge.is_challenge_league_command("cipl", session), ("ipl", "IPL"))
        self.assertEqual(challenge.is_challenge_league_command("challengeBBL", session), ("bbl", "BBL"))
        self.assertEqual(challenge.is_challenge_league_command("cbbl", session), ("bbl", "BBL"))
        self.assertEqual(challenge.is_challenge_league_command("challengeINT", session), ("int", "INT"))
        self.assertEqual(challenge.is_challenge_league_command("cint", session), ("int", "INT"))


    def test_admin_exact_command_alias_resolves(self):
        session = DummySession()

        with patch.object(
            challenge,
            "_challenge_league_command_aliases",
            return_value={"csa20": ("sa20", "SA20"), "playt20": ("t20", "International T20")},
        ):
            self.assertEqual(challenge.is_challenge_league_command("csa20", session), ("sa20", "SA20"))
            self.assertEqual(challenge.is_challenge_league_command("playt20", session), ("t20", "International T20"))

    def test_dynamic_command_resolves_admin_created_league_name(self):
        session = DummySession()

        with patch.object(challenge, "_challenge_leagues", return_value={"sa20": "SA20"}):
            self.assertEqual(challenge.is_challenge_league_command("csa20", session), ("sa20", "SA20"))
            self.assertEqual(challenge.is_challenge_league_command("challengeSA20", session), ("sa20", "SA20"))

    async def test_league_command_requires_reply(self):
        message = DummyMessage("/cipl")
        update = SimpleNamespace(
            effective_message=message,
            message=message,
            effective_user=SimpleNamespace(id=1),
        )

        with patch.object(challenge, "get_session", return_value=DummySession()), \
             patch.object(challenge, "_start_challenge_lobby", new=AsyncMock()) as start:
            await challenge.challenge_league_handler(update, SimpleNamespace())

        self.assertEqual(message.replies[0][0], challenge.CHALLENGE_REPLY_REQUIRED_MESSAGE)
        start.assert_not_called()

    async def test_league_command_rejects_self_challenge(self):
        reply_user = SimpleNamespace(id=1, is_bot=False)
        message = DummyMessage("/cipl", reply_user=reply_user)
        update = SimpleNamespace(
            effective_message=message,
            message=message,
            effective_user=SimpleNamespace(id=1),
        )

        with patch.object(challenge, "get_session", return_value=DummySession()), \
             patch.object(challenge, "_start_challenge_lobby", new=AsyncMock()) as start:
            await challenge.challenge_league_handler(update, SimpleNamespace())

        self.assertEqual(message.replies[0][0], "❌ You cannot challenge yourself.")
        start.assert_not_called()

    async def test_league_command_rejects_bot_accounts(self):
        reply_user = SimpleNamespace(id=2, is_bot=True)
        message = DummyMessage("/cipl", reply_user=reply_user)
        update = SimpleNamespace(
            effective_message=message,
            message=message,
            effective_user=SimpleNamespace(id=1),
        )

        with patch.object(challenge, "get_session", return_value=DummySession()), \
             patch.object(challenge, "_start_challenge_lobby", new=AsyncMock()) as start:
            await challenge.challenge_league_handler(update, SimpleNamespace())

        self.assertEqual(message.replies[0][0], "❌ Bot accounts cannot be challenged.")
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()

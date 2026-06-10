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
    models.ChallengeTeam = type("ChallengeTeam", (), {"league_id": "league_id", "sort_order": "sort_order", "name": "name"})
    models.ChallengePlayer = type("ChallengePlayer", (), {"team_id": "team_id", "sort_order": "sort_order", "name": "name"})
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
    handlers_match._mention = lambda user, fallback_name=None: "@user"
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
        self.photos = []

    async def reply_photo(self, **kwargs):
        self.photos.append(kwargs)

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

    async def test_league_command_sends_ipl_team_buttons_for_host_selection(self):
        reply_user = SimpleNamespace(id=2, is_bot=False)
        message = DummyMessage("/cipl", reply_user=reply_user)
        update = SimpleNamespace(
            effective_message=message,
            message=message,
            effective_user=SimpleNamespace(id=1),
            effective_chat=SimpleNamespace(id=-100),
        )
        host = SimpleNamespace(id=10, telegram_id=1)
        target = SimpleNamespace(id=20, telegram_id=2)
        context = SimpleNamespace(bot_data={})

        with patch.object(challenge, "get_session", return_value=DummySession()), \
             patch.object(challenge, "sync_telegram_user", side_effect=[target, host]):
            await challenge.challenge_league_handler(update, context)

        self.assertEqual(len(message.replies), 1)
        text, kwargs = message.replies[0]
        self.assertIn("League Battles · IPL", text)
        keyboard = kwargs["reply_markup"].inline_keyboard
        labels = [row[0].text for row in keyboard]
        self.assertEqual(labels, challenge.IPL_TEAM_NAMES)
        self.assertTrue(next(iter(context.bot_data)).startswith("challenge_team_draft_"))

    async def test_team_button_rejects_non_host_with_alert(self):
        context = SimpleNamespace(bot_data={
            challenge._challenge_team_draft_key(123456): {
                "host_tg_id": 1,
                "league_name": "IPL",
                "teams": challenge.IPL_TEAM_NAMES,
            }
        })
        query = SimpleNamespace(
            data="cl_team_123456_0",
            from_user=SimpleNamespace(id=99),
            answer=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query)

        await challenge.challenge_team_callback(update, context)

        query.answer.assert_awaited_once_with("This button is not for you. Please use your own command.", show_alert=True)

    async def test_host_selection_prompts_replied_user_next(self):
        context = SimpleNamespace(bot_data={
            challenge._challenge_team_draft_key(123456): {
                "host_tg_id": 1,
                "target_tg_id": 2,
                "league_name": "IPL",
                "teams": challenge.IPL_TEAM_NAMES,
                "turn": "host",
                "host": {"tg_id": 1, "name": "User 1"},
                "target": {"tg_id": 2, "name": "User 2"},
            }
        })
        query = SimpleNamespace(
            data="cl_team_123456_0",
            from_user=SimpleNamespace(id=1),
            answer=AsyncMock(),
            edit_message_caption=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query)

        await challenge.challenge_team_callback(update, context)

        draft = context.bot_data[challenge._challenge_team_draft_key(123456)]
        self.assertEqual(draft["host_team"], challenge.IPL_TEAM_NAMES[0])
        self.assertEqual(draft["turn"], "target")
        query.edit_message_caption.assert_awaited_once()
        self.assertIn("please select your IPL team", query.edit_message_caption.await_args.kwargs["caption"])

    async def test_target_cannot_choose_host_team_when_same_team_disabled(self):
        context = SimpleNamespace(bot_data={
            challenge._challenge_team_draft_key(123456): {
                "host_tg_id": 1,
                "target_tg_id": 2,
                "league_name": "IPL",
                "teams": challenge.IPL_TEAM_NAMES,
                "turn": "target",
                "host_team": challenge.IPL_TEAM_NAMES[0],
                "host": {"tg_id": 1, "name": "User 1"},
                "target": {"tg_id": 2, "name": "User 2"},
            }
        })
        query = SimpleNamespace(
            data="cl_team_123456_0",
            from_user=SimpleNamespace(id=2),
            answer=AsyncMock(),
            edit_message_caption=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query)

        with patch.object(challenge, "_same_team_challenge_enabled", return_value=False):
            await challenge.challenge_team_callback(update, context)

        query.answer.assert_awaited_once_with(
            "This team is already selected. Please choose another team.", show_alert=True
        )
        self.assertNotIn("target_team", context.bot_data[challenge._challenge_team_draft_key(123456)])

    async def test_target_can_choose_host_team_when_same_team_enabled(self):
        context = SimpleNamespace(bot_data={
            challenge._challenge_team_draft_key(123456): {
                "host_tg_id": 1,
                "target_tg_id": 2,
                "league_name": "IPL",
                "teams": challenge.IPL_TEAM_NAMES,
                "turn": "target",
                "host_team": challenge.IPL_TEAM_NAMES[0],
                "host": {"tg_id": 1, "name": "User 1"},
                "target": {"tg_id": 2, "name": "User 2"},
            }
        })
        query = SimpleNamespace(
            data="cl_team_123456_0",
            from_user=SimpleNamespace(id=2),
            answer=AsyncMock(),
            edit_message_caption=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query)

        with patch.object(challenge, "_same_team_challenge_enabled", return_value=True):
            await challenge.challenge_team_callback(update, context)

        draft = context.bot_data[challenge._challenge_team_draft_key(123456)]
        self.assertEqual(draft["target_team"], challenge.IPL_TEAM_NAMES[0])
        self.assertEqual(draft["turn"], "complete")


    async def test_target_selection_sends_challenge_created_xi_buttons(self):
        message = DummyMessage("picker")
        context = SimpleNamespace(bot_data={
            challenge._challenge_team_draft_key(123456): {
                "host_tg_id": 1,
                "target_tg_id": 2,
                "league_name": "IPL",
                "teams": challenge.IPL_TEAM_NAMES,
                "turn": "target",
                "host_team": challenge.IPL_TEAM_NAMES[0],
                "host": {"tg_id": 1, "name": "User 1"},
                "target": {"tg_id": 2, "name": "User 2"},
            }
        })
        query = SimpleNamespace(
            data="cl_team_123456_1",
            from_user=SimpleNamespace(id=2),
            answer=AsyncMock(),
            edit_message_caption=AsyncMock(),
            edit_message_text=AsyncMock(),
            message=message,
        )
        update = SimpleNamespace(callback_query=query)

        with patch.object(challenge, "_same_team_challenge_enabled", return_value=False), \
             patch.object(challenge, "_challenge_created_text", return_value="created"):
            await challenge.challenge_team_callback(update, context)

        self.assertEqual(message.replies[0][0], "created")
        keyboard = message.replies[0][1]["reply_markup"].inline_keyboard
        self.assertEqual([button.text for button in keyboard[0]], ["Select MI XI", "Select CSK XI"])
        self.assertEqual(keyboard[0][0].callback_data, "cl_xi_123456_host")
        self.assertEqual(keyboard[0][1].callback_data, "cl_xi_123456_target")

    async def test_xi_button_rejects_wrong_user_with_requested_alert(self):
        context = SimpleNamespace(bot_data={
            challenge._challenge_team_draft_key(123456): {
                "turn": "complete",
                "host_team": challenge.IPL_TEAM_NAMES[0],
                "target_team": challenge.IPL_TEAM_NAMES[1],
                "host": {"tg_id": 1, "name": "User 1"},
                "target": {"tg_id": 2, "name": "User 2"},
            }
        })
        query = SimpleNamespace(
            data="cl_xi_123456_host",
            from_user=SimpleNamespace(id=99),
            answer=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query)

        await challenge.challenge_xi_callback(update, context)

        query.answer.assert_awaited_once_with("This XI selection is not for you.", show_alert=True)


    async def test_xi_button_shows_all_team_players_as_selectable_buttons(self):
        players = [SimpleNamespace(id=i, name=f"MI Player {i}", details_json='{"category":"Batsman"}') for i in range(1, 13)]
        message = DummyMessage("xi")
        context = SimpleNamespace(bot_data={
            challenge._challenge_team_draft_key(123456): {
                "turn": "complete",
                "host_team": challenge.IPL_TEAM_NAMES[0],
                "target_team": challenge.IPL_TEAM_NAMES[1],
                "host": {"tg_id": 1, "name": "User 1"},
                "target": {"tg_id": 2, "name": "User 2"},
            }
        })
        query = SimpleNamespace(
            data="cl_xi_123456_host",
            from_user=SimpleNamespace(id=1),
            answer=AsyncMock(),
            message=message,
        )
        update = SimpleNamespace(callback_query=query)

        with patch.object(challenge, "get_session", return_value=DummySession()), \
             patch.object(challenge, "_challenge_team_players", return_value=players):
            await challenge.challenge_xi_callback(update, context)

        self.assertIn("<b>Selected:</b> 0/11", message.replies[0][0])
        buttons = [button for row in message.replies[0][1]["reply_markup"].inline_keyboard for button in row]
        self.assertEqual(len(buttons), 12)
        self.assertEqual(buttons[0].text, "MI Player 1")
        self.assertEqual(buttons[0].callback_data, "cl_pick_123456_host_1")

    async def test_xi_selection_enforces_order_count_and_confirm_button(self):
        categories = ["Wicket Keeper", "Bowler", "Bowler", "Bowler", "Bowler", "All-rounder"] + ["Batsman"] * 5
        players = [
            SimpleNamespace(id=i, name=f"Player {i}", details_json=f'{{"category":"{category}"}}')
            for i, category in enumerate(categories, start=1)
        ]
        context = SimpleNamespace(bot_data={
            challenge._challenge_team_draft_key(123456): {
                "turn": "complete",
                "host_team": challenge.IPL_TEAM_NAMES[0],
                "target_team": challenge.IPL_TEAM_NAMES[1],
                "host": {"tg_id": 1, "name": "User 1"},
                "target": {"tg_id": 2, "name": "User 2"},
            }
        })

        with patch.object(challenge, "get_session", return_value=DummySession()), \
             patch.object(challenge, "_challenge_team_players", return_value=players):
            for player_id in range(1, 12):
                query = SimpleNamespace(
                    data=f"cl_pick_123456_host_{player_id}",
                    from_user=SimpleNamespace(id=1),
                    answer=AsyncMock(),
                    edit_message_text=AsyncMock(),
                )
                await challenge.challenge_xi_pick_callback(SimpleNamespace(callback_query=query), context)

        selected = context.bot_data[challenge._challenge_team_draft_key(123456)]["xi_selections"]["host"]["player_ids"]
        self.assertEqual(selected, list(range(1, 12)))
        final_markup = query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(final_markup[-1][0].text, "Confirm XI")
        self.assertIn("11. Player 11", query.edit_message_text.await_args.args[0])

    async def test_xi_selection_rejects_invalid_eleventh_player_without_wicket_keeper(self):
        players = [SimpleNamespace(id=i, name=f"Player {i}", details_json='{"category":"Batsman"}') for i in range(1, 12)]
        context = SimpleNamespace(bot_data={
            challenge._challenge_team_draft_key(123456): {
                "turn": "complete",
                "host_team": challenge.IPL_TEAM_NAMES[0],
                "target_team": challenge.IPL_TEAM_NAMES[1],
                "host": {"tg_id": 1, "name": "User 1"},
                "target": {"tg_id": 2, "name": "User 2"},
                "xi_selections": {"host": {"player_ids": list(range(1, 11)), "confirmed": False}},
            }
        })
        query = SimpleNamespace(
            data="cl_pick_123456_host_11",
            from_user=SimpleNamespace(id=1),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )

        with patch.object(challenge, "get_session", return_value=DummySession()), \
             patch.object(challenge, "_challenge_team_players", return_value=players):
            await challenge.challenge_xi_pick_callback(SimpleNamespace(callback_query=query), context)

        query.answer.assert_awaited_once_with("Wicket Keeper is Must", show_alert=True)
        self.assertEqual(context.bot_data[challenge._challenge_team_draft_key(123456)]["xi_selections"]["host"]["player_ids"], list(range(1, 11)))

    async def test_both_xi_confirmations_send_match_ready_start_button(self):
        categories = ["Wicket Keeper", "Bowler", "Bowler", "Bowler", "Bowler", "All-rounder"] + ["Batsman"] * 5
        players = [
            SimpleNamespace(id=i, name=f"Player {i}", details_json=f'{{"category":"{category}"}}')
            for i, category in enumerate(categories, start=1)
        ]
        message = DummyMessage("confirm")
        context = SimpleNamespace(bot_data={
            challenge._challenge_team_draft_key(123456): {
                "turn": "complete",
                "league_name": "IPL",
                "host_team": challenge.IPL_TEAM_NAMES[0],
                "target_team": challenge.IPL_TEAM_NAMES[1],
                "host": {"tg_id": 1, "name": "User 1"},
                "target": {"tg_id": 2, "name": "User 2"},
                "xi_selections": {
                    "host": {"player_ids": list(range(1, 12)), "confirmed": True},
                    "target": {"player_ids": list(range(1, 12)), "confirmed": False},
                },
            }
        })
        query = SimpleNamespace(
            data="cl_confirm_123456_target",
            from_user=SimpleNamespace(id=2),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            message=message,
        )

        with patch.object(challenge, "get_session", return_value=DummySession()), \
             patch.object(challenge, "_challenge_team_players", return_value=players):
            await challenge.challenge_xi_confirm_callback(SimpleNamespace(callback_query=query), context)

        self.assertTrue(context.bot_data[challenge._challenge_team_draft_key(123456)]["xi_selections"]["target"]["confirmed"])
        ready_text, kwargs = message.replies[0]
        self.assertIn("🏏 <b>MATCH READY!</b>", ready_text)
        self.assertIn("⚔️ <b>Challenge Mode:</b> IPL", ready_text)
        self.assertIn("🔵 <b>Host Team:</b> Mumbai Indians", ready_text)
        self.assertIn("🟡 <b>Guest Team:</b> Chennai Super Kings", ready_text)
        self.assertIn("🎮 <b>Game Mode:</b> Classic Challenge", ready_text)
        self.assertIn("🌱 <b>Pitch Profile:</b> Balanced Pitch", ready_text)
        self.assertIn("🔥 MI vs CSK is ready to begin!", ready_text)
        self.assertIn("🟢 @user Click on Start Match", ready_text)
        self.assertEqual(kwargs["reply_markup"].inline_keyboard[0][0].text, "Start Match")
        self.assertEqual(kwargs["reply_markup"].inline_keyboard[0][0].callback_data, "cl_start_123456")

    async def test_start_match_rejects_non_host_with_requested_alert(self):
        context = SimpleNamespace(bot_data={
            challenge._challenge_team_draft_key(123456): {
                "turn": "complete",
                "league_name": "IPL",
                "host_team": challenge.IPL_TEAM_NAMES[0],
                "target_team": challenge.IPL_TEAM_NAMES[1],
                "host": {"tg_id": 1, "name": "User 1"},
                "target": {"tg_id": 2, "name": "User 2"},
                "xi_selections": {
                    "host": {"player_ids": list(range(1, 12)), "confirmed": True},
                    "target": {"player_ids": list(range(1, 12)), "confirmed": True},
                },
            }
        })
        query = SimpleNamespace(
            data="cl_start_123456",
            from_user=SimpleNamespace(id=2),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )

        await challenge.challenge_start_match_callback(SimpleNamespace(callback_query=query), context)

        query.answer.assert_awaited_once_with("Only the Host can start this match.", show_alert=True)
        query.edit_message_text.assert_not_awaited()

    async def test_host_can_start_match_after_both_xi_confirmed(self):
        context = SimpleNamespace(bot_data={
            challenge._challenge_team_draft_key(123456): {
                "turn": "complete",
                "league_name": "IPL",
                "host_team": challenge.IPL_TEAM_NAMES[0],
                "target_team": challenge.IPL_TEAM_NAMES[1],
                "host": {"tg_id": 1, "name": "User 1"},
                "target": {"tg_id": 2, "name": "User 2"},
                "xi_selections": {
                    "host": {"player_ids": list(range(1, 12)), "confirmed": True},
                    "target": {"player_ids": list(range(1, 12)), "confirmed": True},
                },
            }
        })
        query = SimpleNamespace(
            data="cl_start_123456",
            from_user=SimpleNamespace(id=1),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )

        await challenge.challenge_start_match_callback(SimpleNamespace(callback_query=query), context)

        self.assertTrue(context.bot_data[challenge._challenge_team_draft_key(123456)]["match_started"])
        query.answer.assert_awaited_once_with("Match started!")
        # Starting the match now launches the toss "exactly like the current system":
        # the guest is prompted to call the coin before the over-by-over chat match begins.
        self.assertIn("🪙 <b>TOSS</b>", query.edit_message_text.await_args.args[0])
        markup = query.edit_message_text.await_args.kwargs["reply_markup"]
        coin_callbacks = {btn.callback_data for row in markup.inline_keyboard for btn in row}
        self.assertEqual(coin_callbacks, {"cipl_coin_heads_123456", "cipl_coin_tails_123456"})


if __name__ == "__main__":
    unittest.main()

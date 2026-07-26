import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


# Modules this file swaps for stubs so ``handlers.challenge`` can be imported
# without Telegram or a database. They are put back the moment that import is
# done — leaving a stub ``models`` in sys.modules breaks every test module
# collected after this one ("cannot import name X from 'models'").
_STUBBED = ("telegram", "telegram.ext", "database", "models",
            "services.match_constants", "services.telegram_user_service",
            "handlers.match", "handlers.challenge")


def _load_challenge_with_stubs():
    saved = {name: sys.modules.get(name) for name in _STUBBED}
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
        id = "id"
    models.ChallengeLeague = ChallengeLeague
    models.ChallengeTeam = type("ChallengeTeam", (), {"league_id": "league_id", "sort_order": "sort_order", "name": "name"})
    models.ChallengePlayer = type("ChallengePlayer", (), {"team_id": "team_id", "sort_order": "sort_order", "name": "name"})
    models.FantasyLeague = type("FantasyLeague", (), {"name": "name"})
    sys.modules["models"] = models

    match_constants = types.ModuleType("services.match_constants")
    match_constants.MATCH_EXPIRE = 60
    match_constants.random_match_settings = lambda: {}
    match_constants.PITCH_TYPES = ["Dry", "Dusty", "Hard", "Even", "Flat", "Green", "Bouncy"]
    sys.modules["services.match_constants"] = match_constants

    telegram_user_service = types.ModuleType("services.telegram_user_service")
    telegram_user_service.resolve_command_target = lambda *args, **kwargs: (None, "missing")
    telegram_user_service.sync_telegram_user = lambda session, tg_user: None
    sys.modules["services.telegram_user_service"] = telegram_user_service

    handlers_match = types.ModuleType("handlers.match")
    handlers_match._active_cric_match_for_user = lambda *args, **kwargs: None
    handlers_match._active_cric_match_in_chat = lambda *args, **kwargs: None
    handlers_match._active_match_in_chat = lambda *args, **kwargs: None
    handlers_match._active_match_for_user = lambda *args, **kwargs: None
    handlers_match._chat_busy_message = lambda *args, **kwargs: "busy"
    handlers_match._cric_lobby_for_user = lambda *args, **kwargs: None
    handlers_match._mention = lambda user, fallback_name=None: "@user"
    handlers_match._user_busy_message = lambda *args, **kwargs: "busy"
    handlers_match._user_label = lambda user: "User"
    sys.modules["handlers.match"] = handlers_match

    sys.modules.pop("handlers.challenge", None)
    try:
        from handlers import challenge
    finally:
        # ``challenge`` holds direct references to the stub classes it imported,
        # so the tests below still run against them — but sys.modules goes back
        # to the real thing for everybody else.
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
    return challenge


class DummyQuery:
    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return []

    def first(self):
        return None


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
        # Teams render two per row; the final row is the Cancel button. Buttons
        # show the short code (e.g. MI, CSK) rather than the full name.
        team_labels = [btn.text for row in keyboard[:-1] for btn in row]
        expected_codes = [challenge.IPL_TEAM_META[name][0] for name in challenge.IPL_TEAM_NAMES]
        self.assertEqual(team_labels, expected_codes)
        self.assertEqual(keyboard[-1][0].text, "❌ Cancel")
        draft_id = int(next(k for k in context.bot_data if k.startswith("challenge_team_draft_")).rsplit("_", 1)[1])
        self.assertEqual(keyboard[-1][0].callback_data, f"cl_cancel_{draft_id}")
        # The same final row also carries a guest-facing Deny Match button.
        self.assertEqual(keyboard[-1][1].text, "🚫 Deny Match")
        self.assertEqual(keyboard[-1][1].callback_data, f"cl_denymatch_{draft_id}")
        self.assertTrue(any(k.startswith("challenge_team_draft_") for k in context.bot_data))
        # Starting the draft registers a per-chat lock so a second concurrent
        # league challenge in the same group is refused.
        self.assertEqual(
            context.bot_data.get(challenge._challenge_draft_chat_key(-100)), draft_id)

    async def test_draft_pins_league_id_from_resolved_record(self):
        # Regression: a /cipl draft must pin the resolved league's id so the XI
        # step resolves the *same* league whose teams populated the picker. Without
        # this the XI callback re-resolves by league_key alone and can land on a
        # different active league sharing the key — whose id has no matching team —
        # producing a spurious "No players are configured" alert.
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
        league_record = SimpleNamespace(
            id=77, name="IPL", short_code="IPL", image_url=None)

        with patch.object(challenge, "get_session", return_value=DummySession()), \
             patch.object(challenge, "sync_telegram_user", side_effect=[target, host]), \
             patch.object(challenge, "_get_challenge_league_record", return_value=league_record), \
             patch.object(challenge, "_league_teams", return_value=list(challenge.IPL_TEAM_NAMES)):
            await challenge.challenge_league_handler(update, context)

        draft = next(v for k, v in context.bot_data.items()
                     if k.startswith("challenge_team_draft_"))
        self.assertEqual(draft.get("league_id"), 77)

    def test_league_resolution_prefers_league_with_roster(self):
        # Regression: when two active leagues normalize to the same key (a
        # duplicate/leftover sharing a short_code), resolution must deterministically
        # prefer the one that actually has a populated roster. Landing on the empty
        # duplicate makes the team picker fall back to built-in names that don't exist
        # in that league, so the XI step surfaces a spurious "No players are configured"
        # alert. A team shell with no players must not win, either.
        empty_league = SimpleNamespace(id=9, name="IPL Old", short_code="IPL", command=None)
        full_league = SimpleNamespace(id=5, name="IPL", short_code="IPL", command=None)

        class _LeagueQuery:
            def __init__(self, rows):
                self._rows = rows

            def filter(self, *args, **kwargs):
                return self

            def order_by(self, *args, **kwargs):
                return self

            def all(self):
                return self._rows

        class _FakeSession:
            def __init__(self, rows):
                self._rows = rows

            def query(self, *args, **kwargs):
                return _LeagueQuery(self._rows)

            def close(self):
                pass

        has_players = lambda session, lid: lid == full_league.id
        # The empty duplicate is yielded first; the league with players must still win.
        for order in ([empty_league, full_league], [full_league, empty_league]):
            with patch.object(challenge, "_league_has_players", side_effect=has_players):
                resolved = challenge._get_challenge_league_record(
                    _FakeSession(order), "ipl")
            self.assertIs(resolved, full_league)

    def test_league_resolution_roster_check_error_avoids_known_empty(self):
        # If the roster check errors for one duplicate (transient DB blip) and another
        # is *known* empty, prefer the indeterminate one rather than pinning the league
        # we positively know has no roster — a failure must not read as "empty".
        known_empty = SimpleNamespace(id=3, name="IPL Old", short_code="IPL", command=None)
        blips = SimpleNamespace(id=7, name="IPL", short_code="IPL", command=None)

        class _LeagueQuery:
            def __init__(self, rows):
                self._rows = rows

            def filter(self, *args, **kwargs):
                return self

            def order_by(self, *args, **kwargs):
                return self

            def all(self):
                return self._rows

        class _FakeSession:
            def query(self, *args, **kwargs):
                return _LeagueQuery([known_empty, blips])

            def close(self):
                pass

        def roster(session, lid):
            if lid == blips.id:
                raise RuntimeError("connection reset")
            return False

        with patch.object(challenge, "_league_has_players", side_effect=roster):
            resolved = challenge._get_challenge_league_record(_FakeSession(), "ipl")
        self.assertIs(resolved, blips)

    async def test_second_league_command_blocked_while_draft_in_progress(self):
        reply_user = SimpleNamespace(id=3, is_bot=False)
        message = DummyMessage("/cipl", reply_user=reply_user)
        update = SimpleNamespace(
            effective_message=message,
            message=message,
            effective_user=SimpleNamespace(id=4),
            effective_chat=SimpleNamespace(id=-100),
        )
        host = SimpleNamespace(id=40, telegram_id=4)
        target = SimpleNamespace(id=30, telegram_id=3)
        # A draft is already live in this chat (-100).
        context = SimpleNamespace(bot_data={
            challenge._challenge_draft_chat_key(-100): 555555,
            challenge._challenge_team_draft_key(555555): {"chat_id": -100, "turn": "host"},
        })

        with patch.object(challenge, "get_session", return_value=DummySession()), \
             patch.object(challenge, "sync_telegram_user", side_effect=[target, host]):
            await challenge.challenge_league_handler(update, context)

        self.assertEqual(len(message.replies), 1)
        text, _ = message.replies[0]
        self.assertIn("already in progress", text)
        # No new draft was created.
        draft_keys = [k for k in context.bot_data if k.startswith("challenge_team_draft_")]
        self.assertEqual(draft_keys, [challenge._challenge_team_draft_key(555555)])

    async def test_deny_match_releases_chat_lock(self):
        context = SimpleNamespace(bot_data={
            challenge._challenge_draft_chat_key(-100): 123456,
            challenge._challenge_team_draft_key(123456): {
                "chat_id": -100,
                "host_tg_id": 1,
                "target_tg_id": 2,
                "host": {"tg_id": 1, "name": "User 1"},
                "target": {"tg_id": 2, "name": "User 2"},
            },
        })
        query = SimpleNamespace(
            data="cl_denymatch_123456",
            from_user=SimpleNamespace(id=2),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            edit_message_caption=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query)

        await challenge.challenge_deny_match_callback(update, context)

        # The draft and its per-chat lock are both gone, freeing the group.
        self.assertNotIn(challenge._challenge_team_draft_key(123456), context.bot_data)
        self.assertIsNone(
            challenge._active_draft_in_chat(context.bot_data, -100))

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


    async def test_target_selection_then_host_pitch_choice_sends_xi_buttons(self):
        message = DummyMessage("picker")
        context = SimpleNamespace(bot_data={
            challenge._challenge_team_draft_key(123456): {
                "draft_id": 123456,
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
             patch.object(challenge, "get_session", return_value=DummySession()), \
             patch.object(challenge, "_challenge_created_text", return_value="created"):
            await challenge.challenge_team_callback(update, context)

            # Once both teams are chosen, the HOST is first asked to pick a pitch.
            pitch_text, pitch_kwargs = message.replies[0]
            self.assertIn("Choose the pitch", pitch_text)
            pitch_rows = pitch_kwargs["reply_markup"].inline_keyboard
            pitch_buttons = [b for row in pitch_rows for b in row
                             if b.callback_data.startswith("cl_pitch_")]
            self.assertEqual(len(pitch_buttons), len(challenge.PITCH_TYPES))
            self.assertEqual(pitch_buttons[0].callback_data, "cl_pitch_123456_0")
            # The guest's Deny Match button sits on its own row at the bottom.
            self.assertEqual(pitch_rows[-1][0].callback_data, "cl_denymatch_123456")

            # The host picks the first pitch; only then do the XI buttons appear.
            pitch_query = SimpleNamespace(
                data="cl_pitch_123456_0",
                from_user=SimpleNamespace(id=1),
                answer=AsyncMock(),
                edit_message_text=AsyncMock(),
                edit_message_caption=AsyncMock(),
                message=message,
            )
            await challenge.challenge_pitch_callback(
                SimpleNamespace(callback_query=pitch_query), context)

        draft = context.bot_data[challenge._challenge_team_draft_key(123456)]
        self.assertEqual(draft["pitch_type"], challenge.PITCH_TYPES[0])
        self.assertEqual(message.replies[1][0], "created")
        keyboard = message.replies[1][1]["reply_markup"].inline_keyboard
        self.assertEqual([button.text for button in keyboard[0]], ["Select MI XI", "Select CSK XI"])
        self.assertEqual(keyboard[0][0].callback_data, "cl_xi_123456_host")
        self.assertEqual(keyboard[0][1].callback_data, "cl_xi_123456_target")

    async def test_deny_match_button_lets_guest_tear_down_the_challenge(self):
        context = SimpleNamespace(bot_data={
            challenge._challenge_team_draft_key(123456): {
                "draft_id": 123456,
                "turn": "complete",
                "host": {"tg_id": 1, "name": "User 1"},
                "target": {"tg_id": 2, "name": "User 2"},
            }
        })
        query = SimpleNamespace(
            data="cl_denymatch_123456",
            from_user=SimpleNamespace(id=2),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            edit_message_caption=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query)

        await challenge.challenge_deny_match_callback(update, context)

        # The guest denying drops the draft so the match cannot proceed.
        self.assertNotIn(
            challenge._challenge_team_draft_key(123456), context.bot_data)
        query.answer.assert_awaited_once_with("Match denied.")
        denied_text = query.edit_message_text.await_args.args[0]
        self.assertIn("Match denied", denied_text)

    async def test_deny_match_button_tells_host_to_use_own_command(self):
        context = SimpleNamespace(bot_data={
            challenge._challenge_team_draft_key(123456): {
                "draft_id": 123456,
                "turn": "complete",
                "host": {"tg_id": 1, "name": "User 1"},
                "target": {"tg_id": 2, "name": "User 2"},
            }
        })
        query = SimpleNamespace(
            data="cl_denymatch_123456",
            from_user=SimpleNamespace(id=1),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query)

        await challenge.challenge_deny_match_callback(update, context)

        # The host pressing the guest's Deny button is rejected, draft untouched.
        self.assertIn(
            challenge._challenge_team_draft_key(123456), context.bot_data)
        query.answer.assert_awaited_once_with(
            "This button is not for you. Please use your own command.",
            show_alert=True)

    async def test_pitch_button_rejects_guest_with_host_only_alert(self):
        context = SimpleNamespace(bot_data={
            challenge._challenge_team_draft_key(123456): {
                "draft_id": 123456,
                "turn": "complete",
                "host_team": challenge.IPL_TEAM_NAMES[0],
                "target_team": challenge.IPL_TEAM_NAMES[1],
                "host": {"tg_id": 1, "name": "User 1"},
                "target": {"tg_id": 2, "name": "User 2"},
            }
        })
        query = SimpleNamespace(
            data="cl_pitch_123456_0",
            from_user=SimpleNamespace(id=2),
            answer=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query)

        await challenge.challenge_pitch_callback(update, context)

        query.answer.assert_awaited_once_with(
            "Only the host can select the pitch.", show_alert=True)

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
             patch.object(challenge, "_query_team_players", return_value=players):
            await challenge.challenge_xi_callback(update, context)

        self.assertIn("<b>Selected:</b> 0/11", message.replies[0][0])
        buttons = [button for row in message.replies[0][1]["reply_markup"].inline_keyboard for button in row]
        # 12 numbered name buttons (no control row at 0 selected).
        self.assertEqual(len(buttons), 12)
        self.assertEqual(buttons[0].text, "1. MI Player 1")
        self.assertEqual(buttons[0].callback_data, "cl_pick_123456_host_1")

    async def test_xi_button_retries_transient_db_error_then_renders(self):
        # Regression: a transient DB error while loading the squad must NOT surface
        # as "No players are configured" (which looked random across teams/leagues).
        # The squad-open path retries with a fresh session and renders on success.
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

        # First attempt blows up (connection recycled/killed); second succeeds.
        load = patch.object(
            challenge, "_query_team_players",
            side_effect=[RuntimeError("connection reset"), players])
        # A *fresh* session per attempt — the retry must not reuse the failed one.
        sessions = [DummySession(), DummySession()]
        with patch.object(challenge, "get_session", side_effect=sessions) as get_session, load:
            await challenge.challenge_xi_callback(update, context)

        self.assertEqual(get_session.call_count, 2)
        # Rendered the squad — never told the user the team had no players.
        self.assertIn("<b>Selected:</b> 0/11", message.replies[0][0])
        for call in query.answer.await_args_list:
            self.assertNotIn("No players are configured", str(call))

    async def test_xi_button_shows_retry_hint_when_load_keeps_failing(self):
        # When every attempt errors out, the user gets a retriable hint, never the
        # misleading "No players are configured" alert.
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
            message=DummyMessage("xi"),
        )
        update = SimpleNamespace(callback_query=query)

        load = patch.object(
            challenge, "_query_team_players",
            side_effect=RuntimeError("connection reset"))
        with patch.object(challenge, "get_session", return_value=DummySession()), load:
            await challenge.challenge_xi_callback(update, context)

        (alert_text, kwargs), = [c.args and (c.args[0], c.kwargs) or (None, c.kwargs)
                                 for c in query.answer.await_args_list]
        self.assertIn("tap Select XI again", alert_text)
        self.assertNotIn("No players are configured", alert_text)

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
        control_labels = [btn.text for btn in final_markup[-1]]
        self.assertIn("✅ Confirm XI", control_labels)
        self.assertIn("🧹 Clear", control_labels)
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
            challenge._challenge_draft_chat_key(-100): 123456,
            challenge._challenge_team_draft_key(123456): {
                "chat_id": -100,
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
        # The per-chat lock must STILL be held through the toss window — the live
        # Match row doesn't exist until cipl_toss, so releasing here would let a
        # second /cipl open in this chat during the toss.
        self.assertIsNotNone(
            challenge._active_draft_in_chat(context.bot_data, -100))


def _squad_15():
    """15-man squad: ids 1-11 form a valid XI (1 keeper, 5 bowling options)."""
    categories = [
        "Wicket Keeper", "Bowler", "Bowler", "Bowler", "Bowler", "All-rounder",
        "Batsman", "Batsman", "Batsman", "Batsman", "Batsman",
        "Bowler", "All-rounder", "Wicket Keeper", "Batsman",
    ]
    return [
        SimpleNamespace(id=i, name=f"Player {i}", details_json=f'{{"category":"{c}","rating":{50 + i}}}')
        for i, c in enumerate(categories, start=1)
    ]


def _xi_draft(*, started=True, confirmed=False, player_ids=None):
    sel = {"player_ids": list(player_ids or []), "confirmed": confirmed}
    return {
        challenge._challenge_draft_chat_key(-100): 123456,
        challenge._challenge_team_draft_key(123456): {
            "draft_id": 123456,
            "chat_id": -100,
            "turn": "complete",
            "league_name": "IPL",
            "host_team": challenge.IPL_TEAM_NAMES[0],
            "target_team": challenge.IPL_TEAM_NAMES[1],
            "host": {"tg_id": 1, "name": "User 1"},
            "target": {"tg_id": 2, "name": "User 2"},
            "xi_started": {"host": started},
            "xi_selections": {"host": sel},
        },
    }


class CiplXiHybridTests(unittest.IsolatedAsyncioTestCase):
    def test_confirmed_text_lists_xi_and_bench(self):
        players = _squad_15()
        draft = {"host": {"tg_id": 1, "name": "User 1"}}
        text = challenge._challenge_xi_confirmed_text(
            draft, "host", "Mumbai Indians", players, list(range(1, 12)))
        self.assertIn("🏏 Playing XI", text)
        self.assertIn("🪑 Bench", text)
        self.assertIn("1. Player 1", text)
        self.assertIn("11. Player 11", text)
        # Bench continues from 12 over the non-selected squad players.
        self.assertIn("12. Player 12", text)
        self.assertIn("15. Player 15", text)

    def test_confirmed_text_capped_for_huge_roster(self):
        # A pathological admin team with a very large roster must not blow past
        # Telegram's 4096-char message limit (the bench list would overflow).
        players = [
            SimpleNamespace(id=i, name=f"Player With A Fairly Long Name {i}",
                            details_json='{"category":"Batsman","rating":80}')
            for i in range(1, 201)
        ]
        draft = {"host": {"tg_id": 1, "name": "User 1"}}
        confirmed = challenge._challenge_xi_confirmed_text(
            draft, "host", "Mega Team", players, list(range(1, 12)))
        self.assertLessEqual(len(confirmed), challenge.TELEGRAM_MSG_LIMIT)
        self.assertIn("1. Player With A Fairly Long Name 1", confirmed)  # XI kept

    async def test_quickselect_sets_batting_order(self):
        players = _squad_15()
        context = SimpleNamespace(bot_data=_xi_draft())
        msg = SimpleNamespace(text="11 10 9 8 7 6 5 4 3 2 1",
                              reply_text=AsyncMock(), delete=AsyncMock())
        update = SimpleNamespace(
            effective_message=msg,
            effective_chat=SimpleNamespace(id=-100),
            effective_user=SimpleNamespace(id=1),
        )
        with patch.object(challenge, "get_session", return_value=DummySession()), \
             patch.object(challenge, "_challenge_team_players", return_value=players):
            await challenge.challenge_xi_quickselect(update, context)

        ids = context.bot_data[challenge._challenge_team_draft_key(123456)]["xi_selections"]["host"]["player_ids"]
        self.assertEqual(ids, list(range(11, 0, -1)))
        msg.delete.assert_awaited_once()

    async def test_quickselect_allows_partial_selection(self):
        players = _squad_15()
        context = SimpleNamespace(bot_data=_xi_draft())
        msg = SimpleNamespace(text="1 2 3 4 5", reply_text=AsyncMock(), delete=AsyncMock())
        update = SimpleNamespace(
            effective_message=msg,
            effective_chat=SimpleNamespace(id=-100),
            effective_user=SimpleNamespace(id=1),
        )
        with patch.object(challenge, "get_session", return_value=DummySession()), \
             patch.object(challenge, "_challenge_team_players", return_value=players):
            await challenge.challenge_xi_quickselect(update, context)

        # Fewer than 11 are accepted as-is (no validation, no error reply).
        sel = context.bot_data[challenge._challenge_team_draft_key(123456)]["xi_selections"]["host"]
        self.assertEqual(sel["player_ids"], [1, 2, 3, 4, 5])
        # No Confirm XI button while under 11.
        markup = msg.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
        labels = [btn.text for row in markup for btn in row]
        self.assertNotIn("✅ Confirm XI", labels)
        msg.delete.assert_awaited_once()

    async def test_quickselect_rejects_more_than_eleven(self):
        players = _squad_15()
        context = SimpleNamespace(bot_data=_xi_draft())
        msg = SimpleNamespace(text="1 2 3 4 5 6 7 8 9 10 11 12",
                              reply_text=AsyncMock(), delete=AsyncMock())
        update = SimpleNamespace(
            effective_message=msg,
            effective_chat=SimpleNamespace(id=-100),
            effective_user=SimpleNamespace(id=1),
        )
        with patch.object(challenge, "get_session", return_value=DummySession()), \
             patch.object(challenge, "_challenge_team_players", return_value=players):
            await challenge.challenge_xi_quickselect(update, context)

        self.assertIn("at most 11", msg.reply_text.await_args.args[0])
        self.assertEqual(context.bot_data[challenge._challenge_team_draft_key(123456)]["xi_selections"]["host"]["player_ids"], [])

    async def test_quickselect_surfaces_validation_error(self):
        players = _squad_15()
        context = SimpleNamespace(bot_data=_xi_draft())
        # 11 batsmen-only style pick (no keeper / not enough bowling): use ids 7-11 + 15 batsmen mix
        msg = SimpleNamespace(text="7 8 9 10 11 15 2 3 4 5 6",
                              reply_text=AsyncMock(), delete=AsyncMock())
        update = SimpleNamespace(
            effective_message=msg,
            effective_chat=SimpleNamespace(id=-100),
            effective_user=SimpleNamespace(id=1),
        )
        with patch.object(challenge, "get_session", return_value=DummySession()), \
             patch.object(challenge, "_challenge_team_players", return_value=players):
            await challenge.challenge_xi_quickselect(update, context)

        # No keeper among these ids → validation rejects, selection unchanged.
        self.assertIn("Wicket Keeper", msg.reply_text.await_args.args[0])
        self.assertEqual(context.bot_data[challenge._challenge_team_draft_key(123456)]["xi_selections"]["host"]["player_ids"], [])

    async def test_quickselect_ignores_non_participant_and_unstarted(self):
        players = _squad_15()
        # Not a participant (id 99)
        context = SimpleNamespace(bot_data=_xi_draft())
        msg = SimpleNamespace(text="1 2 3 4 5 6 7 8 9 10 11",
                              reply_text=AsyncMock(), delete=AsyncMock())
        update = SimpleNamespace(
            effective_message=msg,
            effective_chat=SimpleNamespace(id=-100),
            effective_user=SimpleNamespace(id=99),
        )
        await challenge.challenge_xi_quickselect(update, context)
        msg.reply_text.assert_not_awaited()

        # Participant who never opened the picker (xi_started false)
        context2 = SimpleNamespace(bot_data=_xi_draft(started=False))
        msg2 = SimpleNamespace(text="1 2 3 4 5 6 7 8 9 10 11",
                               reply_text=AsyncMock(), delete=AsyncMock())
        update2 = SimpleNamespace(
            effective_message=msg2,
            effective_chat=SimpleNamespace(id=-100),
            effective_user=SimpleNamespace(id=1),
        )
        await challenge.challenge_xi_quickselect(update2, context2)
        msg2.reply_text.assert_not_awaited()

    async def test_quickselect_ignores_plain_chat(self):
        context = SimpleNamespace(bot_data=_xi_draft())
        msg = SimpleNamespace(text="hello team good luck", reply_text=AsyncMock(), delete=AsyncMock())
        update = SimpleNamespace(
            effective_message=msg,
            effective_chat=SimpleNamespace(id=-100),
            effective_user=SimpleNamespace(id=1),
        )
        await challenge.challenge_xi_quickselect(update, context)
        msg.reply_text.assert_not_awaited()

    async def test_clear_callback_empties_selection(self):
        players = _squad_15()
        context = SimpleNamespace(bot_data=_xi_draft(player_ids=range(1, 12)))
        query = SimpleNamespace(
            data="cl_clear_123456_host",
            from_user=SimpleNamespace(id=1),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            message=SimpleNamespace(message_id=5, chat_id=-100),
        )
        with patch.object(challenge, "get_session", return_value=DummySession()), \
             patch.object(challenge, "_challenge_team_players", return_value=players):
            await challenge.challenge_xi_clear_callback(SimpleNamespace(callback_query=query), context)

        self.assertEqual(context.bot_data[challenge._challenge_team_draft_key(123456)]["xi_selections"]["host"]["player_ids"], [])
        query.answer.assert_awaited_with("Selection cleared.")

    async def test_edit_callback_unconfirms_and_reopens(self):
        players = _squad_15()
        context = SimpleNamespace(
            bot_data=_xi_draft(player_ids=range(1, 12), confirmed=True),
            job_queue=None,
        )
        query = SimpleNamespace(
            data="cl_edit_123456_host",
            from_user=SimpleNamespace(id=1),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            message=SimpleNamespace(message_id=5, chat_id=-100),
        )
        with patch.object(challenge, "get_session", return_value=DummySession()), \
             patch.object(challenge, "_challenge_team_players", return_value=players):
            await challenge.challenge_xi_edit_callback(SimpleNamespace(callback_query=query), context)

        self.assertFalse(context.bot_data[challenge._challenge_team_draft_key(123456)]["xi_selections"]["host"]["confirmed"])

    async def test_edit_callback_refused_after_match_ready(self):
        players = _squad_15()
        bot_data = _xi_draft(player_ids=range(1, 12), confirmed=True)
        bot_data[challenge._challenge_team_draft_key(123456)]["match_ready_sent"] = True
        context = SimpleNamespace(bot_data=bot_data, job_queue=None)
        query = SimpleNamespace(
            data="cl_edit_123456_host",
            from_user=SimpleNamespace(id=1),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            message=SimpleNamespace(message_id=5, chat_id=-100),
        )
        await challenge.challenge_xi_edit_callback(SimpleNamespace(callback_query=query), context)
        query.answer.assert_awaited_once_with("Both XIs are locked — the match is starting.", show_alert=True)
        self.assertTrue(context.bot_data[challenge._challenge_team_draft_key(123456)]["xi_selections"]["host"]["confirmed"])

    async def test_change_swaps_bench_player_in_place(self):
        players = _squad_15()
        bot_data = _xi_draft(player_ids=range(1, 12), confirmed=True)
        # Pretend the confirmed message is tracked so /change edits in place.
        bot_data[challenge._challenge_team_draft_key(123456)]["xi_selections"]["host"].update(
            {"msg_id": 7, "msg_chat_id": -100})
        context = SimpleNamespace(
            bot_data=bot_data,
            args=["2", "13"],
            bot=SimpleNamespace(edit_message_text=AsyncMock()),
        )
        msg = SimpleNamespace(text="/change 2 13", reply_text=AsyncMock(), delete=AsyncMock())
        update = SimpleNamespace(
            effective_message=msg,
            effective_chat=SimpleNamespace(id=-100),
            effective_user=SimpleNamespace(id=1),
        )
        with patch.object(challenge, "get_session", return_value=DummySession()), \
             patch.object(challenge, "_challenge_team_players", return_value=players):
            await challenge.challenge_change_handler(update, context)

        ids = context.bot_data[challenge._challenge_team_draft_key(123456)]["xi_selections"]["host"]["player_ids"]
        # Slot 2 (player 2) replaced by bench #13 (player 13); order preserved otherwise.
        self.assertEqual(ids[1], 13)
        self.assertNotIn(2, ids)
        context.bot.edit_message_text.assert_awaited_once()
        msg.reply_text.assert_not_awaited()  # edited in place, no new message

    async def test_change_rejects_rule_breaking_swap(self):
        players = _squad_15()
        bot_data = _xi_draft(player_ids=range(1, 12), confirmed=True)
        context = SimpleNamespace(
            bot_data=bot_data,
            args=["1", "15"],  # drop the only keeper for a batsman
            bot=SimpleNamespace(edit_message_text=AsyncMock()),
        )
        msg = SimpleNamespace(text="/change 1 15", reply_text=AsyncMock(), delete=AsyncMock())
        update = SimpleNamespace(
            effective_message=msg,
            effective_chat=SimpleNamespace(id=-100),
            effective_user=SimpleNamespace(id=1),
        )
        with patch.object(challenge, "get_session", return_value=DummySession()), \
             patch.object(challenge, "_challenge_team_players", return_value=players):
            await challenge.challenge_change_handler(update, context)

        ids = context.bot_data[challenge._challenge_team_draft_key(123456)]["xi_selections"]["host"]["player_ids"]
        self.assertEqual(ids, list(range(1, 12)))  # unchanged
        self.assertIn("Wicket Keeper", msg.reply_text.await_args.args[0])

    async def test_change_requires_full_xi(self):
        players = _squad_15()
        context = SimpleNamespace(
            bot_data=_xi_draft(player_ids=[1, 2, 3]),
            args=["2", "13"],
            bot=SimpleNamespace(edit_message_text=AsyncMock()),
        )
        msg = SimpleNamespace(text="/change 2 13", reply_text=AsyncMock(), delete=AsyncMock())
        update = SimpleNamespace(
            effective_message=msg,
            effective_chat=SimpleNamespace(id=-100),
            effective_user=SimpleNamespace(id=1),
        )
        await challenge.challenge_change_handler(update, context)
        self.assertIn("full Playing XI", msg.reply_text.await_args.args[0])


class OverseasXiValidationTests(unittest.TestCase):
    """Min/Max overseas-in-XI enforcement in _challenge_xi_validation."""

    @staticmethod
    def _valid_eleven(overseas=0):
        """11 players satisfying keeper + 5 bowling-option rules.

        The first ``overseas`` players are flagged overseas via details_json so the
        column-less test stubs still resolve through _challenge_is_overseas.
        """
        roles = (["Wicket Keeper"] + ["Bowler"] * 5 + ["Batsman"] * 5)
        players = []
        for i, role in enumerate(roles):
            is_os = "true" if i < overseas else "false"
            players.append(SimpleNamespace(
                id=i + 1,
                name=f"P{i + 1}",
                details_json=f'{{"category":"{role}","is_overseas":{is_os}}}',
            ))
        return players

    def test_no_limits_allows_any_overseas_count(self):
        valid, _ = challenge._challenge_xi_validation(self._valid_eleven(overseas=7))
        self.assertTrue(valid)

    def test_max_overseas_blocks_excess(self):
        valid, error = challenge._challenge_xi_validation(
            self._valid_eleven(overseas=5), min_overseas=0, max_overseas=4)
        self.assertFalse(valid)
        self.assertIn("Max 4 overseas", error)

    def test_min_overseas_blocks_shortfall(self):
        valid, error = challenge._challenge_xi_validation(
            self._valid_eleven(overseas=0), min_overseas=1, max_overseas=4)
        self.assertFalse(valid)
        self.assertIn("Min 1 overseas", error)

    def test_within_limits_passes(self):
        valid, error = challenge._challenge_xi_validation(
            self._valid_eleven(overseas=3), min_overseas=1, max_overseas=4)
        self.assertTrue(valid)
        self.assertEqual(error, "")


if __name__ == "__main__":
    unittest.main()

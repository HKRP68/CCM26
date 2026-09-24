"""Auction focus mode — while a lot is on the block, the room runs one feature.

What is pinned here:

  • **The lock is the auction's group, not the bot.** A DM, another group, and
    an auction still in setup are all untouched; only the bound group of a
    live or paused auction refuses anything.
  • **Talking is never blocked.** The gate sees commands and buttons; plain
    chatter, photos and service messages pass without even a lookup.
  • **Every auction command survives its own lock**, aliases included — the
    set is pinned against bot.py's registrations in both directions, because a
    new a-command missing from it would be locked out by the feature it
    belongs to.
  • **Admins are never locked out**, and the switch (``/afocus``) can turn the
    whole thing off.
  • **The gate runs last** of the middleware and fails open.
"""

import ast
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services import auction_focus as F

ROOT = Path(__file__).resolve().parent.parent
LIVE = SimpleNamespace(status="live", focus_mode=1, name="Season 2", chat_id=-100)


class DummyMessage:
    def __init__(self, text, chat_type="supergroup"):
        self.text = text
        self.entities = []
        self.chat = SimpleNamespace(type=chat_type)


class DummyCallback:
    def __init__(self, data):
        self.data = data

    async def answer(self, *a, **kw):
        return None


class DummyUpdate:
    def __init__(self, text=None, callback_data=None, user_id=555,
                 chat_type="supergroup", **message_fields):
        self.message = (DummyMessage(text, chat_type)
                        if text is not None or message_fields else None)
        for field, value in message_fields.items():
            setattr(self.message, field, value)
        self.callback_query = (DummyCallback(callback_data)
                               if callback_data is not None else None)
        self.effective_user = SimpleNamespace(id=user_id)
        self.effective_chat = SimpleNamespace(type=chat_type, id=-100)


def _block(update, locked=True):
    """should_block_update with nobody bypassed."""
    with patch.object(F, "is_bypassed", return_value=False):
        return F.should_block_update(update, locked=locked)


class TheSwitchTests(unittest.TestCase):
    """focus_mode is an integer, and NULL — an older season — means ON."""

    def test_on_off_and_null(self):
        self.assertTrue(F.focus_mode_on(SimpleNamespace(focus_mode=1)))
        self.assertFalse(F.focus_mode_on(SimpleNamespace(focus_mode=0)))
        self.assertTrue(F.focus_mode_on(SimpleNamespace(focus_mode=None)))
        # A season row older than the column at all.
        self.assertTrue(F.focus_mode_on(SimpleNamespace()))
        self.assertFalse(F.focus_mode_on(None))

    def test_only_a_running_auction_locks_the_room(self):
        for status, locks in (("live", True), ("paused", True),
                              ("setup", False), ("completed", False),
                              ("cancelled", False)):
            season = SimpleNamespace(status=status, focus_mode=1)
            self.assertEqual(F.locks_the_room(season), locks, status)

    def test_the_switch_beats_the_status(self):
        self.assertFalse(F.locks_the_room(
            SimpleNamespace(status="live", focus_mode=0)))


class WhatIsBlockedTests(unittest.TestCase):

    def test_an_unrelated_command_is_blocked(self):
        for text in ("/claim", "/daily", "/vsbot 5", "/pxi"):
            self.assertTrue(_block(DummyUpdate(text)), text)

    def test_every_auction_command_gets_through(self):
        for name in sorted(F.AUCTION_COMMANDS):
            self.assertFalse(_block(DummyUpdate(f"/{name}")), name)

    def test_the_doorway_commands_get_through(self):
        for name in sorted(F.ALWAYS_ALLOWED):
            self.assertFalse(_block(DummyUpdate(f"/{name}")), name)

    def test_talking_is_never_blocked(self):
        for text in ("kohli is going for 20 cr", "lol", "@mumbai bid!"):
            self.assertFalse(_block(DummyUpdate(text)), text)

    def test_a_photo_or_a_service_message_is_never_blocked(self):
        self.assertFalse(_block(DummyUpdate(new_chat_members=[1])))
        self.assertFalse(_block(DummyUpdate(text=None, photo=["x"])))

    def test_auction_buttons_get_through_and_others_do_not(self):
        for data in ("au_bid_12_200", "au_rtm_3_yes", "au_info_purse",
                     "au_sets_2", "au_ret_7_yes", "noop"):
            self.assertFalse(_block(DummyUpdate(callback_data=data)), data)
        for data in ("market_buy_3", "quest_claim_1", "xi_pick_4"):
            self.assertTrue(_block(DummyUpdate(callback_data=data)), data)

    def test_another_bots_command_is_not_ours_to_refuse(self):
        update = DummyUpdate("/claim@SomeOtherBot")
        with patch.object(F, "is_bypassed", return_value=False):
            self.assertFalse(F.should_block_update(
                update, locked=True, bot_username="CricMasterBot"))
        update = DummyUpdate("/claim@CricMasterBot")
        with patch.object(F, "is_bypassed", return_value=False):
            self.assertTrue(F.should_block_update(
                update, locked=True, bot_username="CricMasterBot"))


class WhereTheLockAppliesTests(unittest.TestCase):

    def test_a_dm_is_never_locked(self):
        self.assertFalse(_block(DummyUpdate("/claim", chat_type="private")))
        self.assertFalse(_block(DummyUpdate(callback_data="market_buy_3",
                                            chat_type="private")))

    def test_an_unlocked_group_is_untouched(self):
        self.assertFalse(_block(DummyUpdate("/claim"), locked=False))
        self.assertFalse(_block(DummyUpdate(callback_data="market_buy_3"),
                                locked=False))

    def test_admins_are_never_locked_out(self):
        with patch.object(F, "is_bypassed", return_value=True):
            self.assertFalse(F.should_block_update(
                DummyUpdate("/claim"), locked=True))
            self.assertFalse(F.should_block_update(
                DummyUpdate(callback_data="market_buy_3"), locked=True))

    def test_the_bypass_is_only_asked_about_a_refusal(self):
        """The auction-admin lookup is a query — it must not run on every /bid."""
        with patch.object(F, "is_bypassed", return_value=False) as bypass:
            _ = F.should_block_update(DummyUpdate("/bid 15"), locked=True)
            _ = F.should_block_update(DummyUpdate("kohli!"), locked=True)
            _ = F.should_block_update(DummyUpdate(callback_data="au_bid_1_20"),
                                      locked=True)
        bypass.assert_not_called()


class TheBypassTests(unittest.TestCase):
    """Who is never locked out, and what a broken lookup does."""

    def test_a_bot_admin_is_bypassed_without_a_query(self):
        from services import admin_ids
        with patch.object(admin_ids, "is_admin", return_value=True), \
                patch("database.get_session") as session:
            self.assertTrue(F.is_bypassed(999))
        session.assert_not_called()

    def test_an_auction_admin_is_bypassed(self):
        from services import admin_ids, auction_service
        with patch.object(admin_ids, "is_admin", return_value=False), \
                patch("database.get_session",
                      return_value=SimpleNamespace(close=lambda: None)), \
                patch.object(auction_service, "is_auction_admin",
                             return_value=True):
            self.assertTrue(F.is_bypassed(999))

    def test_nobody_else_is(self):
        from services import admin_ids, auction_service
        with patch.object(admin_ids, "is_admin", return_value=False), \
                patch("database.get_session",
                      return_value=SimpleNamespace(close=lambda: None)), \
                patch.object(auction_service, "is_auction_admin",
                             return_value=False):
            self.assertFalse(F.is_bypassed(999))
        self.assertFalse(F.is_bypassed(None))

    def test_a_broken_lookup_fails_open(self):
        """Not knowing must cost a command, not cost somebody the bot."""
        from services import admin_ids
        def _boom(_id):
            raise RuntimeError("no config")
        with patch.object(admin_ids, "is_admin", _boom), \
                patch.object(F, "logger") as log:
            self.assertTrue(F.is_bypassed(999))
        self.assertTrue(log.exception.called)


class ThePreFilterTests(unittest.TestCase):
    """What the middleware asks before it looks anything up."""

    def test_only_commands_and_buttons_reach_the_lookup(self):
        self.assertTrue(F.is_command_or_button(DummyUpdate("/claim")))
        self.assertTrue(F.is_command_or_button(
            DummyUpdate(callback_data="market_buy_3")))
        self.assertFalse(F.is_command_or_button(DummyUpdate("just talking")))
        self.assertFalse(F.is_command_or_button(DummyUpdate(new_chat_members=[1])))

    def test_a_blocked_update_always_gets_an_answer(self):
        self.assertTrue(F.should_reply(DummyUpdate("/claim")))
        self.assertTrue(F.should_reply(DummyUpdate(callback_data="market_buy_3")))


class TheCacheTests(unittest.TestCase):
    """One query per chat per window, and an admin's change lands at once."""

    def setUp(self):
        F.invalidate()
        self.addCleanup(F.invalidate)

    def test_it_names_the_auction_and_caches_the_answer(self):
        calls = []
        session = SimpleNamespace(close=lambda: None)
        import database
        from services import auction_service

        def _lookup(_session, _chat_id):
            calls.append(_chat_id)
            return LIVE

        with patch.object(database, "get_session", lambda: session), \
                patch.object(auction_service, "season_for_chat", _lookup):
            self.assertEqual(F.locked_season_for_chat(-100), "Season 2")
            self.assertEqual(F.locked_season_for_chat(-100), "Season 2")
        self.assertEqual(len(calls), 1, "the lock state was queried twice")

    def test_an_auction_in_setup_does_not_lock(self):
        session = SimpleNamespace(close=lambda: None)
        import database
        from services import auction_service
        setup = SimpleNamespace(status="setup", focus_mode=1, name="Season 2")
        with patch.object(database, "get_session", lambda: session), \
                patch.object(auction_service, "season_for_chat",
                             lambda _s, _c: setup):
            self.assertIsNone(F.locked_season_for_chat(-100))

    def test_invalidation_makes_the_next_ask_look_again(self):
        answers = [LIVE, None]
        session = SimpleNamespace(close=lambda: None)
        import database
        from services import auction_service
        with patch.object(database, "get_session", lambda: session), \
                patch.object(auction_service, "season_for_chat",
                             lambda _s, _c: answers.pop(0)):
            self.assertEqual(F.locked_season_for_chat(-100), "Season 2")
            F.invalidate(-100)
            self.assertIsNone(F.locked_season_for_chat(-100))

    def test_a_broken_lookup_fails_open(self):
        import database

        def _boom():
            raise RuntimeError("no database")

        # The failure is logged rather than raised. The logger is patched
        # rather than asserted on with assertLogs, because several suites in
        # this directory call ``logging.disable`` at import time and a shared
        # test run would then see no records at all.
        with patch.object(database, "get_session", _boom), \
                patch.object(F, "logger") as log:
            self.assertIsNone(F.locked_season_for_chat(-100))
        self.assertTrue(log.exception.called, "the failure was swallowed silently")


class MessagingTests(unittest.TestCase):

    def test_the_refusal_names_the_auction_and_where_else_to_go(self):
        text = F.locked_message("Season 2")
        self.assertIn("Season 2", text)
        self.assertIn("/bid", text)
        self.assertIn("DM", text)

    def test_the_alert_carries_no_markup(self):
        alert = F.locked_alert("Season 2")
        self.assertNotIn("<b>", alert)
        self.assertNotIn("<code>", alert)

    def test_a_name_with_markup_in_it_is_escaped(self):
        self.assertIn("&lt;b&gt;", F.locked_message("<b>hax</b>"))


class TheCommandSetIsRealTests(unittest.TestCase):
    """The allow-list and bot.py's auction registrations must not drift.

    Parses bot.py rather than importing it (see tests/test_bot_menu_commands.py).
    The second direction is the one that would actually bite: an auction
    command added later and forgotten here would be locked out by the very
    feature it belongs to.
    """

    def _registered_auction_commands(self):
        src = (ROOT / "bot.py").read_text()
        start = src.index("from handlers.auction import (")
        end = src.index('app.add_handler(CommandHandler(["unscramble"', start)
        names = set()
        for entry in re.findall(
                r'CommandHandler\(\s*(\[[^\]]*\]|"[a-z0-9_]+")', src[start:end],
                re.S):
            names.update(re.findall(r'"([a-z0-9_]+)"', entry)
                         if entry.startswith("[") else [entry.strip('"')])
        return names

    def test_every_auction_command_bot_py_registers_is_allowed(self):
        missing = sorted(self._registered_auction_commands() - F.AUCTION_COMMANDS)
        self.assertFalse(missing, (
            "handlers/auction.py registers commands auction_focus would lock "
            f"out of the auction's own group: {missing}"))

    def test_the_allow_list_names_no_command_that_does_not_exist(self):
        src = (ROOT / "bot.py").read_text()
        registered = set()
        for entry in re.findall(
                r'CommandHandler\(\s*(\[[^\]]*\]|"[a-z0-9_]+")', src):
            registered.update(re.findall(r'"([a-z0-9_]+)"', entry)
                              if entry.startswith("[") else [entry.strip('"')])
        for name in sorted(F.AUCTION_COMMANDS | F.ALWAYS_ALLOWED):
            self.assertIn(name, registered, f"/{name} is not registered")

    def test_the_admin_help_card_names_the_switch(self):
        src = (ROOT / "services" / "auction_rich.py").read_text()
        self.assertIn("/afocus", src)


class TheMiddlewareTests(unittest.TestCase):
    """Where the gate sits, and that it cannot take the bot down with it."""

    def setUp(self):
        self.src = (ROOT / "bot.py").read_text()
        self.groups = {handler: int(group) for handler, group in re.findall(
            r"TypeHandler\(_TGUpdate,\s*(\w+)\),\s*group=(-?\d+)", self.src)}

    def test_it_is_registered_and_runs_last_of_the_middleware(self):
        self.assertIn("_auction_focus_check", self.groups)
        mine = self.groups["_auction_focus_check"]
        for other in ("_maintenance_check", "_ban_check", "_rookie_check"):
            self.assertGreater(mine, self.groups[other], other)
        # …and still before every command handler, which live in group 0+.
        self.assertLess(mine, 0)

    def test_each_middleware_still_has_a_group_of_its_own(self):
        groups = [int(g) for _h, g in re.findall(
            r"TypeHandler\(_TGUpdate,\s*(\w+)\),\s*group=(-?\d+)", self.src)]
        self.assertEqual(len(groups), len(set(groups)),
                         "two middlewares share a handler group")

    def test_the_gate_swallows_its_own_failures(self):
        """Everything but ApplicationHandlerStop is logged, not raised."""
        tree = ast.parse(self.src)
        found = [node for node in ast.walk(tree)
                 if isinstance(node, ast.AsyncFunctionDef)
                 and node.name == "_auction_focus_check"]
        self.assertEqual(len(found), 1)
        handlers = [h for node in found
                    for t in ast.walk(node) if isinstance(t, ast.Try)
                    for h in t.handlers]
        names = {getattr(h.type, "id", None) for h in handlers}
        self.assertIn("Exception", names)
        self.assertIn("ApplicationHandlerStop", names)


if __name__ == "__main__":
    unittest.main()

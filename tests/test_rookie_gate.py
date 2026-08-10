"""Tests for Rookie mode — the members-only gate.

Two things must hold, and they pull against each other:
  * ON, a non-member reaches NOTHING except the doorway commands, in the bot
    and in the Mini App alike;
  * OFF, the gate is invisible — no behaviour change, and no database work.
"""

import ast
import re
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services import rookie_gate

ROOKIE_ON = {"rookie_mode": True}
ROOKIE_OFF = {"rookie_mode": False}


def _member(tier="rookie", days_left=20):
    return SimpleNamespace(
        subscription_tier=tier,
        subscription_expires_at=(None if tier == "none"
                                 else datetime.utcnow()
                                 + timedelta(days=days_left)),
    )


class DummyMessage:
    def __init__(self, text):
        self.text = text
        self.entities = []


class DummyCallback:
    def __init__(self, data):
        self.data = data


class DummyUpdate:
    def __init__(self, text=None, callback_data=None, user_id=555):
        self.message = DummyMessage(text) if text is not None else None
        self.callback_query = (DummyCallback(callback_data)
                               if callback_data is not None else None)
        self.effective_user = SimpleNamespace(id=user_id)


def _block(update, has_membership=False, **kw):
    """should_block_update with the gate ON and nobody bypassed."""
    with patch.object(rookie_gate, "is_bypassed", return_value=False):
        return rookie_gate.should_block_update(
            update, has_membership=has_membership, cfg=ROOKIE_ON, **kw)


class ModeOffTests(unittest.TestCase):
    """With the switch off the gate must be inert."""

    def test_nothing_is_blocked(self):
        for update in (DummyUpdate("/claim"), DummyUpdate("hello"),
                       DummyUpdate(callback_data="market_buy_3")):
            self.assertFalse(rookie_gate.should_block_update(
                update, has_membership=False, cfg=ROOKIE_OFF))

    def test_miniapp_is_open(self):
        self.assertFalse(rookie_gate.should_block_miniapp_path(
            "/api/webapp/claim", has_membership=False, cfg=ROOKIE_OFF))

    def test_mode_reads_the_config_flag(self):
        self.assertTrue(rookie_gate.is_rookie_mode_active(ROOKIE_ON))
        self.assertFalse(rookie_gate.is_rookie_mode_active(ROOKIE_OFF))
        self.assertFalse(rookie_gate.is_rookie_mode_active({}))


class NonMemberTests(unittest.TestCase):
    """ON + no membership = no bot."""

    def test_ordinary_commands_are_blocked(self):
        for command in ("/claim", "/daily", "/playmatch", "/cmumysterybox",
                        "/myroster", "/buypl 3"):
            self.assertTrue(_block(DummyUpdate(command)), command)

    def test_buttons_are_blocked(self):
        # Unlike maintenance mode, callbacks are NOT waved through: a
        # non-member could not have started anything worth protecting.
        self.assertTrue(_block(DummyUpdate(callback_data="mp_stats_7")))

    def test_group_chatter_is_blocked_silently(self):
        update = DummyUpdate("just chatting")
        self.assertTrue(_block(update))
        self.assertFalse(rookie_gate.should_reply(update))

    def test_blocked_commands_and_buttons_do_get_an_answer(self):
        self.assertTrue(rookie_gate.should_reply(DummyUpdate("/claim")))
        self.assertTrue(rookie_gate.should_reply(
            DummyUpdate(callback_data="mp_stats_7")))

    def test_doorway_commands_still_work(self):
        for command in ("/debut", "/d", "/start", "/membership", "/help",
                        "/howto", "/redeem ABC123", "/feedback", "/ping"):
            self.assertFalse(_block(DummyUpdate(command)), command)

    def test_doorway_buttons_still_work(self):
        # The Skip button on the referral prompt /debut itself puts up.
        self.assertFalse(_block(DummyUpdate(callback_data="refcode_skip_12")))

    def test_the_referral_code_typed_after_debut_still_gets_through(self):
        # /debut asks the new user to type their code; the gate must not eat
        # the answer to a question it just allowed the bot to ask.
        self.assertFalse(_block(DummyUpdate("AB12CD"), allow_text=True))

    def test_the_group_welcome_for_a_new_member_still_fires(self):
        # Somebody joining a group is an event about the chat, not a player
        # using the bot — and the welcome it triggers is what tells them to
        # run /debut in the first place.
        update = DummyUpdate("")
        update.message.new_chat_members = [SimpleNamespace(id=999)]
        self.assertFalse(_block(update))

    def test_updates_that_are_not_messages_are_left_alone(self):
        # my_chat_member / chat_member updates (the bot added to a group) carry
        # no message and no callback; blocking them would break chat tracking.
        update = DummyUpdate()
        self.assertFalse(_block(update))

    def test_a_command_addressed_to_another_bot_is_not_ours_to_answer(self):
        update = DummyUpdate("/claim@SomeOtherBot")
        self.assertTrue(_block(update))          # still stopped…
        self.assertFalse(rookie_gate.should_reply(update))   # …but silently

    def test_a_command_addressed_to_this_bot_is_handled_normally(self):
        self.assertFalse(_block(DummyUpdate("/debut@CricketCardBot"),
                                bot_username="CricketCardBot"))
        self.assertTrue(_block(DummyUpdate("/claim@CricketCardBot"),
                               bot_username="CricketCardBot"))

    def test_miniapp_is_locked(self):
        for path in ("/api/webapp/claim", "/api/webapp/match/state",
                     "/api/fantasy/squad", "/api/ipl16/start"):
            self.assertTrue(rookie_gate.should_block_miniapp_path(
                path, has_membership=False, cfg=ROOKIE_ON), path)

    def test_non_miniapp_paths_are_none_of_the_gates_business(self):
        for path in ("/dashboard", "/api/health", "/webapp/player-card/3"):
            self.assertFalse(rookie_gate.should_block_miniapp_path(
                path, has_membership=False, cfg=ROOKIE_ON), path)

    def test_ad_postbacks_stay_open(self):
        # An ad network reporting a finished ad is not a player using the app;
        # dropping it would destroy a reward that was already earned.
        for path in ("/api/ads/reward", "/api/adsgram/reward"):
            self.assertFalse(rookie_gate.should_block_miniapp_path(
                path, has_membership=False, cfg=ROOKIE_ON), path)


class MemberTests(unittest.TestCase):
    """ON + any active membership = the bot behaves exactly as before."""

    def test_every_tier_passes_the_gate(self):
        from services.subscription_service import VALID_TIERS
        for tier in VALID_TIERS:
            self.assertTrue(rookie_gate.has_access(_member(tier)), tier)

    def test_free_and_unknown_users_do_not(self):
        self.assertFalse(rookie_gate.has_access(_member("none")))
        self.assertFalse(rookie_gate.has_access(None))

    def test_an_expired_membership_locks_the_bot_again(self):
        lapsed = _member("diamond", days_left=-1)
        self.assertFalse(rookie_gate.has_access(lapsed))

    def test_members_are_never_blocked(self):
        for update in (DummyUpdate("/playmatch"), DummyUpdate("chatter"),
                       DummyUpdate(callback_data="market_buy_3")):
            self.assertFalse(_block(update, has_membership=True))

    def test_members_reach_the_miniapp(self):
        self.assertFalse(rookie_gate.should_block_miniapp_path(
            "/api/webapp/claim", has_membership=True, cfg=ROOKIE_ON))

    def test_admins_bypass_the_gate(self):
        with patch.object(rookie_gate, "is_bypassed", return_value=True):
            self.assertFalse(rookie_gate.should_block_update(
                DummyUpdate("/playmatch"), has_membership=False,
                cfg=ROOKIE_ON))
            self.assertFalse(rookie_gate.should_block_miniapp_path(
                "/api/webapp/claim", has_membership=False, telegram_id=1,
                cfg=ROOKIE_ON))

    def test_a_broken_tier_lookup_fails_open(self):
        # A membership check that explodes must not lock every paying player
        # out of the bot.
        broken = SimpleNamespace()
        with patch("services.subscription_service.has_tier_at_least",
                   side_effect=RuntimeError("boom")):
            self.assertTrue(rookie_gate.has_access(broken))


class MessagingTests(unittest.TestCase):
    def test_the_default_message_quotes_the_live_price(self):
        from config import SUBSCRIPTION_TIERS
        msg = rookie_gate.rookie_required_message(ROOKIE_ON)
        self.assertIn(str(SUBSCRIPTION_TIERS["rookie"]["price_inr"]), msg)
        self.assertIn("/membership", msg)

    def test_a_brand_new_user_is_pointed_at_debut_first(self):
        msg = rookie_gate.rookie_required_message(ROOKIE_ON, is_new_user=True)
        self.assertIn("/debut", msg)

    def test_an_admin_message_replaces_the_default(self):
        cfg = dict(ROOKIE_ON, rookie_message="🔒 Buy Rookie, please.")
        self.assertEqual(rookie_gate.rookie_required_message(cfg),
                         "🔒 Buy Rookie, please.")

    def test_the_alert_version_carries_no_markup(self):
        # Telegram alerts and Mini App toasts render text, not HTML.
        alert = rookie_gate.rookie_required_alert()
        self.assertNotIn("<b>", alert)
        self.assertIn("/membership", alert)


class DoorwayCommandsAreRealTests(unittest.TestCase):
    """Every doorway command must actually be registered in bot.py.

    A renamed alias would otherwise lock the front door: the gate would let
    /debut through while the bot answered to something else. Parses bot.py
    rather than importing it (see tests/test_bot_menu_commands.py).
    """

    def test_every_free_command_has_a_handler(self):
        src = (Path(__file__).resolve().parent.parent / "bot.py").read_text()
        registered = set()
        for entry in re.findall(
                r'CommandHandler\(\s*(\[[^\]]*\]|"[a-z0-9_]+")', src):
            registered.update(re.findall(r'"([a-z0-9_]+)"', entry)
                              if entry.startswith("[") else [entry.strip('"')])
        missing = sorted(rookie_gate.FREE_COMMANDS - registered)
        self.assertFalse(missing, (
            f"rookie_gate.FREE_COMMANDS names commands bot.py does not "
            f"register: {missing}"))

    def test_debut_and_membership_are_always_reachable(self):
        # The two the upsell itself tells a locked-out player to use.
        for command in ("debut", "membership"):
            self.assertTrue(rookie_gate.is_free_command(command), command)

    def test_the_gate_runs_after_the_ban_and_maintenance_middleware(self):
        """"You are banned" and "the bot is down" must win over the upsell."""
        src = (Path(__file__).resolve().parent.parent / "bot.py").read_text()
        groups = {}
        for handler, group in re.findall(
                r"TypeHandler\(_TGUpdate,\s*(\w+)\),\s*group=(-?\d+)", src):
            groups[handler] = int(group)
        self.assertIn("_rookie_check", groups)
        self.assertGreater(groups["_rookie_check"], groups["_ban_check"])
        self.assertGreater(groups["_rookie_check"], groups["_maintenance_check"])
        # …and before every command handler, which all live in group 0+.
        self.assertLess(groups["_rookie_check"], 0)

    def test_each_middleware_has_a_group_of_its_own(self):
        # PTB runs only the FIRST matching handler per group — two middlewares
        # sharing a group would silently disable one of them.
        src = (Path(__file__).resolve().parent.parent / "bot.py").read_text()
        groups = [int(g) for _h, g in re.findall(
            r"TypeHandler\(_TGUpdate,\s*(\w+)\),\s*group=(-?\d+)", src)]
        self.assertEqual(len(groups), len(set(groups)),
                         "two middlewares share a handler group")


class ConfigDefaultsTests(unittest.TestCase):
    def test_rookie_mode_ships_off(self):
        # An existing deployment must not wake up locked down.
        from services.config_service import DEFAULTS
        self.assertIs(DEFAULTS["rookie_mode"], False)
        self.assertIsNone(DEFAULTS["rookie_message"])

    def test_the_columns_are_migrated_in(self):
        src = (Path(__file__).resolve().parent.parent / "database.py").read_text()
        for column in ("rookie_mode", "rookie_message"):
            self.assertIn(f'"game_config", "{column}"', src,
                          f"{column} has no _try_add migration")

    def test_the_model_declares_them(self):
        from models import GameConfig
        for column in ("rookie_mode", "rookie_message"):
            self.assertIn(column, GameConfig.__table__.columns)


class CommandTokenParsingTests(unittest.TestCase):
    """The shared parser both gates read commands with."""

    def test_command_name_strips_the_slash_and_the_bot_suffix(self):
        from services.command_token import command_name
        self.assertEqual(command_name("/Claim@MyBot"), "claim")
        self.assertEqual(command_name("/daily"), "daily")
        self.assertEqual(command_name("hello"), "")

    def test_maintenance_and_rookie_read_the_same_update(self):
        from services.maintenance_service import is_command_update
        from services.command_token import command_name_from_update
        update = DummyUpdate("/claim now")
        self.assertTrue(is_command_update(update))
        self.assertEqual(command_name_from_update(update), "claim")

    def test_entities_are_honoured_when_the_text_is_not_normalized(self):
        from services.command_token import command_name_from_update
        update = DummyUpdate("  /debut")
        update.message.text = "  /debut"
        update.message.entities = [SimpleNamespace(type="bot_command",
                                                   offset=2, length=6)]
        self.assertEqual(command_name_from_update(update), "debut")


class AdminPanelWiringTests(unittest.TestCase):
    """The switch has to be reachable from the website, or the feature is
    code with no way to turn it on."""

    def test_the_maintenance_route_handles_the_rookie_actions(self):
        src = (Path(__file__).resolve().parent.parent / "admin.py").read_text()
        for action in ("rookie_enable", "rookie_disable", "rookie_save_message"):
            self.assertIn(f'"{action}"', src)

    def test_the_template_posts_those_actions(self):
        template = (Path(__file__).resolve().parent.parent
                    / "templates" / "admin_maintenance.html").read_text()
        for action in ("rookie_enable", "rookie_disable", "rookie_save_message"):
            self.assertIn(f'value="{action}"', template)

    def test_the_miniapp_hook_is_registered(self):
        src = (Path(__file__).resolve().parent.parent / "admin.py").read_text()
        tree = ast.parse(src)
        hooks = [node.name for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef)
                 and any(getattr(d, "attr", None) == "before_request"
                         for d in node.decorator_list)]
        self.assertIn("_block_miniapp_without_membership", hooks)


if __name__ == "__main__":
    unittest.main()

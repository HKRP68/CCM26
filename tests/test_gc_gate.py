"""Tests for the forced Official GC join gate (services/gc_gate.py).

ON, a non-member reaches nothing but the doorway commands and the verify
button. OFF (or no group configured), the gate is invisible. A failed
membership lookup never locks anyone out.
"""

import asyncio
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services import gc_gate

GID = -1001234567890
ON = {"force_gc_join": True, "official_group_id": GID,
      "official_group_link": "https://t.me/cmugames"}
OFF = {"force_gc_join": False, "official_group_id": GID}
NO_GROUP = {"force_gc_join": True, "official_group_id": None}


class _Msg:
    def __init__(self, text, chat_type="private"):
        self.text = text
        self.entities = []
        self.chat = SimpleNamespace(type=chat_type)


class _Update:
    def __init__(self, text=None, callback=None, chat_id=555, chat_type="private",
                 user_id=42, is_bot=False):
        self.message = _Msg(text, chat_type) if text is not None else None
        self.callback_query = (SimpleNamespace(data=callback)
                               if callback is not None else None)
        self.effective_user = SimpleNamespace(id=user_id, is_bot=is_bot)
        self.effective_chat = SimpleNamespace(id=chat_id, type=chat_type)


def _blocks(update, is_member=False, cfg=ON, **kw):
    with patch.object(gc_gate, "is_bypassed", return_value=False):
        return gc_gate.should_block_update(update, is_member=is_member, cfg=cfg, **kw)


class ModeTests(unittest.TestCase):
    def test_off_or_unconfigured_never_blocks(self):
        for cfg in (OFF, NO_GROUP, {}):
            self.assertFalse(_blocks(_Update("/claim"), cfg=cfg), cfg)

    def test_active_needs_switch_and_group(self):
        self.assertTrue(gc_gate.is_gc_gate_active(ON))
        self.assertFalse(gc_gate.is_gc_gate_active(OFF))
        self.assertFalse(gc_gate.is_gc_gate_active(NO_GROUP))


class BlockingTests(unittest.TestCase):
    def test_non_member_command_is_blocked(self):
        self.assertTrue(_blocks(_Update("/claim")))

    def test_member_is_never_blocked(self):
        self.assertFalse(_blocks(_Update("/claim"), is_member=True))

    def test_failed_lookup_fails_open(self):
        self.assertFalse(_blocks(_Update("/claim"), is_member=None))

    def test_doorway_commands_pass(self):
        for cmd in ("/start", "/debut", "/d", "/help", "/feedback", "/commands"):
            self.assertFalse(_blocks(_Update(cmd)), cmd)

    def test_verify_button_passes_other_buttons_blocked(self):
        self.assertFalse(_blocks(_Update(callback="gcjoin_check")))
        self.assertTrue(_blocks(_Update(callback="xi_view_1")))

    def test_anything_inside_the_official_gc_passes(self):
        self.assertFalse(_blocks(_Update("/claim", chat_id=GID, chat_type="supergroup")))

    def test_bypassed_admin_passes(self):
        with patch.object(gc_gate, "is_bypassed", return_value=True):
            self.assertFalse(gc_gate.should_block_update(
                _Update("/claim"), is_member=False, cfg=ON))

    def test_service_messages_and_non_messages_pass(self):
        upd = _Update("hi", chat_type="group")
        upd.message.new_chat_members = [object()]
        self.assertFalse(_blocks(upd))
        self.assertFalse(_blocks(_Update()))

    def test_referral_code_reply_passes_only_when_awaited(self):
        self.assertFalse(_blocks(_Update("ABCDEF"), allow_text=True))
        self.assertTrue(_blocks(_Update("ABCDEF")))

    def test_chatter_is_blocked_but_silently(self):
        upd = _Update("hello there", chat_type="group")
        self.assertTrue(_blocks(upd))
        self.assertFalse(gc_gate.should_reply(upd))
        self.assertTrue(gc_gate.should_reply(_Update("/claim")))


class MembershipTests(unittest.TestCase):
    def _check(self, member=None, exc=None):
        class Bot:
            async def get_chat_member(self, gid, uid):
                if exc:
                    raise exc
                return member
        return asyncio.run(gc_gate.check_membership(Bot(), GID, 1))

    def test_statuses(self):
        for status, expected in (("member", True), ("administrator", True),
                                 ("creator", True), ("left", False), ("kicked", False)):
            self.assertEqual(self._check(SimpleNamespace(status=status)), expected, status)

    def test_restricted_counts_only_while_member(self):
        self.assertTrue(self._check(SimpleNamespace(status="restricted", is_member=True)))
        self.assertFalse(self._check(SimpleNamespace(status="restricted", is_member=False)))

    def test_user_not_found_is_not_a_member(self):
        self.assertFalse(self._check(exc=Exception("Bad Request: user not found")))

    def test_other_errors_fail_open(self):
        self.assertIsNone(self._check(exc=Exception("Bad Request: chat not found")))


class MessagingTests(unittest.TestCase):
    def test_join_url_forms(self):
        self.assertEqual(gc_gate.join_url({"official_group_link": "@cmu"}), "https://t.me/cmu")
        self.assertEqual(gc_gate.join_url({"official_group_link": "t.me/+abc"}), "https://t.me/+abc")
        self.assertEqual(gc_gate.join_url({"branding_group_username": "cmu"}), "https://t.me/cmu")
        self.assertIsNone(gc_gate.join_url({}))

    def test_custom_message_wins(self):
        self.assertEqual(gc_gate.join_required_message(dict(ON, gc_join_message="Join!")), "Join!")

    def test_default_message_escapes_name(self):
        text = gc_gate.join_required_message(ON, first_name="<b>x</b>")
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", text)
        self.assertIn("@cmugames", text)

    def test_keyboard_has_join_and_verify(self):
        kb = gc_gate.join_keyboard(ON)
        flat = [b for row in kb.inline_keyboard for b in row]
        self.assertEqual(flat[0].url, "https://t.me/cmugames")
        self.assertEqual(flat[-1].callback_data, gc_gate.VERIFY_CALLBACK)


class WiringTests(unittest.TestCase):
    SRC = (Path(__file__).resolve().parent.parent / "bot.py").read_text()

    def test_every_free_command_has_a_handler(self):
        registered = set()
        for entry in re.findall(r'CommandHandler\(\s*(\[[^\]]*\]|"[a-z0-9_]+")', self.SRC):
            registered.update(re.findall(r'"([a-z0-9_]+)"', entry)
                              if entry.startswith("[") else [entry.strip('"')])
        missing = sorted(gc_gate.FREE_COMMANDS - registered)
        self.assertFalse(missing, f"gc_gate.FREE_COMMANDS not registered: {missing}")

    def test_gate_runs_after_ban_and_before_rookie(self):
        def group_of(name):
            m = re.search(rf"TypeHandler\(_TGUpdate, {name}\), group=(-?\d+)", self.SRC)
            self.assertIsNotNone(m, name)
            return int(m.group(1))
        gc = group_of("_gc_check")
        self.assertGreater(gc, group_of("_ban_check"))
        self.assertGreater(gc, group_of("_maintenance_check"))
        self.assertLess(gc, group_of("_rookie_check"))

    def test_verify_callback_is_registered(self):
        self.assertIn('pattern=r"^gcjoin_check$"', self.SRC)


if __name__ == "__main__":
    unittest.main()

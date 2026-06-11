"""Tests for /wpm overs+invite parsing (up to 20 overs, optional directed target).

These cover handlers.match._parse_overs_and_target, which splits the args into
an over count and the leftover tokens it hands to resolve_command_target so a
player can be invited by @mention / id while the numeric overs token is ignored.
"""

import unittest

import handlers.match as bm


class _FakeUser:
    def __init__(self, uid, tg_id):
        self.id = uid
        self.telegram_id = tg_id


class ParseOversAndTargetTests(unittest.TestCase):
    def setUp(self):
        self._orig = bm.resolve_command_target
        self.captured = []

        def _fake_resolve(session, update, shim, command_name):
            # Capture what the resolver was asked to look at, then echo a target
            # when a mention-ish token is present so we can assert wiring.
            self.captured.append((list(shim.args), command_name))
            for tok in shim.args:
                if tok.startswith("@"):
                    return _FakeUser(99, 12345), "username"
            return None, "missing"

        bm.resolve_command_target = _fake_resolve

    def tearDown(self):
        bm.resolve_command_target = self._orig

    def _call(self, args):
        ctx = type("C", (), {"args": args})()
        return bm._parse_overs_and_target(None, None, ctx, "wpm")

    def test_default_overs_no_args(self):
        overs, explicit, target, _ = self._call([])
        self.assertEqual(overs, 1)
        self.assertFalse(explicit)
        self.assertIsNone(target)
        # Empty args reach the resolver so reply/text-mention fallbacks still work.
        self.assertEqual(self.captured[-1], ([], "wpm"))

    def test_explicit_overs_only(self):
        overs, explicit, target, _ = self._call(["20"])
        self.assertEqual(overs, 20)
        self.assertTrue(explicit)
        self.assertIsNone(target)
        # The numeric overs token is consumed, not forwarded as a target.
        self.assertEqual(self.captured[-1][0], [])

    def test_overs_then_mention(self):
        overs, explicit, target, reason = self._call(["20", "@user2"])
        self.assertEqual(overs, 20)
        self.assertTrue(explicit)
        self.assertEqual(target.id, 99)
        self.assertEqual(reason, "username")
        self.assertEqual(self.captured[-1][0], ["@user2"])

    def test_mention_then_overs_order_independent(self):
        overs, explicit, target, _ = self._call(["@user2", "5"])
        self.assertEqual(overs, 5)
        self.assertTrue(explicit)
        self.assertEqual(target.id, 99)
        self.assertEqual(self.captured[-1][0], ["@user2"])

    def test_mention_only_defaults_overs(self):
        overs, explicit, target, _ = self._call(["@user2"])
        self.assertEqual(overs, 1)
        self.assertFalse(explicit)
        self.assertEqual(target.id, 99)

    def test_only_first_number_is_overs(self):
        # A trailing number is treated as a leftover token, not a second overs.
        overs, _, _, _ = self._call(["12", "7"])
        self.assertEqual(overs, 12)
        self.assertEqual(self.captured[-1][0], ["7"])


if __name__ == "__main__":
    unittest.main()

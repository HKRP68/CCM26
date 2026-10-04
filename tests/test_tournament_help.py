"""/tourhelp — the one card listing every tournament command.

Pins down that every command the card names is really registered in bot.py
(so the card can't drift as commands are renamed or retired), that the admin
sections are for admins only, and that the card is well-formed HTML that fits
Telegram's message limit.
"""

import os
import re
import unittest
from html.parser import HTMLParser

from handlers import tournament_help as th
from utils.message_chunks import chunk_blocks

_BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "bot.py")


def _registered():
    src = open(_BOT, encoding="utf-8").read()
    names = set()
    for entry in re.findall(r'CommandHandler\(\s*(\[[^\]]*\]|"[a-z0-9_]+")', src):
        names.update(re.findall(r'"([a-z0-9_]+)"', entry) if entry.startswith("[")
                     else [entry.strip('"')])
    return names


class _Balance(HTMLParser):
    """Fails on an unclosed or mismatched tag — what Telegram rejects."""

    def __init__(self):
        super().__init__()
        self.stack = []

    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack[-1] == tag, f"mismatched </{tag}>"
        self.stack.pop()


class TourHelpTests(unittest.TestCase):
    def test_every_command_on_the_card_is_registered(self):
        missing = sorted(set(th.all_commands()) - _registered())
        self.assertEqual(missing, [], f"on /tourhelp but not in bot.py: {missing}")

    def test_tourhelp_itself_is_registered(self):
        self.assertTrue({"tourhelp", "thelp", "tournamenthelp"} <= _registered())

    def test_players_do_not_see_the_admin_sections(self):
        text = "\n".join(th.render_help(admin=False))
        self.assertNotIn("🔒", text)
        self.assertNotIn("/tsim", text)
        self.assertIn("/lptour", text)
        self.assertIn("/ctfixtures", text)

    def test_admins_see_everything(self):
        text = "\n".join(th.render_help(admin=True))
        for cmd in ("/tsim", "/tpoints", "/lptschedule", "/dadmin", "/lptour"):
            self.assertIn(cmd, text)

    def test_the_leagues_own_command_is_named_when_one_is_set(self):
        text = "\n".join(th.render_help(cipl_command="tipl"))
        self.assertIn("<code>/tipl</code>", text)
        self.assertNotIn("your league's tournament command",
                         "\n".join(th.render_help(cipl_command="tipl")))
        self.assertIn("your league's tournament command",
                      "\n".join(th.render_help()))

    def test_the_card_is_well_formed_and_fits_a_message(self):
        for admin in (False, True):
            for part in chunk_blocks(th.render_help(admin=admin)):
                self.assertLessEqual(len(part), 4096)
                parser = _Balance()
                parser.feed(part)
                parser.close()
                self.assertEqual(parser.stack, [])


if __name__ == "__main__":
    unittest.main()

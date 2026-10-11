"""/matchguide — the complete match & tournament guide.

Pins down that every command the guide names is really registered (so the
guide can't drift as commands are renamed or retired), that every topic resolves
and renders to well-formed HTML Telegram will accept, that the command itself is
registered, and that the committed PDF was rebuilt after the Markdown changed.
"""

import os
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

from handlers import match_guide as mg

ROOT = Path(__file__).resolve().parent.parent

# Commands that exist without a literal CommandHandler: the Challenge League
# family is a regex MessageHandler over whatever leagues are configured, and a
# league tournament's commands are named per league on the website (the guide
# uses /tipl as its worked example).
DYNAMIC = re.compile(r"^(cipl|cbbl|cint|cmulti|challenge\w*|tipl\w*)$", re.I)


def _registered():
    names = set()
    for path in [ROOT / "bot.py", *sorted((ROOT / "handlers").glob("*.py"))]:
        src = path.read_text(encoding="utf-8")
        for entry in re.findall(r'CommandHandler\(\s*(\[[^\]]*\]|"[a-z0-9_]+")', src):
            names.update(re.findall(r'"([a-z0-9_]+)"', entry)
                         if entry.startswith("[") else [entry.strip('"')])
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


class MatchGuideTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = mg.load_guide()
        cls.sections = mg.parse_sections(cls.text)

    def test_every_command_in_the_guide_is_registered(self):
        named = set(re.findall(r"(?<![\w.])/([a-z][a-z0-9_]*)", self.text, flags=re.I))
        missing = sorted(c for c in named
                         if c.lower() not in _registered() and not DYNAMIC.match(c))
        self.assertEqual(missing, [], f"in the guide but not registered: {missing}")

    def test_matchguide_itself_is_registered(self):
        self.assertTrue({"matchguide", "mguide", "fullguide", "matchtypes"}
                        <= _registered())

    def test_every_section_has_a_unique_topic_key(self):
        self.assertGreaterEqual(len(self.sections), 25)
        keys = [k for s in self.sections for k in s["keys"]]
        self.assertEqual(len(keys), len(set(keys)), "a topic key is used twice")

    def test_the_contents_table_matches_the_sections(self):
        listed = re.findall(r"^\| \d+ \| .*?\| `([a-z]+)` \|$", self.text, flags=re.M)
        self.assertEqual(listed, [s["keys"][0] for s in self.sections])

    def test_topics_resolve_by_key_number_prefix_and_command(self):
        find = lambda q: mg.find_section(self.sections, q)["keys"][0]
        self.assertEqual(find("letsplay"), "letsplay")
        self.assertEqual(find("LP"), "letsplay")
        self.assertEqual(find("12"), "sim")
        self.assertEqual(find("t10"), "sim")
        self.assertEqual(find("/lptour"), "lpt")
        self.assertEqual(find("simtolast"), "rcpl")
        self.assertIsNone(mg.find_section(self.sections, "nonsense-topic"))
        self.assertIsNone(mg.find_section(self.sections, ""))

    def test_every_section_renders_to_valid_html_within_the_limit(self):
        for section in self.sections:
            for part in mg.render_section(section):
                self.assertLessEqual(len(part), 4096, section["title"])
                parser = _Balance()
                parser.feed(part)
                parser.close()
                self.assertEqual(parser.stack, [], section["title"])

    def test_the_topic_list_is_valid_html(self):
        parser = _Balance()
        parser.feed(mg.topic_list_text(self.sections))
        parser.close()
        self.assertEqual(parser.stack, [])

    def test_the_markdown_is_escaped_in_chat(self):
        html_out = "".join(mg.render_blocks(["Use `/sim <overs>` & **win**"]))
        self.assertIn("<code>/sim &lt;overs&gt;</code>", html_out)
        self.assertIn("&amp;", html_out)
        self.assertIn("<b>win</b>", html_out)

    def test_the_pdf_is_built_from_the_current_markdown(self):
        self.assertTrue(os.path.exists(mg.GUIDE_PDF), "run tools/make_match_guide.py")
        with open(mg.GUIDE_PDF, "rb") as fh:
            self.assertEqual(fh.read(5), b"%PDF-")
        # Git does not keep mtimes, so a fresh checkout cannot tell which file
        # is newer; only compare when the Markdown is visibly edited after it.
        if os.path.getmtime(mg.GUIDE_MD) - os.path.getmtime(mg.GUIDE_PDF) > 60:
            try:
                import subprocess
                dirty = subprocess.run(
                    ["git", "status", "--porcelain", mg.GUIDE_MD],
                    cwd=ROOT, capture_output=True, text=True).stdout.strip()
            except Exception:
                dirty = ""
            self.assertFalse(dirty, "the guide changed — re-run "
                                    "tools/make_match_guide.py")


if __name__ == "__main__":
    unittest.main()

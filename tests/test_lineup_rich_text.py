"""The Playing XI and bench, rendered as Bot API 10.1 rich blocks.

Two renderers now describe the same XI: the HTML string in ``format_xi_text``
and the block tree in ``build_xi_blocks``. They must agree on the parts a user
acts on — which section a card sits in, and the 1-N display number typed at
/swap and /release — so the tests below read both and compare, rather than
pinning the block tree on its own.

The rest guards the transport: a rich send that Telegram refuses has to come
out as the old HTML message, and a server with no ``sendRichMessage`` at all
must be asked only once.
"""

import asyncio
import json
import os
import re
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# handlers.lineup imports database, which builds an engine at import time. This
# module never reads a row, but it must not create a stray DB in the repo root.
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP.name}")

import config  # noqa: E402
from handlers import lineup  # noqa: E402
from services import rich_message  # noqa: E402
from services.fancy_text import bold_digits  # noqa: E402


def tearDownModule():
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


def _pair(rid, name, category="Batsman", bowl_style="Fast", rating=80):
    """A stand-in ``(UserRoster, Player)`` pair — only display fields are read."""
    return (SimpleNamespace(id=rid),
            SimpleNamespace(name=name, country="India", rating=rating,
                            bat_rating=rating - 10, bowl_rating=rating - 20,
                            category=category, bowl_style=bowl_style))


def _squad(bench=0):
    """An XI whose categories land in all four sections, plus optional bench.

    The bowlers are deliberately interleaved spin/pace so the pacers-before-
    spinners rule inside the bowling section has something to reorder.
    """
    roster = [
        _pair(1, "Opener One"), _pair(2, "Opener Two"), _pair(3, "Number Three"),
        _pair(4, "Number Four"), _pair(5, "Number Five"),
        _pair(6, "The Keeper", "Wicket Keeper"),
        _pair(7, "The Allrounder", "All-rounder"),
        _pair(8, "Spinner One", "Bowler", "Off Spin"),
        _pair(9, "Pacer One", "Bowler", "Fast"),
        _pair(10, "Spinner Two", "Bowler", "Leg Spin"),
        _pair(11, "Pacer Two", "Bowler", "Fast-medium"),
    ]
    roster += [_pair(12 + i, f"Bench {i + 1}") for i in range(bench)]
    return roster


def _tables(blocks):
    return [b for b in blocks if b["type"] == "table"]


def _player_rows(table):
    """Rows that describe a player, i.e. not the header and not a section row."""
    return [row for row in table["cells"][1:] if len(row) > 1]


def _cell_text(cell):
    """Flatten a cell's RichText back to plain text."""
    def walk(node):
        if isinstance(node, str):
            return node
        if isinstance(node, list):
            return "".join(walk(n) for n in node)
        return walk(node.get("text", ""))
    return walk(cell.get("text", ""))


class XiBlockStructureTests(unittest.TestCase):

    def test_xi_is_one_table_with_header_and_section_rows(self):
        blocks = lineup.build_xi_blocks(_squad(), "@tester")
        self.assertEqual(blocks[0]["type"], "heading")

        tables = _tables(blocks)
        self.assertEqual(len(tables), 1, "the XI should be a single table")
        cells = tables[0]["cells"]

        # 1 header + 4 section rows + 11 players.
        self.assertEqual(len(cells), 16)
        self.assertTrue(all(c.get("is_header") for c in cells[0]))
        section_rows = [r for r in cells if len(r) == 1]
        self.assertEqual([_cell_text(r[0]) for r in section_rows],
                         ["🏏 BATSMEN", "🧤 WICKET-KEEPER",
                          "⚡ ALL-ROUNDERS", "🎯 BOWLERS"])
        # A section row has to span the table or the columns shear.
        for row in section_rows:
            self.assertEqual(row[0]["colspan"], lineup._XI_TABLE_COLUMNS)

    def test_every_cell_carries_required_alignment(self):
        blocks = lineup.build_xi_blocks(_squad(bench=3), "@tester",
                                        show_bench=True)
        seen = 0
        for table in _tables(blocks) + _tables(
                [b for block in blocks if block["type"] == "details"
                 for b in block["blocks"]]):
            for row in table["cells"]:
                for cell in row:
                    # align and valign are required by the API, not optional.
                    self.assertIn(cell["align"], ("left", "center", "right"))
                    self.assertIn(cell["valign"], ("top", "middle", "bottom"))
                    seen += 1
        self.assertGreater(seen, 0)

    def test_blocks_are_json_serialisable(self):
        blocks = lineup.build_xi_blocks(_squad(bench=2), "@tester",
                                        captain_rid=7, show_bench=True)
        self.assertEqual(json.loads(json.dumps(blocks)), blocks)

    def test_captain_crown_on_exactly_the_captain(self):
        blocks = lineup.build_xi_blocks(_squad(), "@tester", captain_rid=6)
        crowned = [_cell_text(row[1]) for row in _player_rows(_tables(blocks)[0])
                   if "👑" in _cell_text(row[1])]
        self.assertEqual(len(crowned), 1)
        self.assertIn("ᴛʜᴇ ᴋᴇᴇᴘᴇʀ", crowned[0])

    def test_partial_squad_renders_without_chemistry(self):
        # chemistry.xi_summary returns None below a full XI; the block tree must
        # cope rather than index into it.
        blocks = lineup.build_xi_blocks(_squad()[:4], "@tester")
        self.assertEqual(len(_player_rows(_tables(blocks)[0])), 4)
        self.assertFalse(any("CHEMISTRY" in json.dumps(b, ensure_ascii=False)
                             for b in blocks))

    def test_empty_roster_still_builds(self):
        blocks = lineup.build_xi_blocks([], "@tester")
        self.assertEqual(_tables(blocks)[0]["cells"], [lineup._rich_stat_header()])


class RendererAgreementTests(unittest.TestCase):
    """The block tree and the HTML string must not drift apart."""

    def _html_order(self, text):
        """Player names in display order, read out of the HTML rendering."""
        return re.findall(r"[①-⑳]\s(\S[^ ]*(?: \S+)*?)\s\s", text)

    def test_display_order_and_numbering_match_html(self):
        roster = _squad(bench=2)
        blocks = lineup.build_xi_blocks(roster, "@tester")
        rows = _player_rows(_tables(blocks)[0])

        # Serials run 1..11 with no gaps, continuous across sections.
        self.assertEqual([_cell_text(r[0]) for r in rows],
                         [str(i) for i in range(1, 12)])

        rich_names = [_cell_text(r[1]).split("  ")[0] for r in rows]
        html_names = self._html_order(
            lineup.format_xi_text(roster, "@tester"))[:11]
        self.assertEqual(rich_names, html_names)

    def test_bench_numbering_continues_the_xi_in_both_renderers(self):
        roster = _squad(bench=3)
        details = lineup.build_bench_details(roster)
        rows = _player_rows(details["blocks"][0])
        self.assertEqual([_cell_text(r[0]) for r in rows], ["12", "13", "14"])

        html = lineup.format_bench_text(roster)
        for serial in ("⑫", "⑬", "⑭"):
            self.assertIn(serial, html)

    def test_totals_match_html(self):
        roster = _squad()
        blocks = lineup.build_xi_blocks(roster, "@tester")
        total = sum(p.rating for _e, p in roster)
        flat = json.dumps(blocks, ensure_ascii=False)
        self.assertIn(f"TOTAL OVR: {total}", flat)
        # The HTML renderer stylises its digits; the table uses plain ones so
        # the numeric columns line up.
        self.assertIn(bold_digits(total), lineup.format_xi_text(roster, "@tester"))


class BenchDetailsTests(unittest.TestCase):

    def test_no_bench_means_no_block(self):
        self.assertIsNone(lineup.build_bench_details(_squad()))

    def test_bench_is_collapsed_by_default(self):
        details = lineup.build_bench_details(_squad(bench=2))
        self.assertEqual(details["type"], "details")
        self.assertNotIn("is_open", details)
        self.assertIn("📋 BENCH (2)", json.dumps(details, ensure_ascii=False))

    def test_xi_embeds_bench_only_when_asked(self):
        roster = _squad(bench=2)
        without = lineup.build_xi_blocks(roster, "@tester", show_bench=False)
        with_bench = lineup.build_xi_blocks(roster, "@tester", show_bench=True)
        self.assertFalse(any(b["type"] == "details" for b in without))
        self.assertTrue(any(b["type"] == "details" for b in with_bench))


class _FakeBot:
    """Records ``_post`` and ``send_message`` calls; ``_post`` can be made to fail."""

    def __init__(self, post_error=None):
        self.post_error = post_error
        self.posts = []
        self.send_message = AsyncMock(return_value="html-message")

    async def _post(self, endpoint, payload):
        self.posts.append((endpoint, payload))
        if self.post_error is not None:
            raise self.post_error
        return {"message_id": 1, "date": 0,
                "chat": {"id": 42, "type": "private"}}


class RichSendTests(unittest.TestCase):

    def setUp(self):
        rich_message.reset_support_latch()
        self.addCleanup(rich_message.reset_support_latch)
        self.blocks = lineup.build_xi_blocks(_squad(), "@tester")

    def _send(self, bot, **kwargs):
        return asyncio.run(rich_message.send_rich_message(
            bot, 42, self.blocks, "<b>fallback</b>", **kwargs))

    def test_posts_blocks_when_enabled(self):
        bot = _FakeBot()
        with patch.object(config, "RICH_TEXT_ENABLED", True):
            self._send(bot)
        self.assertEqual(len(bot.posts), 1)
        endpoint, payload = bot.posts[0]
        self.assertEqual(endpoint, "sendRichMessage")
        self.assertEqual(payload["rich_message"]["blocks"], self.blocks)
        # Exactly one of html/markdown/blocks may be set.
        self.assertEqual(set(payload["rich_message"]), {"blocks"})
        bot.send_message.assert_not_called()

    def test_disabled_sends_html_without_touching_the_endpoint(self):
        bot = _FakeBot()
        with patch.object(config, "RICH_TEXT_ENABLED", False):
            self._send(bot)
        self.assertEqual(bot.posts, [])
        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.await_args.kwargs["parse_mode"], "HTML")
        self.assertEqual(bot.send_message.await_args.args[1], "<b>fallback</b>")

    def test_refused_send_falls_back_to_html(self):
        from telegram.error import BadRequest
        bot = _FakeBot(post_error=BadRequest("Bad Request: block is invalid"))
        with patch.object(config, "RICH_TEXT_ENABLED", True):
            self._send(bot)
        bot.send_message.assert_awaited_once()
        # A bad payload is this bot's bug, not a missing endpoint: keep trying,
        # so the logs keep naming it.
        self.assertTrue(rich_message.rich_text_enabled())

    def test_missing_method_latches_off_after_one_attempt(self):
        # PTB maps the 404 for an unknown method onto InvalidToken.
        from telegram.error import InvalidToken
        bot = _FakeBot(post_error=InvalidToken("Not Found: method not found"))
        with patch.object(config, "RICH_TEXT_ENABLED", True):
            self._send(bot)
            self.assertFalse(rich_message.rich_text_enabled())
            self._send(bot)
        self.assertEqual(len(bot.posts), 1, "should not retry a missing method")
        self.assertEqual(bot.send_message.await_count, 2)

    def test_reply_markup_and_quote_ride_along(self):
        bot = _FakeBot()
        with patch.object(config, "RICH_TEXT_ENABLED", True):
            self._send(bot, reply_markup="kb", reply_to_message_id=7)
        _endpoint, payload = bot.posts[0]
        self.assertEqual(payload["reply_markup"], "kb")
        self.assertEqual(payload["reply_parameters"], {"message_id": 7})

    def test_empty_blocks_fall_back(self):
        bot = _FakeBot()
        with patch.object(config, "RICH_TEXT_ENABLED", True):
            asyncio.run(rich_message.send_rich_message(
                bot, 42, [], "<b>fallback</b>"))
        self.assertEqual(bot.posts, [])
        bot.send_message.assert_awaited_once()


class RichEditTests(unittest.TestCase):

    def setUp(self):
        rich_message.reset_support_latch()
        self.addCleanup(rich_message.reset_support_latch)

    def test_edit_posts_rich_message(self):
        bot = _FakeBot()
        blocks = lineup.build_xi_blocks(_squad(bench=2), "@tester",
                                        show_bench=True)
        with patch.object(config, "RICH_TEXT_ENABLED", True):
            ok = asyncio.run(rich_message.edit_rich_message(bot, 42, 9, blocks))
        self.assertTrue(ok)
        endpoint, payload = bot.posts[0]
        self.assertEqual(endpoint, "editMessageText")
        self.assertEqual(payload["message_id"], 9)
        self.assertEqual(payload["rich_message"]["blocks"], blocks)

    def test_edit_reports_failure_so_caller_keeps_html_path(self):
        from telegram.error import BadRequest
        bot = _FakeBot(post_error=BadRequest("Bad Request: nope"))
        with patch.object(config, "RICH_TEXT_ENABLED", True):
            self.assertFalse(
                asyncio.run(rich_message.edit_rich_message(bot, 42, 9, [{}])))

    def test_edit_skipped_when_disabled(self):
        bot = _FakeBot()
        with patch.object(config, "RICH_TEXT_ENABLED", False):
            self.assertFalse(
                asyncio.run(rich_message.edit_rich_message(bot, 42, 9, [{}])))
        self.assertEqual(bot.posts, [])


if __name__ == "__main__":
    unittest.main()

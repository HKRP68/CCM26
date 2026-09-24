"""Nothing a board or a schedule knows is allowed to stop at ten.

Two surfaces used to answer a question with a number instead of an answer:

  • ``/tournamentstats`` ranked exactly ten players and stopped. The person
    reading it is almost never in that ten — that is *why* they opened it — so
    the one line that would have told them where they stand was the first line
    the board threw away.
  • the fixture list rendered forty matches and then said "…and 22 more.",
    which is not a schedule. The match somebody opened the card for was usually
    one of the 22.

Both now render everything and put the tail behind Telegram's own tap-to-expand
— an ``<blockquote expandable>`` in the HTML, a ``details`` block in the rich
rendering. This file pins the three things that makes true:

  1. **nothing is dropped** — every rank, every fixture, is somewhere on the card;
  2. **the card does not grow** — only the first ten (or twenty) are open;
  3. **it survives Telegram's 4,096-character limit** — a long tail is wrapped
     as several quotes rather than one that cannot be sent, because a split
     inside a blockquote is a message Telegram refuses outright.
"""

import itertools
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap  # noqa: E402  (sibling helper; see its docstring)

from utils.message_chunks import DEFAULT_CHUNK_LIMIT, expandable_quotes  # noqa: E402

_TG = itertools.count(994_001)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config",
                 "services.tournament_service", "services.cl_tournament_view",
                 "services.cl_tournament_rich",
                 "services.league_schedule_service", "services.knockout_service")


def setUpModule():
    global _PREV_DATABASE_URL, _SAVED_MODULES, _TMP, _ENGINE

    _PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
    _SAVED_MODULES = _module_swap.save(_MODULE_NAMES)
    _module_swap.unload(_MODULE_NAMES)

    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"

    from database import Base, engine
    import models  # noqa: F401  (registers the tables on Base)

    _ENGINE = engine
    Base.metadata.create_all(bind=engine)


def tearDownModule():
    try:
        _ENGINE.dispose()
    except Exception:
        pass
    if _PREV_DATABASE_URL is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _PREV_DATABASE_URL
    _module_swap.restore(_SAVED_MODULES)
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


# ══════════════════════════════════════════════════════════════════════
# The expandable-quote wrapper
# ══════════════════════════════════════════════════════════════════════

class ExpandableQuoteTests(unittest.TestCase):
    """The piece every "tap to expand" section is built out of."""

    def test_nothing_in_nothing_out(self):
        # So a caller can splice the result in without first asking whether
        # there is a tail at all.
        self.assertEqual(expandable_quotes([]), [])
        self.assertEqual(expandable_quotes(["", None]), [])

    def test_a_short_tail_is_one_quote(self):
        quotes = expandable_quotes(["first", "second"])
        self.assertEqual(
            quotes, ["<blockquote expandable>first\nsecond</blockquote>"])

    def test_a_long_tail_becomes_several_sendable_quotes(self):
        # The failure this prevents: one quote longer than a message, which the
        # chunker can only split BETWEEN the tag and its closing tag — and
        # Telegram rejects that outright, so the section stops being sent.
        quotes = expandable_quotes([f"line {i} " + "x" * 120 for i in range(200)])
        self.assertGreater(len(quotes), 1)
        for quote in quotes:
            self.assertLessEqual(len(quote), DEFAULT_CHUNK_LIMIT)
            self.assertTrue(quote.startswith("<blockquote expandable>"))
            self.assertTrue(quote.endswith("</blockquote>"))

    def test_every_line_survives_the_wrapping(self):
        lines = [f"line {i}" for i in range(500)]
        body = "\n".join(quotes for quotes in expandable_quotes(lines))
        for line in lines:
            self.assertIn(line, body)

    def test_each_quote_is_its_own_block_for_the_splitter(self):
        # html_parts() splits on blank lines and falls back to single newlines
        # only for an oversized block. A quote with no blank line inside it can
        # therefore never be cut in half by the first pass.
        for quote in expandable_quotes([f"line {i}" for i in range(300)]):
            self.assertNotIn("\n\n", quote)


# ══════════════════════════════════════════════════════════════════════
# /tournamentstats — the ranks past tenth
# ══════════════════════════════════════════════════════════════════════

class StatBoardDepthTests(unittest.TestCase):
    """The board ranks 25 and opens 10 of them."""

    def setUp(self):
        from handlers import tournament as T
        self.T = T
        self.tour = type("Tour", (), {"name": "Summer Trophy", "id": 1})()
        self.rows = [T._leader(f"Player {i}", f"Team {i % 4}", 500 - i,
                               extras=[f"{100 + i}b"], matches=i,
                               cells=[("BALLS", str(100 + i)), ("M", str(i))])
                     for i in range(1, T.BOARD_LIMIT + 1)]

    def test_the_first_ten_are_open(self):
        body = self.T._render(self.tour, "runs", self.rows)
        head = body.split("<blockquote expandable>")[0]
        for row in self.rows[:self.T.BOARD_OPEN]:
            self.assertIn(row["name"], head)

    def test_the_rest_are_behind_a_tap_rather_than_gone(self):
        body = self.T._render(self.tour, "runs", self.rows)
        self.assertIn("<blockquote expandable>", body)
        tail = body.split("<blockquote expandable>", 1)[1]
        for row in self.rows[self.T.BOARD_OPEN:]:
            self.assertIn(row["name"], tail)

    def test_the_ranks_keep_counting_past_ten(self):
        body = self.T._render(self.tour, "runs", self.rows)
        # 11th is written "11." — the medals stop at three, and a tail that
        # restarted at 1. would tell the reader they are first.
        self.assertIn("11. <b>Player 11</b>", body)
        self.assertIn(f"{self.T.BOARD_LIMIT}. <b>Player {self.T.BOARD_LIMIT}</b>",
                      body)

    def test_a_short_board_grows_no_expander(self):
        body = self.T._render(self.tour, "runs", self.rows[:6])
        self.assertNotIn("<blockquote expandable>", body)
        self.assertIn("Top 6", body)

    def test_the_rich_twin_splits_at_the_same_place(self):
        blocks = self.T._leaderboard_blocks(self.tour, "runs", self.rows)
        self.assertIsNotNone(blocks)
        tables = [b for b in blocks if b.get("type") == "table"]
        details = [b for b in blocks if b.get("type") == "details"]
        # One open table of ten (plus its header row), and the rest collapsed.
        self.assertEqual(len(tables[0]["cells"]), self.T.BOARD_OPEN + 1)
        self.assertEqual(len(details), 1)
        inner = details[0]["blocks"][0]
        self.assertEqual(len(inner["cells"]),
                         self.T.BOARD_LIMIT - self.T.BOARD_OPEN + 1)

    def test_an_empty_board_still_renders_both_ways(self):
        self.assertIn("No qualifying players yet",
                      self.T._render(self.tour, "runs", []))
        blocks = self.T._leaderboard_blocks(self.tour, "runs", [])
        self.assertTrue(blocks)


# ══════════════════════════════════════════════════════════════════════
# The fixture list — every match, not the first forty
# ══════════════════════════════════════════════════════════════════════

class FixtureListTests(unittest.TestCase):
    """A 56-match schedule renders 56 matches."""

    FIXTURES = 56

    def setUp(self):
        from database import get_session
        from models import (ChallengeMode, ChallengeLeague, ChallengeTeam,
                            Tournament, TournamentTeam, TournamentMatch)
        from services import cl_tournament_view, cl_tournament_rich

        self.session = get_session()
        self.ctv = cl_tournament_view
        self.ctr = cl_tournament_rich

        mode = ChallengeMode(name=f"Mode {next(_TG)}")
        self.session.add(mode)
        self.session.flush()
        league = ChallengeLeague(mode_id=mode.id, name=f"League {next(_TG)}",
                                 short_code="EXP")
        self.session.add(league)
        self.session.flush()
        self.tour = Tournament(name="Long Season", league_id=league.id,
                               league_name=league.name, kind="challenge",
                               status="active", format="League",
                               league_format="double_rr", overs=20, max_teams=8)
        self.session.add(self.tour)
        self.session.flush()

        self.tteams = []
        for i in range(8):
            ct = ChallengeTeam(league_id=league.id, name=f"Team {i}",
                               sort_order=i)
            self.session.add(ct)
            self.session.flush()
            tt = TournamentTeam(tournament_id=self.tour.id,
                                challenge_team_id=ct.id, name=f"Team {i}",
                                sort_order=i)
            self.session.add(tt)
            self.tteams.append(tt)
        self.session.flush()

        for n in range(1, self.FIXTURES + 1):
            a = self.tteams[n % 8]
            b = self.tteams[(n + 3) % 8]
            self.session.add(TournamentMatch(
                tournament_id=self.tour.id, team1_id=a.id, team2_id=b.id,
                stage="league", match_no=n, round_no=1,
                status="scheduled"))
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def test_every_fixture_is_on_the_card(self):
        body = self.ctv.render_fixtures(self.session, self.tour)
        for n in range(1, self.FIXTURES + 1):
            self.assertIn(f"<code>M{n}</code>", body,
                          f"match {n} went missing")

    def test_the_old_truncation_notice_is_gone(self):
        body = self.ctv.render_fixtures(self.session, self.tour)
        self.assertNotIn("more.</i>", body)

    def test_only_the_first_twenty_are_open(self):
        body = self.ctv.render_fixtures(self.session, self.tour)
        head = body.split("<blockquote expandable>")[0]
        opened = re.findall(r"<code>M(\d+)</code>", head)
        self.assertEqual(len(opened), self.ctv.FIXTURE_OPEN)

    def test_the_html_still_fits_telegram_once_split(self):
        from services.rich_message import html_parts
        body = self.ctv.render_fixtures(self.session, self.tour)
        parts = html_parts(body)
        for part in parts:
            self.assertLessEqual(len(part), DEFAULT_CHUNK_LIMIT)
            # A part that opened a quote must close it: a blockquote cut in
            # half is a message Telegram refuses to parse at all.
            self.assertEqual(part.count("<blockquote expandable>"),
                             part.count("</blockquote>"))

    def test_the_rich_twin_carries_every_fixture_too(self):
        blocks = self.ctr.fixtures_blocks(self.session, self.tour)
        self.assertIsNotNone(blocks)
        tables = [b for b in blocks if b.get("type") == "table"]
        details = [b for b in blocks if b.get("type") == "details"]
        rows = len(tables[0]["cells"]) - 1
        rows += sum(len(t["cells"]) - 1
                    for d in details for t in d["blocks"]
                    if t.get("type") == "table")
        self.assertEqual(rows, self.FIXTURES)

    def test_a_short_schedule_grows_no_expander(self):
        from models import TournamentMatch
        (self.session.query(TournamentMatch)
         .filter(TournamentMatch.tournament_id == self.tour.id,
                 TournamentMatch.match_no > 5).delete())
        self.session.commit()
        body = self.ctv.render_fixtures(self.session, self.tour)
        self.assertNotIn("<blockquote expandable>", body)
        blocks = self.ctr.fixtures_blocks(self.session, self.tour)
        self.assertFalse([b for b in blocks if b.get("type") == "details"])


if __name__ == "__main__":
    unittest.main()

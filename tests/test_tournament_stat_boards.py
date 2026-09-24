"""``/tournamentstats`` — a board that answers the next question too.

A leaderboard that prints "383" tells you who is top and nothing else. Whether
that was 383 off 144 balls in thirteen matches or 383 off 400 in thirty is the
whole of what a reader wants to know next, and the figures were already loaded
— ``stat_leaders`` reads whole rows — so the board was throwing them away.

What these pin:

  • every board's rows carry the numbers behind their number, and the *right*
    ones: a batting board does not show economy and a bowling board does not
    show a strike rate
  • the two renderers agree. The HTML board puts the figures in a bracket and
    the rich table gives them columns; both are built from the same row, so
    neither can drift into showing something the other does not
  • a per-innings board (one knock, one spell) carries no match count, because
    the answer to "how many matches" is one
  • a plain three-tuple row still renders, so nothing that builds one outside
    this module breaks on the richer shape
"""

import itertools
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap  # noqa: E402  (sibling helper; see its docstring)

_SEQ = itertools.count(1)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "services.tournament_service")


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


class BoardCase(unittest.TestCase):
    def setUp(self):
        from database import get_session
        from handlers import tournament as T
        from models import Tournament, TournamentPlayerStats, User

        self.T = T
        self.session = get_session()
        self.tour = Tournament(name=f"Stat Trophy {next(_SEQ)}")
        self.session.add(self.tour)
        self.session.flush()

        user = User(telegram_id=next(_SEQ) + 770_000, first_name="Owner")
        self.session.add(user)
        self.session.flush()

        # One all-rounder's season, with every column a board reads.
        self.session.add(TournamentPlayerStats(
            tournament_id=self.tour.id, user_id=user.id,
            name="Sai Sudharsan", team_name="PBKS",
            matches=13, bat_innings=13, bat_runs=383, bat_balls=144,
            bat_fours=40, bat_sixes=24, bat_outs=7, highest_score=101,
            bowl_innings=9, bowl_wickets=14, bowl_runs=231, bowl_balls=108,
            best_bowl_wickets=4, best_bowl_runs=27))
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def _rows(self, category):
        return self.T._leaders_for(self.session, self.tour, category)

    def _top(self, category):
        rows = self._rows(category)
        self.assertTrue(rows, f"no rows on the {category} board")
        return rows[0]


class RunBoardTests(BoardCase):
    """The board the request was about: 383 runs (144b, SR 265.97) · 13 M."""

    def test_the_row_carries_the_balls_the_rate_and_the_matches(self):
        row = self._top("runs")
        self.assertEqual(row["value"], "383")
        self.assertEqual(row["extras"], ["144b", "SR 265.97"])
        self.assertEqual(row["matches"], 13)

    def test_the_line_reads_as_one_sentence_under_the_name(self):
        line = self.T._rank_line(1, self._top("runs"), "runs")
        head, detail = line.split("\n")
        self.assertIn("<b>Sai Sudharsan</b>", head)
        self.assertIn("(PBKS)", head)
        self.assertIn("🏃", detail)
        self.assertIn("<b>383</b> runs", detail)
        self.assertIn("144b · SR 265.97", detail)
        self.assertIn("13 M", detail)

    def test_the_table_gives_the_same_figures_their_own_columns(self):
        cells = self.T._rank_cells("runs", self._rows("runs"))
        headers = [c["text"]["text"] for c in cells[0]]
        self.assertEqual(headers, ["#", "PLAYER", "TEAM", "RUNS",
                                   "BALLS", "SR", "M"])
        values = [c["text"] for c in cells[1]]
        self.assertIn("144", values)
        self.assertIn("265.97", values)
        self.assertIn("13", values)


class WicketBoardTests(BoardCase):
    def test_the_row_carries_the_overs_the_economy_and_the_best_figure(self):
        row = self._top("wkts")
        self.assertEqual(row["value"], "14")
        # 108 balls is eighteen overs, not 18.0 arrived at by dividing by six.
        self.assertEqual(row["extras"],
                         ["18.0 ov", "Econ 12.83", "Best 4/27"])
        self.assertEqual(row["matches"], 13)

    def test_a_bowler_is_never_shown_a_batting_figure(self):
        detail = self.T._rank_line(1, self._top("wkts"), "wkts")
        self.assertIn("wkts", detail)
        self.assertNotIn("SR", detail)

    def test_the_overs_are_cricket_overs(self):
        """110 balls is 18.2 — eighteen overs and two balls — never 18.33."""
        self.assertEqual(self.T._overs(110), "18.2")
        self.assertEqual(self.T._overs(108), "18.0")
        self.assertEqual(self.T._overs(0), "0.0")


class PerInningsBoardTests(BoardCase):
    """One knock or one spell. "How many matches" has one answer, so the board
    does not ask it."""

    def test_a_best_figure_carries_no_match_count(self):
        from handlers import tournament as T
        row = T._unpack(T._leader("Somebody", "PBKS", "5/21"))
        line = T._rank_line(1, row, "fig")
        self.assertNotIn("M", line.split("<b>5/21</b>")[-1])
        self.assertNotIn("\n", line)

    def test_a_row_with_nothing_extra_stays_on_one_line(self):
        from handlers import tournament as T
        line = T._rank_line(1, ("Plain", "PBKS", "42"), "runs")
        self.assertNotIn("\n", line)
        self.assertIn("<b>42</b>", line)


class FigureFormattingTests(BoardCase):
    def test_a_bowler_with_no_figure_yet_shows_none(self):
        """``best_bowl_runs`` is seeded at -1 because 0 runs is a real (and
        excellent) figure, so the sentinel is what has to be checked."""
        from types import SimpleNamespace
        self.assertIsNone(self.T._figure(
            SimpleNamespace(best_bowl_wickets=0, best_bowl_runs=-1)))
        self.assertEqual(self.T._figure(
            SimpleNamespace(best_bowl_wickets=3, best_bowl_runs=0)), "3/0")


class LegacyRowTests(unittest.TestCase):
    """Rows used to be plain ``(name, team, value)`` tuples."""

    def test_a_bare_tuple_still_unpacks(self):
        from handlers import tournament as T
        row = T._unpack(("Player", "Team", "100"))
        self.assertEqual(row["name"], "Player")
        self.assertEqual(row["value"], "100")
        self.assertEqual(row["extras"], [])
        self.assertIsNone(row["matches"])

    def test_a_table_of_bare_tuples_has_no_extra_columns(self):
        from handlers import tournament as T
        cells = T._rank_cells("runs", [("Player", "Team", "100")])
        self.assertEqual(len(cells[0]), 4)
        self.assertEqual(len(cells[1]), 4)


if __name__ == "__main__":
    unittest.main()

"""The dashboard's "Most Matches Today" board — who is actually playing.

The ops tiles say how many matches finished today and the Top Users panel ranks
all-time wins; between them, the manager who ground out twenty games this
morning does not appear anywhere. This is the board that shows them, and these
tests pin what makes it honest:

  • **a match is a game for both sides.** Counting one column would halve
    everybody's day and hide whoever only ever gets challenged.
  • **the bot opponent is not a player.** /vsbot's "Bot Opponent" is a real
    ``User`` row that appears in every bot match, so it would win this board
    every day and pad the head count with nobody.
  • **the window is the window.** ``completed_at`` is UTC and the board is an
    IST calendar day, so a match either side of the boundary belongs to the
    other day, not to this one.
"""

import itertools
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap  # noqa: E402  (sibling helper; see its docstring)

_TG = itertools.count(990_001)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "services.match_day_stats")

DAY = datetime(2026, 3, 14, 0, 0, 0)


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


class MatchDayCase(unittest.TestCase):
    def setUp(self):
        from database import get_session
        from services import match_day_stats

        self.session = get_session()
        self.stats = match_day_stats
        self.people = {}

    def tearDown(self):
        from models import Match, User
        self.session.query(Match).delete()
        self.session.query(User).delete()
        self.session.commit()
        self.session.close()

    # ── helpers ───────────────────────────────────────────────────────

    def user(self, name, telegram_id=None):
        from models import User
        if name in self.people:
            return self.people[name]
        row = User(telegram_id=next(_TG) if telegram_id is None else telegram_id,
                   username=name.lower(), first_name=name)
        self.session.add(row)
        self.session.flush()
        self.people[name] = row
        return row

    def match(self, one, two, *, winner=None, at=None):
        from models import Match
        row = Match(user1_id=self.user(one).id, user2_id=self.user(two).id,
                    status="completed",
                    winner_id=self.user(winner).id if winner else None,
                    completed_at=at or (DAY + timedelta(hours=9)))
        self.session.add(row)
        self.session.commit()
        return row

    def board(self, limit=10):
        return self.stats.most_matches(self.session, DAY, DAY + timedelta(days=1),
                                       limit=limit)

    def names(self, board):
        return [(row["user"].first_name, row["count"], row["won"])
                for row in board["leaders"]]


class CountingTests(MatchDayCase):
    def test_an_empty_day_is_an_empty_board_not_a_crash(self):
        board = self.board()
        self.assertEqual(board["total"], 0)
        self.assertEqual(board["players"], 0)
        self.assertEqual(board["leaders"], [])

    def test_both_sides_of_a_match_played_it(self):
        self.match("Asha", "Bo", winner="Asha")
        self.assertEqual(self.names(self.board()),
                         [("Asha", 1, 1), ("Bo", 1, 0)])
        self.assertEqual(self.board()["total"], 1)
        self.assertEqual(self.board()["players"], 2)

    def test_the_busiest_player_is_first(self):
        for _ in range(3):
            self.match("Asha", "Bo", winner="Bo")
        self.match("Cai", "Dev", winner="Cai")
        board = self.board()
        self.assertEqual(self.names(board)[0][:2], ("Asha", 3))
        self.assertEqual(board["total"], 4)
        self.assertEqual(board["players"], 4)

    def test_wins_are_counted_alongside_the_games(self):
        self.match("Asha", "Bo", winner="Asha")
        self.match("Asha", "Bo", winner="Asha")
        self.match("Asha", "Bo", winner="Bo")
        self.assertEqual(self.names(self.board()),
                         [("Asha", 3, 2), ("Bo", 3, 1)])

    def test_a_tie_gives_nobody_a_win_but_still_counts_as_played(self):
        self.match("Asha", "Bo", winner=None)
        self.assertEqual(self.names(self.board()), [("Asha", 1, 0), ("Bo", 1, 0)])

    def test_the_bar_is_a_share_of_the_busiest_day(self):
        for _ in range(4):
            self.match("Asha", "Bo")
        self.match("Cai", "Dev")
        board = self.board()
        by_name = {r["user"].first_name: r["pct"] for r in board["leaders"]}
        self.assertEqual(by_name["Asha"], 100)
        self.assertEqual(by_name["Cai"], 25)

    def test_the_board_is_capped_but_the_head_count_is_not(self):
        for i in range(7):
            self.match(f"P{i}", f"Q{i}")
        board = self.board(limit=3)
        self.assertEqual(len(board["leaders"]), 3)
        self.assertEqual(board["players"], 14)


class ExclusionTests(MatchDayCase):
    def test_the_bot_opponent_never_appears_on_the_board(self):
        """It plays every /vsbot match, so it would top this every day."""
        self.user("Bot Opponent", telegram_id=-1)
        self.match("Asha", "Bot Opponent", winner="Asha")
        self.match("Asha", "Bot Opponent", winner="Asha")
        board = self.board()
        self.assertEqual(self.names(board), [("Asha", 2, 2)])
        # …and it is not one of the day's players either.
        self.assertEqual(board["players"], 1)
        # The matches themselves still happened.
        self.assertEqual(board["total"], 2)

    def test_an_unfinished_match_is_not_a_match_played(self):
        from models import Match
        self.session.add(Match(user1_id=self.user("Asha").id,
                               user2_id=self.user("Bo").id,
                               status="active",
                               completed_at=DAY + timedelta(hours=9)))
        self.session.commit()
        self.assertEqual(self.board()["total"], 0)


class WindowTests(MatchDayCase):
    def test_yesterdays_last_match_belongs_to_yesterday(self):
        self.match("Asha", "Bo", at=DAY - timedelta(seconds=1))
        self.assertEqual(self.board()["total"], 0)

    def test_tomorrows_first_match_belongs_to_tomorrow(self):
        self.match("Asha", "Bo", at=DAY + timedelta(days=1))
        self.assertEqual(self.board()["total"], 0)

    def test_the_first_and_last_instant_of_the_day_are_in_it(self):
        self.match("Asha", "Bo", at=DAY)
        self.match("Cai", "Dev", at=DAY + timedelta(days=1, microseconds=-1))
        self.assertEqual(self.board()["total"], 2)


class DashboardWiringTests(unittest.TestCase):
    """The route and the panel, checked at source level.

    Importing ``admin`` builds the whole Flask app and binds it to a live
    database — far more than these assertions need, and it poisons the module
    identity the other test files rely on.
    """

    @staticmethod
    def _read(*parts):
        path = os.path.join(os.path.dirname(__file__), "..", *parts)
        with open(path) as fh:
            return fh.read()

    def test_the_dashboard_builds_the_board_over_the_ist_day(self):
        route = (self._read("admin.py").split("def dashboard():")[1]
                 .split("\n@app.route")[0])
        self.assertIn("from services.match_day_stats import most_matches", route)
        # ist_today_0 is IST wall-clock; completed_at is stored in UTC.
        self.assertIn("ist_today_0 - _IST_OFFSET", route)
        self.assertIn("match_day=match_day", route)

    def test_the_panel_shows_who_played_how_many_and_how_many_they_won(self):
        html = self._read("templates", "dashboard.html")
        self.assertIn("Most Matches Today", html)
        self.assertIn("match_day.leaders", html)
        self.assertIn("row.count", html)
        self.assertIn("row.won", html)
        self.assertIn("match_day.total", html)
        self.assertIn("match_day.players", html)

    def test_a_quiet_day_reads_as_quiet_rather_than_broken(self):
        html = self._read("templates", "dashboard.html")
        self.assertIn("No matches have finished today yet.", html)


if __name__ == "__main__":
    unittest.main()

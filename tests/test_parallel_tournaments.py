"""Parallel Challenge League tournaments: one active tournament per league.

  • two leagues' tournaments stay live side by side
  • activating a second tournament in the *same* league retires the first
  • ``get_active_tournament(league_id=…)`` finds each league's own one
  • every running tournament gets its own derived read-only commands
    (``/tipl`` → ``/tipltable``, ``/tiplstats`` …) and the router resolves them
"""

import itertools
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap  # noqa: E402  (sibling helper, see its docstring)

_N = itertools.count(1)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "services.tournament_service",
                 "handlers.challenge")


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


class ParallelTournamentTests(unittest.TestCase):
    def setUp(self):
        from database import get_session
        from models import ChallengeMode
        self.session = get_session()
        self.mode = ChallengeMode(name=f"Mode {next(_N)}")
        self.session.add(self.mode)
        self.session.flush()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def _league(self, command):
        from models import ChallengeLeague
        lg = ChallengeLeague(mode_id=self.mode.id, name=f"League {next(_N)}",
                             tournament_command=command)
        self.session.add(lg)
        self.session.flush()
        return lg

    def _tournament(self, league, *, active=True):
        from models import Tournament
        from services import tournament_service as ts
        t = Tournament(name=f"Cup {next(_N)}", league_id=league.id,
                       league_name=league.name, status="active")
        self.session.add(t)
        self.session.flush()
        if active:
            ts.activate_tournament(self.session, t.id)
        self.session.flush()
        return t

    def _active_ids(self):
        from services import tournament_service as ts
        self.session.expire_all()
        return {t.id for t in ts.get_active_tournaments(self.session)}

    def test_two_leagues_run_in_parallel(self):
        a = self._tournament(self._league(f"/ta{next(_N)}"))
        b = self._tournament(self._league(f"/tb{next(_N)}"))
        self.assertTrue({a.id, b.id} <= self._active_ids())

    def test_second_tournament_in_same_league_retires_the_first(self):
        lg = self._league(f"/tc{next(_N)}")
        first = self._tournament(lg)
        second = self._tournament(lg)
        ids = self._active_ids()
        self.assertIn(second.id, ids)
        self.assertNotIn(first.id, ids)

    def test_lookup_by_league(self):
        from services import tournament_service as ts
        la, lb = self._league(f"/td{next(_N)}"), self._league(f"/te{next(_N)}")
        a, b = self._tournament(la), self._tournament(lb)
        self.assertEqual(ts.get_active_tournament(self.session, league_id=la.id).id, a.id)
        self.assertEqual(ts.get_active_tournament(self.session, league_id=lb.id).id, b.id)
        self.assertIsNone(ts.get_active_tournament(
            self.session, league_id=self._league(f"/tf{next(_N)}").id))

    def test_derived_view_commands(self):
        from services import tournament_service as ts
        n = next(_N)
        t = self._tournament(self._league(f"/TIPL{n}@SomeBot"))
        cmds = {view: cmd for view, cmd, _label in ts.view_commands(self.session, t)}
        self.assertEqual(cmds["table"], f"/tipl{n}table")
        self.assertEqual(cmds["stats"], f"/tipl{n}stats")
        self.assertEqual(cmds["mvp"], f"/tipl{n}mvp")
        text = ts.running_commands_text(self.session, [t])
        self.assertIn(f"/tipl{n}table", text)
        self.assertIn(f"Play: /tipl{n}", text)

    def test_router_resolves_view_commands(self):
        from handlers import challenge
        n = next(_N)
        lg = self._league(f"/tz{n}")
        self.session.commit()
        hit = challenge.is_tournament_view_command(f"/tz{n}table", self.session)
        self.assertIsNotNone(hit)
        self.assertEqual((hit[0].id, hit[1]), (lg.id, "table"))
        self.assertEqual(challenge.is_tournament_view_command(
            f"tz{n}player@Bot", self.session)[1], "player")
        self.assertIsNone(challenge.is_tournament_view_command(
            f"tz{n}nonsense", self.session))
        # The start command itself is not a view command.
        self.assertIsNone(challenge.is_tournament_view_command(f"tz{n}", self.session))


if __name__ == "__main__":
    unittest.main()

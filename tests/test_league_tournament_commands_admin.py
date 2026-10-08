"""Challenge Data admin: naming a league's tournament view commands.

  • the league page shows one field per tournament view, with the automatic
    name as its placeholder
  • a saved name is stored, routed by the bot, and a blank field keeps the
    automatic name
  • a name another league already uses is skipped, the rest still save

Follows the throwaway-sqlite pattern from
``tests/test_challenge_data_import.py``.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap  # noqa: E402

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "admin",
                 "handlers.challenge", "services.tournament_service")

IDS = {}


def setUpModule():
    global _PREV_DATABASE_URL, _SAVED_MODULES, _TMP, _ENGINE
    _PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
    _SAVED_MODULES = _module_swap.save(_MODULE_NAMES)
    _module_swap.unload(_MODULE_NAMES)
    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"
    os.environ.setdefault("BOT_TOKEN", "test-token")
    os.environ.setdefault("ADMIN_PASSWORD", "test")
    os.environ.setdefault("ADMIN_USERNAME", "admin")
    from database import Base, engine, get_session
    from models import ChallengeLeague, ChallengeMode
    _ENGINE = engine
    Base.metadata.create_all(bind=engine)
    s = get_session()
    try:
        mode = ChallengeMode(name="Leagues")
        s.add(mode)
        s.flush()
        ipl = ChallengeLeague(mode_id=mode.id, name="IPL", short_code="IPL",
                              command="/cipl", tournament_command="/tipl")
        psl = ChallengeLeague(mode_id=mode.id, name="PSL", short_code="PSL",
                              command="/cpsl", tournament_command="/tpsl",
                              tournament_view_commands_json=json.dumps(
                                  {"table": "/psltable"}))
        s.add_all([ipl, psl])
        s.commit()
        IDS["ipl"], IDS["psl"] = ipl.id, psl.id
    finally:
        s.close()


def tearDownModule():
    if _ENGINE is not None:
        _ENGINE.dispose()
    if _PREV_DATABASE_URL is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _PREV_DATABASE_URL
    _module_swap.restore(_SAVED_MODULES)
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


class LeagueTournamentCommandsAdminTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            import admin
        except Exception as exc:            # pragma: no cover - env-dependent
            raise unittest.SkipTest(f"admin app unavailable: {exc}")
        admin.app.config["TESTING"] = True
        admin.app.config["WTF_CSRF_ENABLED"] = False
        cls.admin = admin

    def setUp(self):
        from database import get_session
        self.session = get_session()
        self.client = self.admin.app.test_client()
        with self.client.session_transaction() as flask_session:
            flask_session["admin"] = True

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def _save_ipl(self, **fields):
        data = {"action": "save_league", "league_name": "IPL", "short_code": "IPL",
                "command": "/cipl", "tournament_command": "/tipl",
                "league_is_active": "on", "min_overseas": "0", "max_overseas": "11"}
        data.update(fields)
        return self.client.post(f"/challenge-data/leagues/{IDS['ipl']}", data=data)

    def _ipl(self):
        from models import ChallengeLeague
        self.session.expire_all()
        return self.session.get(ChallengeLeague, IDS["ipl"])

    def test_league_page_shows_the_command_fields(self):
        resp = self.client.get(f"/challenge-data/leagues/{IDS['ipl']}")
        self.assertEqual(resp.status_code, 200)
        page = resp.get_data(as_text=True)
        self.assertIn('name="tv_table"', page)
        self.assertIn('placeholder="/tipltable"', page)
        self.assertIn('name="tv_mvp"', page)

    def test_saved_names_route_and_blanks_stay_automatic(self):
        from handlers import challenge
        resp = self._save_ipl(tv_table="ipltable", tv_stats="/IPLStats", tv_mvp="")
        self.assertEqual(resp.status_code, 302)
        lg = self._ipl()
        self.assertEqual(json.loads(lg.tournament_view_commands_json),
                         {"table": "/ipltable", "stats": "/iplstats"})
        hit = challenge.is_tournament_view_command("/ipltable", self.session)
        self.assertEqual((hit[0].id, hit[1]), (IDS["ipl"], "table"))
        self.assertEqual(challenge.is_tournament_view_command(
            "iplstats", self.session)[1], "stats")
        # Left blank → the automatic name still works.
        self.assertEqual(challenge.is_tournament_view_command(
            "tiplmvp", self.session)[1], "mvp")

    def test_a_name_another_league_uses_is_skipped(self):
        self._save_ipl(tv_table="/psltable", tv_teams="/iplteams")
        stored = json.loads(self._ipl().tournament_view_commands_json)
        self.assertNotIn("table", stored)
        self.assertEqual(stored["teams"], "/iplteams")

    def test_the_same_name_twice_keeps_only_the_first(self):
        self._save_ipl(tv_table="/iplx", tv_teams="/iplx")
        self.assertEqual(json.loads(self._ipl().tournament_view_commands_json),
                         {"table": "/iplx"})

    def test_all_blank_clears_the_custom_names(self):
        self._save_ipl(tv_table="/iplclear")
        self._save_ipl()
        self.assertIsNone(self._ipl().tournament_view_commands_json)


if __name__ == "__main__":
    unittest.main()

"""Preferred pitches, driven through the Flask admin app.

What is pinned here: a tournament can be created with a preferred-pitch mode,
share and list; the Manage page saves them (and refuses an over-long list
instead of trimming it); each team saves its own list; and the pages that show
all of this still render.
"""

import json
import itertools
import os
import re
import sys
import tempfile
import unittest

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None

# Everything that caches a reference to ``database`` / ``models``, for the reason
# tests/test_auction_bidding.py gives at length. ``admin`` is in the list because
# it binds ``get_session`` at import time.
_MODULE_NAMES = ("database", "models", "config", "admin",
                 "services.tournament_service",
                 "services.league_schedule_service")

_PID = itertools.count(1)


def _unload(names):
    for name in names:
        sys.modules.pop(name, None)
        parent, _, child = name.rpartition(".")
        package = sys.modules.get(parent) if parent else None
        if package is not None:
            try:
                delattr(package, child)
            except AttributeError:
                pass


def _restore(saved):
    for name, module in saved.items():
        parent, _, child = name.rpartition(".")
        if module is None:
            _unload([name])
            continue
        sys.modules[name] = module
        package = sys.modules.get(parent) if parent else None
        if package is not None:
            setattr(package, child, module)


def setUpModule():
    global _PREV_DATABASE_URL, _SAVED_MODULES, _TMP, _ENGINE

    _PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
    _SAVED_MODULES = {name: sys.modules.get(name) for name in _MODULE_NAMES}
    _unload(_MODULE_NAMES)

    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"
    os.environ.setdefault("BOT_TOKEN", "test-token")
    os.environ.setdefault("ADMIN_PASSWORD", "test")
    os.environ.setdefault("ADMIN_USERNAME", "admin")

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
    _restore(_SAVED_MODULES)
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


class PreferredPitchAdminTests(unittest.TestCase):

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
        from models import ChallengeMode, ChallengeLeague, ChallengeTeam
        self.session = get_session()
        tag = next(_PID)
        mode = ChallengeMode(name=f"Mode {tag}")
        self.session.add(mode)
        self.session.flush()
        self.league = ChallengeLeague(mode_id=mode.id, name=f"League {tag}",
                                      short_code="PP")
        self.session.add(self.league)
        self.session.flush()
        self.cteams = []
        for i, name in enumerate(("Alpha", "Bravo", "Charlie", "Delta")):
            ct = ChallengeTeam(league_id=self.league.id, name=name, sort_order=i)
            self.session.add(ct)
            self.cteams.append(ct)
        self.session.commit()
        self.client = self.admin.app.test_client()
        with self.client.session_transaction() as flask_session:
            flask_session["admin"] = True

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def create(self, **extra):
        from models import Tournament
        data = {"action": "create_tournament", "name": f"Cup {next(_PID)}",
                "league_id": str(self.league.id), "tour_format": "double_round_robin",
                "team_ids": [str(ct.id) for ct in self.cteams]}
        data.update(extra)
        response = self.client.post("/tournaments", data=data,
                                    follow_redirects=True)
        self.assertEqual(200, response.status_code)
        self.session.expire_all()
        return (self.session.query(Tournament).filter_by(name=data["name"]).one())

    def detail(self, t, data=None):
        url = f"/tournaments/{t.id}"
        response = (self.client.post(url, data=data, follow_redirects=True)
                    if data else self.client.get(url))
        self.assertEqual(200, response.status_code)
        self.session.expire_all()
        return response.get_data(as_text=True)

    def test_create_with_a_tournament_list(self):
        t = self.create(pitch_mode="tour_preferred", preferred_pitch_pct="70",
                        preferred_pitch_limit="3",
                        preferred_pitches=["Green", "Dusty"])
        self.assertEqual(t.pitch_mode, "tour_preferred")
        self.assertEqual(t.preferred_pitch_pct, 70)
        self.assertEqual(t.preferred_pitch_limit, 3)
        self.assertEqual(json.loads(t.preferred_pitches_json), ["Green", "Dusty"])
        self.assertIn("Tournament preferred pitches", self.detail(t))

    def test_the_create_page_offers_the_new_modes(self):
        body = self.client.get("/tournaments").get_data(as_text=True)
        self.assertIn('value="team_preferred"', body)
        self.assertIn('name="preferred_pitches"', body)

    def test_settings_save_and_re_stamp_the_schedule(self):
        from services import league_schedule_service as lss
        t = self.create()
        lss.generate_schedule(self.session, t.id)
        self.session.commit()
        self.detail(t, {"action": "save_settings", "name": t.name,
                        "pitch_mode": "tour_preferred",
                        "preferred_pitch_pct": "100", "preferred_pitch_limit": "4",
                        "preferred_pitches": ["Flat"]})
        self.assertEqual(t.pitch_mode, "tour_preferred")
        pitches = {fx.pitch_type for fx in lss.list_fixtures(self.session, t.id)}
        self.assertEqual(pitches, {"Flat"})

    def test_an_over_long_list_is_refused_not_trimmed(self):
        t = self.create(preferred_pitches=["Green"])
        body = self.detail(t, {"action": "save_settings", "name": t.name,
                               "pitch_mode": "tour_preferred",
                               "preferred_pitch_pct": "80",
                               "preferred_pitch_limit": "2",
                               "preferred_pitches": ["Green", "Dusty", "Flat"]})
        self.assertIn("at most 2", body)
        self.assertEqual(json.loads(t.preferred_pitches_json), ["Green"])

    def test_a_team_saves_its_own_list(self):
        from models import TournamentTeam
        t = self.create(pitch_mode="team_preferred")
        tt = self.session.query(TournamentTeam).filter_by(tournament_id=t.id).first()
        tt.home_pitch = "Bouncy"
        self.session.commit()
        body = self.detail(t, {"action": "set_team_preferred_pitches",
                               "team_id": str(tt.id),
                               "preferred_pitches": ["Green", "Hard"]})
        self.assertIn("Green, Hard", body)
        self.session.refresh(tt)
        self.assertEqual(json.loads(tt.preferred_pitches_json), ["Green", "Hard"])
        self.assertIsNone(tt.home_pitch)


if __name__ == "__main__":
    unittest.main()

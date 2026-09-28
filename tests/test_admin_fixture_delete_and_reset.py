"""The Schedule page's fixture buttons and the Reset user button, end to end.

The Schedule page is GET-only, and its Delete / Swap forms used to post back to
it — a 405, so a fixture could never be deleted from there. They now post to the
Manage handler and are sent back to the Schedule page. Reset user must leave the
account flagged for a fresh /debut.
"""

import itertools
import os
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
                 "services.league_schedule_service",
                 "services.user_reset_service")

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



class AdminFixtureAndResetTests(unittest.TestCase):

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
        from models import Tournament, TournamentTeam
        self.session = get_session()
        self.tour = Tournament(name=f"Cup {next(_PID)}", kind="challenge",
                               status="active", format="League",
                               league_format="single_rr", overs=20, max_teams=8,
                               schedule_generated=True)
        self.session.add(self.tour)
        self.session.flush()
        self.a = TournamentTeam(tournament_id=self.tour.id, name="Alpha")
        self.b = TournamentTeam(tournament_id=self.tour.id, name="Bravo")
        self.session.add_all([self.a, self.b])
        self.session.commit()
        self.client = self.admin.app.test_client()
        with self.client.session_transaction() as flask_session:
            flask_session["admin"] = True

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def fixture(self, status="scheduled"):
        from models import TournamentMatch
        fx = TournamentMatch(tournament_id=self.tour.id, team1_id=self.a.id,
                             team2_id=self.b.id, status=status, stage="league",
                             round_no=1, match_no=next(_PID))
        self.session.add(fx)
        self.session.commit()
        return fx

    def schedule_url(self):
        return f"/tournaments/{self.tour.id}/schedule"

    def test_the_schedule_page_forms_post_to_the_manage_handler(self):
        self.fixture()
        self.fixture(status="completed")
        body = self.client.get(self.schedule_url()).get_data(as_text=True)
        manage = f'action="/tournaments/{self.tour.id}"'
        self.assertEqual(body.count("<form"), body.count(manage))
        self.assertIn('name="force"', body)  # the completed fixture's delete

    def test_delete_from_the_schedule_page_lands_back_on_it(self):
        from models import TournamentMatch
        fid = self.fixture().id
        response = self.client.post(
            f"/tournaments/{self.tour.id}",
            data={"action": "delete_fixture", "fixture_id": str(fid),
                  "return_to": "schedule"})
        self.assertEqual(302, response.status_code)
        self.assertTrue(response.headers["Location"].endswith(self.schedule_url()))
        self.session.expire_all()
        self.assertIsNone(self.session.get(TournamentMatch, fid))

    def test_a_completed_fixture_needs_force(self):
        from models import TournamentMatch
        fid = self.fixture(status="completed").id
        self.client.post(f"/tournaments/{self.tour.id}",
                         data={"action": "delete_fixture", "fixture_id": str(fid)})
        self.session.expire_all()
        self.assertIsNotNone(self.session.get(TournamentMatch, fid))
        self.client.post(f"/tournaments/{self.tour.id}",
                         data={"action": "delete_fixture", "fixture_id": str(fid),
                               "force": "1"})
        self.session.expire_all()
        self.assertIsNone(self.session.get(TournamentMatch, fid))

    def test_reset_user_flags_the_account_for_debut(self):
        from models import User, UserStats
        user = User(telegram_id=660_000 + next(_PID), first_name="Resettable",
                    total_coins=1234, total_gems=5)
        self.session.add(user)
        self.session.flush()
        self.session.add(UserStats(user_id=user.id))
        self.session.commit()
        response = self.client.post(f"/users/{user.id}/reset",
                                    data={"confirmation": f"RESET {user.id}"},
                                    follow_redirects=True)
        self.assertEqual(200, response.status_code)
        self.session.expire_all()
        user = self.session.get(User, user.id)
        self.assertTrue(user.needs_debut)
        self.assertEqual((user.total_coins, user.total_gems), (0, 0))
        self.assertIn("waiting for the player to run", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()

"""Tournament-only enforcement through authenticated and REST game APIs."""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap

_MODULES = ("database", "models", "config", "admin",
            "services.tournament_only_service")
_SAVED = {}
_ENV = {}
_TMP = None
_ENGINE = None


def setUpModule():
    global _SAVED, _ENV, _TMP, _ENGINE
    _SAVED = _module_swap.save(_MODULES)
    _module_swap.unload(_MODULES)
    _ENV = {key: os.environ.get(key) for key in
            ("DATABASE_URL", "BOT_TOKEN", "WEBAPP_DEV_MODE", "ADMIN_SECRET")}
    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    os.environ.update(DATABASE_URL=f"sqlite:///{_TMP.name}", BOT_TOKEN="test-token",
                      WEBAPP_DEV_MODE="1", ADMIN_SECRET="test-secret")
    from database import Base, engine
    import models  # noqa: F401
    _ENGINE = engine
    Base.metadata.create_all(engine)


def tearDownModule():
    if _ENGINE is not None:
        _ENGINE.dispose()
    for key, value in _ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    _module_swap.restore(_SAVED)
    if _TMP is not None:
        os.unlink(_TMP.name)


class TournamentOnlyWebappTests(unittest.TestCase):
    def setUp(self):
        import admin
        from database import get_session
        from models import Match, Tournament, User
        self.admin = admin
        admin.app.config.update(TESTING=True)
        self.client = admin.app.test_client()
        self.db = get_session()
        for model in (Match, Tournament, User):
            self.db.query(model).delete()
        self.me = User(telegram_id=8070001, tournament_only=True,
                       tournament_only_reason="Tournament fixture is overdue")
        self.other = User(telegram_id=8070002)
        self.db.add_all([self.me, self.other])
        self.db.flush()
        self.casual = Match(user1_id=self.me.id, user2_id=self.other.id,
                            match_type="wpm", status="playing")
        tour = Tournament(name="Official cup", kind="challenge", status="active")
        self.db.add(tour)
        self.db.flush()
        self.official = Match(user1_id=self.me.id, user2_id=self.other.id,
                              tournament_id=tour.id, status="playing")
        self.db.add_all([self.casual, self.official])
        self.db.commit()
        self.headers = {"Authorization": f"tma DEV_{self.me.telegram_id}"}
        self.cfg = patch("services.config_service.get_config", return_value={})
        self.cfg.start()
        self.addCleanup(self.cfg.stop)
        reconcile = patch("services.roster_service.reconcile_roster_count",
                          side_effect=lambda db, user: user.roster_count)
        reconcile.start()
        self.addCleanup(reconcile.stop)

    def tearDown(self):
        self.db.close()

    def assertBlocked(self, response):
        self.assertEqual(response.status_code, 403, response.get_json())
        self.assertEqual(response.get_json()["error"], "tournament_only")

    def test_other_game_apis_reject_before_playing(self):
        for path in ("/api/webapp/quickmatch/start", "/api/webapp/quickmatch/phase",
                     "/api/fantasy/save", "/api/ipl16/share"):
            with self.subTest(path=path):
                self.assertBlocked(self.client.post(path, json={}, headers=self.headers))

    def test_casual_match_actions_reject_a_forged_tournament_flag(self):
        self.assertBlocked(self.client.post(
            "/api/webapp/match/deliver", headers=self.headers,
            json={"match_id": self.casual.id, "is_tournament": True,
                  "tournament_id": self.official.tournament_id}))

    def test_opponent_cannot_advance_a_restricted_players_casual_match(self):
        self.assertBlocked(self.client.post(
            "/api/webapp/match/ready", json={"match_id": self.casual.id},
            headers={"Authorization": f"tma DEV_{self.other.telegram_id}"}))

    def test_all_match_mutation_aliases_share_the_same_gate(self):
        for path in self.admin._TOURNAMENT_ONLY_MATCH_ACTION_PATHS:
            with self.subTest(path=path):
                self.assertBlocked(self.client.post(
                    path, json={"match_id": self.casual.id, "userId": self.me.id},
                    headers=self.headers))

    def test_restriction_is_effective_on_the_next_request(self):
        self.assertBlocked(self.client.post("/api/webapp/quickmatch/start",
                                           json={}, headers=self.headers))
        self.me.tournament_only = False
        self.db.commit()
        with self.admin.app.test_request_context("/api/webapp/quickmatch/start",
                                                 method="POST", json={}):
            self.assertIsNone(self.admin._tournament_only_webapp_block(self.db, self.me))

    def test_persisted_official_match_remains_playable(self):
        with self.admin.app.test_request_context("/api/webapp/match/deliver",
                                                 method="POST",
                                                 json={"match_id": self.official.id}):
            self.assertIsNone(self.admin._tournament_only_webapp_block(self.db, self.me))

    def test_official_match_is_allowed_through_rest_alias(self):
        with self.admin.app.test_request_context("/api/match/action", method="POST"):
            with patch("services.match_webapp_access.get_state", return_value=None):
                user, match, error = self.admin._match_rest_user_and_match(
                    self.db, self.me.id, self.official.id)
        self.assertIsNone(error)
        self.assertEqual(match.id, self.official.id)

    def test_rest_read_is_allowed_for_restricted_player(self):
        with self.admin.app.test_request_context("/api/match/state"):
            with patch("services.match_webapp_access.get_state", return_value=None):
                _, _, error = self.admin._match_rest_user_and_match(
                    self.db, self.me.id, self.casual.id)
        self.assertIsNone(error)

    def test_squad_economy_read_only_and_cancel_paths_remain_open(self):
        for path in ("/api/webapp/xi/reorder", "/api/webapp/daily",
                     "/api/webapp/tournament", "/api/webapp/match/state",
                     "/api/webapp/match/scorecard", "/api/webapp/match/abandon",
                     "/api/webapp/quickmatch/quota", "/api/webapp/quickmatch/abandon"):
            with self.subTest(path=path):
                with self.admin.app.test_request_context(
                        path, method="POST", json={"match_id": self.casual.id}):
                    self.assertIsNone(self.admin._tournament_only_webapp_block(self.db, self.me))

    def test_quickmatch_can_be_abandoned_while_restricted(self):
        response = self.client.post("/api/webapp/quickmatch/abandon",
                                    json={}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()["ok"])

    def test_restricted_autoplay_cannot_be_enabled_but_can_be_disabled(self):
        payload = {"userId": self.me.id, "matchId": self.casual.id, "active": True}
        self.assertBlocked(self.client.post("/api/match/autoplay-status", json=payload))
        payload["active"] = "false"
        with patch("services.match_webapp_access.save_autoplay_users",
                   return_value={"match_id": self.casual.id}) as save:
            with patch("services.crickidex_arena.serialize_match_state", return_value={}):
                response = self.client.post("/api/match/autoplay-status", json=payload)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertFalse(save.call_args.args[2])


if __name__ == "__main__":
    unittest.main()

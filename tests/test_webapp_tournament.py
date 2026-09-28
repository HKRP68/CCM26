"""The Mini App's Tournament tab: when it shows, and what its endpoints serve.

Runs against the real Flask endpoints on a throwaway SQLite database: a live
Challenge League tournament with four teams, two results (one with per-player
scorecard lines) and two fixtures still to play.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap  # noqa: E402  (sibling helper; see its docstring)

_PREV_ENV = {}
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "admin",
                 "services.tournament_service", "services.tournament_webapp",
                 "services.tournament_mvp", "services.cl_tournament_view",
                 "services.injury_service", "services.league_schedule_service",
                 "services.season_archive", "services.match_webapp_service")

TG_ID = 8_800_200_555
OTHER_TG = 8_800_200_556


def setUpModule():
    global _PREV_ENV, _SAVED_MODULES, _TMP, _ENGINE

    _PREV_ENV = {k: os.environ.get(k)
                 for k in ("DATABASE_URL", "WEBAPP_DEV_MODE", "BOT_TOKEN",
                           "ADMIN_SECRET")}
    _SAVED_MODULES = _module_swap.save(_MODULE_NAMES)
    _module_swap.unload(_MODULE_NAMES)

    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"
    os.environ["WEBAPP_DEV_MODE"] = "1"
    os.environ.setdefault("BOT_TOKEN", "test-token")
    os.environ.setdefault("ADMIN_SECRET", "test-secret")

    from database import Base, engine
    import models  # noqa: F401

    _ENGINE = engine
    Base.metadata.create_all(bind=engine)


def tearDownModule():
    try:
        _ENGINE.dispose()
    except Exception:
        pass
    for key, value in _PREV_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    _module_swap.restore(_SAVED_MODULES)
    try:
        os.unlink(_TMP.name)
    except Exception:
        pass


def _line(uid, name, team, **kw):
    base = {"user_id": uid, "player_id": None, "roster_id": None, "name": name,
            "team_name": team, "bat_runs": 0, "bat_balls": 0, "bat_fours": 0,
            "bat_sixes": 0, "bat_out": False, "batted": False,
            "bowl_wickets": 0, "bowl_runs": 0, "bowl_balls": 0, "bowled": False}
    base.update(kw)
    return base


class WebappTournamentTests(unittest.TestCase):

    def setUp(self):
        import admin
        from database import SessionLocal
        from models import (Tournament, TournamentMatch, TournamentPlayerStats,
                            TournamentTeam, User)

        self.admin = admin
        self._real_post = admin.post_miniapp_activity
        admin.post_miniapp_activity = lambda *a, **k: None
        admin._tournament_recompute_at.clear()
        admin.app.config["TESTING"] = True
        self.client = admin.app.test_client()

        db = SessionLocal()
        self.db = db
        for model in (TournamentPlayerStats, TournamentMatch, TournamentTeam, Tournament):
            db.query(model).delete()
        db.query(User).filter(User.telegram_id.in_([TG_ID, OTHER_TG])).delete(
            synchronize_session=False)
        db.commit()

        me = User(telegram_id=TG_ID, first_name="Tester", total_coins=0,
                  total_gems=0, roster_count=0)
        other = User(telegram_id=OTHER_TG, first_name="Other", total_coins=0,
                     total_gems=0, roster_count=0)
        db.add_all([me, other])
        db.commit()
        self.uid, self.other_uid = me.id, other.id

        tour = Tournament(name="Summer Cup", kind="challenge", status="active",
                          is_active=True, overs=20, activated_at=datetime.utcnow(),
                          knockout_type="top4_sf")
        db.add(tour)
        db.commit()
        self.tid = tour.id

        names = ["Mumbai Masters", "Delhi Dynamos", "Chennai Chargers", "Kolkata Kings"]
        teams = []
        for i, n in enumerate(names):
            tt = TournamentTeam(tournament_id=tour.id, name=n, sort_order=i,
                                owner_tg_id=TG_ID if i == 0 else None)
            db.add(tt)
            teams.append(tt)
        db.commit()
        self.teams = [t.id for t in teams]
        a, b, c, d = self.teams

        lines = [
            _line(self.uid, "Rohit", "Mumbai Masters", bat_runs=72, bat_balls=40,
                  bat_fours=6, bat_sixes=4, bat_out=True, batted=True),
            _line(self.uid, "Bumrah", "Mumbai Masters", bowl_wickets=3,
                  bowl_runs=22, bowl_balls=24, bowled=True),
            _line(self.other_uid, "Pant", "Delhi Dynamos", bat_runs=41,
                  bat_balls=30, bat_out=True, batted=True),
            _line(self.other_uid, "Axar", "Delhi Dynamos", bowl_wickets=1,
                  bowl_runs=35, bowl_balls=24, bowled=True),
        ]
        m1 = TournamentMatch(tournament_id=tour.id, team1_id=a, team2_id=b,
                             winner_team_id=a, status="completed", stage="league",
                             round_no=1, match_no=1, inn1_runs=180, inn1_wickets=5,
                             inn1_balls=120, inn2_runs=150, inn2_wickets=9,
                             inn2_balls=120, result_text="Mumbai Masters won by 30 runs",
                             scorecard_json=json.dumps(lines),
                             completed_at=datetime.utcnow())
        m2 = TournamentMatch(tournament_id=tour.id, team1_id=c, team2_id=d,
                             winner_team_id=d, status="completed", stage="league",
                             round_no=1, match_no=2, inn1_runs=140, inn1_wickets=8,
                             inn1_balls=120, inn2_runs=141, inn2_wickets=3,
                             inn2_balls=100, result_text="Kolkata Kings won by 7 wickets",
                             completed_at=datetime.utcnow())
        m3 = TournamentMatch(tournament_id=tour.id, team1_id=a, team2_id=c,
                             status="scheduled", stage="league", round_no=2,
                             match_no=3, venue="Wankhede")
        m4 = TournamentMatch(tournament_id=tour.id, team1_id=b, team2_id=d,
                             status="scheduled", stage="league", round_no=2,
                             match_no=4)
        db.add_all([m1, m2, m3, m4])
        db.commit()
        self.m1, self.m2, self.m3 = m1.id, m2.id, m3.id

    def tearDown(self):
        self.admin.post_miniapp_activity = self._real_post
        self.db.close()

    def _post(self, path, body=None, tg=TG_ID):
        res = self.client.post(path, headers={"Authorization": f"tma DEV_{tg}",
                                              "Content-Type": "application/json"},
                               json=body or {})
        return res.status_code, res.get_json()

    def _deactivate(self):
        from models import Tournament
        self.db.query(Tournament).update({Tournament.is_active: False})
        self.db.commit()

    # ── /init flag ──────────────────────────────────────────────────────
    def test_live_summary_lists_the_active_tournament(self):
        from database import SessionLocal
        from services import tournament_webapp as tw
        s = SessionLocal()
        try:
            info = tw.live_summary(s)
        finally:
            s.close()
        self.assertTrue(info["live"])
        self.assertEqual([t["id"] for t in info["tournaments"]], [self.tid])

    def test_live_summary_is_empty_without_an_active_tournament(self):
        self._deactivate()
        from database import SessionLocal
        from services import tournament_webapp as tw
        s = SessionLocal()
        try:
            info = tw.live_summary(s)
        finally:
            s.close()
        self.assertEqual(info, {"live": False, "tournaments": []})

    # ── /tournament ─────────────────────────────────────────────────────
    def test_tournament_payload(self):
        status, data = self._post("/api/webapp/tournament")
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"] and data["live"])
        t = data["tournament"]
        self.assertEqual(t["name"], "Summer Cup")
        self.assertEqual((t["matches_played"], t["matches_total"]), (2, 4))
        self.assertEqual(t["qualify"], 4)

        # Standings come from the recorded results (recompute ran).
        table = data["table"]
        self.assertEqual(len(table), 4)
        self.assertEqual(table[0]["name"], "Mumbai Masters")
        self.assertEqual(table[0]["points"], 2)
        self.assertTrue(table[0]["is_mine"])
        self.assertEqual(table[0]["form"], ["W"])
        self.assertEqual(data["my_team_ids"], [self.teams[0]])

        fx = {f["id"]: f for f in data["fixtures"]}
        self.assertEqual(fx[self.m1]["score1"], "180/5 (20.0)")
        self.assertTrue(fx[self.m1]["has_scorecard"])
        self.assertEqual(data["next_fixture_id"], self.m3)
        self.assertTrue(fx[self.m3]["is_next"])
        self.assertEqual(sum(1 for f in data["fixtures"] if f["status"] == "scheduled"), 2)

    def test_not_mine_for_another_viewer(self):
        status, data = self._post("/api/webapp/tournament", tg=OTHER_TG)
        self.assertEqual(status, 200)
        self.assertEqual(data["my_team_ids"], [])

    def test_no_live_tournament(self):
        self._deactivate()
        status, data = self._post("/api/webapp/tournament")
        self.assertEqual(status, 200)
        self.assertFalse(data["live"])

    def test_inactive_tournament_id_is_refused(self):
        self._deactivate()
        status, data = self._post("/api/webapp/tournament", {"tournament_id": self.tid})
        self.assertFalse(data["live"])
        status, _ = self._post("/api/webapp/tournament/stats", {"tournament_id": self.tid})
        self.assertEqual(status, 404)
        status, _ = self._post("/api/webapp/tournament/match",
                               {"tournament_id": self.tid, "fixture_id": self.m1})
        self.assertEqual(status, 404)

    # ── /tournament/stats ───────────────────────────────────────────────
    def test_stats_boards_and_mvp(self):
        self._post("/api/webapp/tournament")  # triggers the recompute
        status, data = self._post("/api/webapp/tournament/stats",
                                  {"tournament_id": self.tid})
        self.assertEqual(status, 200, data)
        boards = {b["key"]: b for b in data["boards"]}
        self.assertIn("runs", boards)
        self.assertEqual(boards["runs"]["rows"][0]["name"], "Rohit")
        self.assertEqual(boards["runs"]["rows"][0]["value"], "72")
        self.assertEqual(boards["wkts"]["rows"][0]["name"], "Bumrah")
        self.assertEqual(boards["fifties"]["rows"][0]["name"], "Rohit")
        self.assertTrue(data["mvp"])

    # ── /tournament/match ───────────────────────────────────────────────
    def test_scorecard_falls_back_to_player_lines(self):
        status, data = self._post("/api/webapp/tournament/match",
                                  {"tournament_id": self.tid, "fixture_id": self.m1})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["summary_only"])
        inn1, inn2 = data["innings"]
        self.assertEqual(inn1["bat_team"], "Mumbai Masters")
        self.assertEqual([b["name"] for b in inn1["batting"]], ["Rohit"])
        self.assertEqual([b["name"] for b in inn1["bowling"]], ["Axar"])
        self.assertEqual(inn1["runs"], 180)
        self.assertEqual([b["name"] for b in inn2["batting"]], ["Pant"])
        self.assertEqual(inn2["bowling"][0]["overs"], "4.0")

    def test_scorecard_missing(self):
        status, data = self._post("/api/webapp/tournament/match",
                                  {"tournament_id": self.tid, "fixture_id": self.m2})
        self.assertEqual(status, 404)
        status, data = self._post("/api/webapp/tournament/match",
                                  {"tournament_id": self.tid, "fixture_id": self.m3})
        self.assertEqual(status, 404)
        self.assertIn("hasn't been played", data["message"])


if __name__ == "__main__":
    unittest.main()

"""Challenge Data admin: versioned bulk add, team file import, RCPL toggle.

  • ``Virat Kohli - Gold`` in Bulk Add adds the Gold card, not the Base one;
    a plain name prefers the Base card; a hyphenated name still resolves
  • one ``TEAM NAME | PLAYER NAME | VERSION`` sheet creates every team and
    its squad, skipping duplicates and reporting rows it could not place
  • a league switched out of Auction League is not offered by /rcpl

Follows the throwaway-sqlite pattern from
``tests/test_admin_fixture_delete_and_reset.py``.
"""

import io
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
                 "handlers.challenge", "handlers.auction_league",
                 "services.auction_league_service")


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
    from database import Base, engine
    import models  # noqa: F401
    _ENGINE = engine
    Base.metadata.create_all(bind=engine)
    _seed()


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


IDS = {}


def _seed():
    from database import get_session
    from models import ChallengeLeague, ChallengeMode, Player
    s = get_session()
    try:
        def card(name, version, rating, country="India"):
            p = Player(name=name, version=version, rating=rating, category="Batsman",
                       country=country, bat_hand="Right", bowl_hand="Right",
                       bowl_style="Medium Pacer", bat_rating=rating, bowl_rating=30)
            s.add(p)
            s.flush()
            IDS[(name, version)] = p.id

        card("Virat Kohli", "Gold", 95)
        card("Virat Kohli", "Base", 88)
        card("Jasprit Bumrah", "Base", 90)
        card("Rohit Sharma", "Base", 87)
        card("Jean-Paul Duminy", "Base", 80, country="South Africa")
        mode = ChallengeMode(name="Leagues")
        s.add(mode)
        s.flush()
        lg = ChallengeLeague(mode_id=mode.id, name="IPL", short_code="IPL",
                             command="/cipl", home_country="India")
        s.add(lg)
        s.commit()
        IDS["league"] = lg.id
    finally:
        s.close()


class ChallengeDataImportTests(unittest.TestCase):

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

    def _team_cards(self, team_name):
        from models import ChallengePlayer, ChallengeTeam
        self.session.expire_all()
        team = (self.session.query(ChallengeTeam)
                .filter(ChallengeTeam.league_id == IDS["league"],
                        ChallengeTeam.name == team_name).first())
        if team is None:
            return None
        return sorted(cp.source_player_id for cp in
                      self.session.query(ChallengePlayer)
                      .filter(ChallengePlayer.team_id == team.id).all())

    # ── resolver ──────────────────────────────────────────────────────────
    def test_version_suffix_picks_that_card(self):
        p, _ = self.admin._resolve_challenge_source(self.session, "Virat Kohli - Gold")
        self.assertEqual(p.id, IDS[("Virat Kohli", "Gold")])
        p, _ = self.admin._resolve_challenge_source(self.session, "virat kohli - base")
        self.assertEqual(p.id, IDS[("Virat Kohli", "Base")])

    def test_plain_name_prefers_the_base_card(self):
        p, _ = self.admin._resolve_challenge_source(self.session, "Virat Kohli")
        self.assertEqual(p.id, IDS[("Virat Kohli", "Base")])

    def test_hyphenated_name_and_missing_version(self):
        p, _ = self.admin._resolve_challenge_source(self.session, "Jean-Paul Duminy")
        self.assertEqual(p.id, IDS[("Jean-Paul Duminy", "Base")])
        p, reason = self.admin._resolve_challenge_source(self.session, "Rohit Sharma", "Gold")
        self.assertIsNone(p)
        self.assertIn("Gold", reason)

    # ── team file import ──────────────────────────────────────────────────
    def test_import_creates_teams_and_squads_from_a_csv(self):
        csv_bytes = (b"Team Name,Player Name,Version\n"
                     b"Mumbai Indians,Rohit Sharma,Base\n"
                     b"Mumbai Indians,Jasprit Bumrah,\n"
                     b"Royal Challengers,Virat Kohli,Gold\n"
                     b"Royal Challengers,Virat Kohli,Gold\n"
                     b"Royal Challengers,Nobody Here,\n")
        resp = self.client.post(f"/challenge-data/leagues/{IDS['league']}", data={
            "action": "import_teams",
            "teams_file": (io.BytesIO(csv_bytes), "teams.csv"),
        }, content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._team_cards("Mumbai Indians"),
                         sorted([IDS[("Rohit Sharma", "Base")], IDS[("Jasprit Bumrah", "Base")]]))
        self.assertEqual(self._team_cards("Royal Challengers"),
                         [IDS[("Virat Kohli", "Gold")]])

    def test_import_accepts_pasted_pipe_rows(self):
        resp = self.client.post(f"/challenge-data/leagues/{IDS['league']}", data={
            "action": "import_teams",
            "teams_text": "Pipe XI | Jean-Paul Duminy | Base\nPipe XI | Virat Kohli\nPipe XI | Virat Kohli | Gold\n",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._team_cards("Pipe XI"),
                         sorted([IDS[("Jean-Paul Duminy", "Base")], IDS[("Virat Kohli", "Base")]]))

    # ── bulk add with versions ────────────────────────────────────────────
    def test_bulk_add_honours_the_version_suffix(self):
        from models import ChallengeTeam
        team = ChallengeTeam(league_id=IDS["league"], name="Bulk XI")
        self.session.add(team)
        self.session.commit()
        resp = self.client.post(
            f"/challenge-data/leagues/{IDS['league']}/teams/{team.id}",
            data={"action": "bulk_add_players",
                  "bulk_text": "Virat Kohli - Gold\nVirat Kohli - Base\nRohit Sharma - Base"})
        self.assertEqual(resp.status_code, 302)
        # One card per player per team: the Base line is a duplicate of Gold.
        self.assertEqual(self._team_cards("Bulk XI"),
                         sorted([IDS[("Virat Kohli", "Gold")], IDS[("Rohit Sharma", "Base")]]))

    # ── RCPL league toggle ────────────────────────────────────────────────
    def test_league_out_of_auction_is_not_offered_by_rcpl(self):
        from handlers import auction_league
        from models import ChallengeLeague
        self.assertEqual(auction_league._find_league_id("IPL"), IDS["league"])
        resp = self.client.post(f"/challenge-data/leagues/{IDS['league']}", data={
            "action": "save_league", "league_name": "IPL", "short_code": "IPL",
            "command": "/cipl", "league_is_active": "on", "multi_enabled": "on",
            "home_country": "India", "min_overseas": "0", "max_overseas": "11",
        })
        self.assertEqual(resp.status_code, 302)
        try:
            self.session.expire_all()
            self.assertFalse(self.session.get(ChallengeLeague, IDS["league"]).auction_enabled)
            self.assertIsNone(auction_league._find_league_id("IPL"))
        finally:
            lg = self.session.get(ChallengeLeague, IDS["league"])
            lg.auction_enabled = True
            self.session.commit()


if __name__ == "__main__":
    unittest.main()

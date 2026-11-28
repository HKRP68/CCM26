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
import json
import os
import sys
import tempfile
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

_TMP = None
_ENGINE = None
_SAVED_FACTORY = None
_SAVED_ENGINE = None


def setUpModule():
    """Point the app at a throwaway database without re-importing ``models``.

    Re-importing it builds a second declarative registry — another set of mapped
    classes for the same tables. Importing ``admin`` then configures mappers while
    both are live and models.py's relationship() strings resolve across the two.
    Mapper configuration is cached, so restoring the modules does not undo it: the
    next suite to query one of those tables gets ``FROM players, players``. This
    module used to break all 30 of test_t20_championship that way.

    ``get_session()`` reads ``SessionLocal`` from its module global at call time and
    every caller imports the function, so swapping it redirects the whole app with
    one registry and no re-import.
    """
    global _TMP, _ENGINE, _SAVED_FACTORY, _SAVED_ENGINE
    import database
    import models  # noqa: F401  (already imported; named so the tables are registered)
    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    _SAVED_FACTORY, _SAVED_ENGINE = database.SessionLocal, database.engine
    try:
        _ENGINE = create_engine(f"sqlite:///{_TMP.name}",
                                connect_args={"check_same_thread": False})
        database.Base.metadata.create_all(bind=_ENGINE)
        database.SessionLocal = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)
        database.engine = _ENGINE
        _seed()
    except BaseException:
        tearDownModule()
        raise


def tearDownModule():
    global _ENGINE
    import database
    if _SAVED_FACTORY is not None:
        database.SessionLocal = _SAVED_FACTORY
    if _SAVED_ENGINE is not None:
        database.engine = _SAVED_ENGINE
    if _ENGINE is not None:
        _ENGINE.dispose()
        _ENGINE = None
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

    def test_typed_hex_colours_are_normalised_on_create_and_edit(self):
        from models import ChallengeTeam
        self.client.post(f"/challenge-data/leagues/{IDS['league']}", data={
            "action": "add_team", "team_name": "Typed Hex XI",
            "primary_color": "ABC", "secondary_color": "#12AB34"})
        team = self.session.query(ChallengeTeam).filter_by(name="Typed Hex XI").one()
        self.assertEqual((team.primary_color, team.secondary_color), ("#aabbcc", "#12ab34"))
        url = f"/challenge-data/leagues/{IDS['league']}/teams/{team.id}"
        self.client.post(url, data={"action": "save_team", "primary_color": "000000",
                                    "secondary_color": "#fff"})
        self.session.refresh(team)
        self.assertEqual((team.primary_color, team.secondary_color), ("#000000", "#ffffff"))
        self.client.post(url, data={"action": "save_team", "primary_color": "#123456junk",
                                    "secondary_color": "#fff"})
        self.session.refresh(team)
        self.assertEqual(team.primary_color, "#000000")

    def test_team_colour_form_offers_typing_and_picking(self):
        response = self.client.get(f"/challenge-data/leagues/{IDS['league']}")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('name="primary_color" data-colour-hex', page)
        self.assertIn('name="secondary_color" data-colour-hex', page)
        self.assertIn('data-colour-picker', page)

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
    def test_json_import_accepts_each_missing_field(self):
        from models import ChallengeTeam
        for missing in ("team_name", "short_name", "logo_url", "primary_color", "secondary_color"):
            with self.subTest(missing=missing):
                name = f"Optional {missing} XI"
                row = {"team_name": name, "short_name": f"JSON-{missing}",
                       "logo_url": "https://example.com/logo.png",
                       "primary_color": "ABC", "secondary_color": "#12AB34"}
                row.pop(missing)
                response = self.client.post(f"/challenge-data/leagues/{IDS['league']}", data={
                    "action": "import_team_details",
                    "team_details_file": (io.BytesIO(json.dumps([row]).encode()), "teams.json"),
                }, follow_redirects=True)
                self.assertEqual(response.status_code, 200)
                self.assertIn("Created 1 team(s)", response.get_data(as_text=True))
                expected_name = row.get("team_name", row.get("short_name"))
                team = self.session.query(ChallengeTeam).filter_by(
                    league_id=IDS["league"], name=expected_name).one()
                self.assertEqual(team.short_name, row.get("short_name"))
                self.assertEqual(team.logo_url, row.get("logo_url"))
                self.assertEqual(team.primary_color, None if missing == "primary_color" else "#aabbcc")
                self.assertEqual(team.secondary_color, None if missing == "secondary_color" else "#12ab34")

    def test_json_partial_update_preserves_details_and_squad_in_this_league_only(self):
        from models import ChallengeLeague, ChallengePlayer, ChallengeTeam
        league = self.session.get(ChallengeLeague, IDS["league"])
        other_league = ChallengeLeague(mode_id=league.mode_id, name="JSON Other League")
        self.session.add(other_league)
        self.session.flush()
        team = ChallengeTeam(league_id=league.id, name="JSON Preserve XI", short_name="JP",
                             logo_url="/static/logo.png", primary_color="#123456",
                             secondary_color="#654321", is_active=False, sort_order=12)
        other = ChallengeTeam(league_id=other_league.id, name=team.name, primary_color="#111111")
        self.session.add_all([team, other])
        self.session.flush()
        self.session.add(ChallengePlayer(team_id=team.id, name="Existing Player"))
        self.session.commit()
        response = self.client.post(f"/challenge-data/leagues/{league.id}", data={
            "action": "import_team_details", "team_details_json": json.dumps({
                "Team Name": "json preserve xi", "Short Name": "", "Logo / Image": None,
                "Primary colour code": "#abc"})}, follow_redirects=True)
        self.assertIn("updated 1 team(s)", response.get_data(as_text=True))
        self.session.refresh(team)
        self.session.refresh(other)
        self.assertEqual((team.name, team.short_name, team.logo_url, team.primary_color,
                          team.secondary_color, team.sort_order, team.is_active),
                         ("JSON Preserve XI", "JP", "/static/logo.png", "#aabbcc", "#654321", 12, False))
        self.assertEqual([player.name for player in team.players], ["Existing Player"])
        self.assertEqual(other.primary_color, "#111111")
        # A short-name-only patch can also find an existing team.
        self.client.post(f"/challenge-data/leagues/{league.id}", data={
            "action": "import_team_details",
            "team_details_json": '{"short_name":"JP","secondary_colour":"fff"}'})
        self.session.refresh(team)
        self.assertEqual(team.secondary_color, "#ffffff")

    def test_json_export_can_be_imported_into_another_league(self):
        from models import ChallengeLeague, ChallengeTeam
        league = self.session.get(ChallengeLeague, IDS["league"])
        target = ChallengeLeague(mode_id=league.mode_id, name="JSON Roundtrip League")
        self.session.add(target)
        self.session.commit()
        response = self.client.get(f"/challenge-data/leagues/{league.id}/teams.json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/json")
        self.assertIn("attachment;", response.headers["Content-Disposition"])
        rows = response.get_json()
        self.assertTrue(rows)
        self.assertEqual(set(rows[0]), {"team_name", "short_name", "logo_url", "primary_color", "secondary_color"})
        imported = self.client.post(f"/challenge-data/leagues/{target.id}", data={
            "action": "import_team_details", "team_details_json": json.dumps(rows)}, follow_redirects=True)
        self.assertNotIn("team(s) skipped", imported.get_data(as_text=True))
        exported = self.client.get(f"/challenge-data/leagues/{target.id}/teams.json").get_json()
        self.assertEqual(sorted(rows, key=lambda r: r["team_name"]),
                         sorted(exported, key=lambda r: r["team_name"]))
        # Re-importing named teams updates them rather than making duplicates.
        self.client.post(f"/challenge-data/leagues/{target.id}", data={
            "action": "import_team_details", "team_details_json": json.dumps(rows)})
        self.assertEqual(self.session.query(ChallengeTeam).filter_by(league_id=target.id).count(), len(rows))

    def test_json_import_skips_bad_rows_and_accepts_friendly_labels(self):
        from models import ChallengeTeam
        rows = [None, {}, {"team_name": "Bad Colour XI", "primary_color": "red"},
                {"team_name": "Bad Logo XI", "logo_url": "javascript:alert(1)"},
                {"team_name": ["Bad Type"]}, {"team_name": "x" * 121},
                {"Team Name": "Friendly JSON XI", "Short Name": "FJ",
                 "Logo / Image": "/static/teams/fj.png", "Primary colour code": "123",
                 "Secondary colour code": "ABCDEF"}]
        response = self.client.post(f"/challenge-data/leagues/{IDS['league']}", data={
            "action": "import_team_details", "team_details_json": json.dumps({"teams": rows})},
            follow_redirects=True)
        page = response.get_data(as_text=True)
        self.assertIn("6 team(s) skipped", page)
        self.assertIn("Created 1 team(s)", page)
        team = self.session.query(ChallengeTeam).filter_by(name="Friendly JSON XI").one()
        self.assertEqual((team.short_name, team.logo_url, team.primary_color, team.secondary_color),
                         ("FJ", "/static/teams/fj.png", "#112233", "#abcdef"))
        self.assertIsNone(self.session.query(ChallengeTeam).filter_by(name="Bad Colour XI").first())

    def test_json_missing_name_and_short_name_gets_an_automatic_name(self):
        from models import ChallengeTeam
        response = self.client.post(f"/challenge-data/leagues/{IDS['league']}", data={
            "action": "import_team_details", "team_details_json": '[{"primary_color":"#123456"}]'},
            follow_redirects=True)
        self.assertIn("Created 1 team(s)", response.get_data(as_text=True))
        team = self.session.query(ChallengeTeam).filter_by(league_id=IDS["league"], name="Team 1").one()
        self.assertEqual(team.primary_color, "#123456")

    def test_json_invalid_documents_leave_teams_unchanged(self):
        from models import ChallengeTeam
        before = self.session.query(ChallengeTeam).count()
        for text in ("", "{broken", '"teams"', '{"teams":{}}'):
            with self.subTest(text=text):
                response = self.client.post(f"/challenge-data/leagues/{IDS['league']}", data={
                    "action": "import_team_details", "team_details_json": text}, follow_redirects=True)
                self.assertIn("Error:", response.get_data(as_text=True))
        self.assertEqual(self.session.query(ChallengeTeam).count(), before)

    def test_json_export_requires_login(self):
        response = self.admin.app.test_client().get(f"/challenge-data/leagues/{IDS['league']}/teams.json")
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.headers["Location"])

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

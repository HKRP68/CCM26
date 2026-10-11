"""🏟 League Seasons over HTTP: the pages render, and every button does its job.

  • competitions are created and seasons attached from a finished tournament;
  • ✍️ manual teams: the textarea comes pre-filled with last season's squads,
    a player on two teams is refused, and the new league becomes Season 2;
  • the season's tournament is created with last season's owners;
  • 🔁 mini: squads carry over and the release board gives a player back to
    the pool with his price back in the purse;
  • the sidebar highlights 🏟 League Seasons, not the monthly 🏆 Seasons.

Throwaway-sqlite pattern from ``tests/test_challenge_data_import.py``.
"""

import itertools
import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap  # noqa: E402

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "admin",
                 "services.auction_service", "services.league_season_service",
                 "services.tournament_service", "services.season_archive")
_TAG = itertools.count(1)


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


ALICE, BOB = 501, 502


def _season_one(session, tag):
    """A hand-made league of two squads and its finished tournament."""
    from models import (ChallengeLeague, ChallengeMode, ChallengePlayer,
                        ChallengeTeam, Player, Tournament, TournamentTeam)
    mode = session.query(ChallengeMode).first()
    if mode is None:
        mode = ChallengeMode(name="League Battles")
        session.add(mode)
        session.flush()
    league = ChallengeLeague(mode_id=mode.id, name=f"Old League {tag}",
                             command=f"cold{tag}", home_country="India")
    session.add(league)
    session.flush()
    cards = {}
    for team_name, names in (("Mumbai", ("Rohit", "Bumrah", "Surya")),
                             ("Chennai", ("Dhoni", "Jadeja", "Gaikwad"))):
        team = ChallengeTeam(league_id=league.id, name=team_name, short_name=team_name[:3].upper())
        session.add(team)
        session.flush()
        for i, name in enumerate(names):
            p = Player(name=f"{name} {tag}", version="Base", rating=80 + i,
                       category="Batsman", country="India", bat_hand="Right",
                       bowl_hand="Right", bowl_style="Medium Pacer",
                       bat_rating=80, bowl_rating=40, is_active=True)
            session.add(p)
            session.flush()
            cards[p.name] = p
            session.add(ChallengePlayer(team_id=team.id, source_player_id=p.id,
                                        name=p.name, sort_order=i))
    tour = Tournament(name=f"Cup {tag}", league_id=league.id, league_name=league.name,
                      kind="challenge", status="completed", format="Round Robin",
                      league_format="single_rr", overs=12, points_win=2,
                      completed_at=datetime(2026, 2, 1))
    session.add(tour)
    session.flush()
    for team in session.query(ChallengeTeam).filter(ChallengeTeam.league_id == league.id):
        owner = ALICE if team.name == "Mumbai" else BOB
        session.add(TournamentTeam(tournament_id=tour.id, challenge_team_id=team.id,
                                   name=team.name, owner_tg_id=owner,
                                   owner_name=f"Owner {owner}",
                                   points=6 if team.name == "Mumbai" else 2))
    session.commit()
    return league, tour, cards


class PageCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            import admin
        except Exception as exc:  # pragma: no cover - environment guard
            raise unittest.SkipTest(f"admin app unavailable: {exc}")
        admin.app.config["TESTING"] = True
        admin.app.config["WTF_CSRF_ENABLED"] = False
        cls.admin = admin

    def setUp(self):
        from database import get_session
        self.session = get_session()
        self.client = self.admin.app.test_client()
        with self.client.session_transaction() as s:
            s["admin"] = True
        self.tag = next(_TAG)
        self.league, self.tour, self.cards = _season_one(self.session, self.tag)

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def post(self, url, **data):
        return self.client.post(url, data=data)

    def new_series(self):
        from models import LeagueSeries
        r = self.post("/league-seasons", action="create_series",
                      name=f"IPL {self.tag}", short_name="IPL")
        self.assertEqual(302, r.status_code)
        self.session.expire_all()
        series = (self.session.query(LeagueSeries)
                  .filter(LeagueSeries.name == f"IPL {self.tag}").one())
        r = self.post(f"/league-seasons/{series.id}", action="attach",
                      tournament_id=str(self.tour.id), season_no="1")
        self.assertEqual(302, r.status_code)
        return series

    def seasons(self, series):
        from services import league_season_service as LS
        self.session.expire_all()
        return LS.seasons(self.session, series)


class PagesTests(PageCase):

    def test_pages_render_and_the_sidebar_lights_the_right_link(self):
        series = self.new_series()
        self.assertEqual(1, len(self.seasons(series)))
        home = self.client.get("/league-seasons").get_data(as_text=True)
        self.assertIn(f"IPL {self.tag}", home)
        self.assertIn('class="active">🏟 League Seasons', home)
        self.assertNotIn('class="active">🏆 Seasons', home)
        page = self.client.get(f"/league-seasons/{series.id}")
        self.assertEqual(200, page.status_code)
        text = page.get_data(as_text=True)
        self.assertIn("Start mega auction", text)
        self.assertIn("Start mini auction", text)
        self.assertIn("Mumbai", text)           # champion from the table

    def test_manual_teams_become_season_two_and_its_tournament(self):
        from models import ChallengeLeague, Tournament, TournamentTeam
        series = self.new_series()
        form = self.client.get(f"/league-seasons/{series.id}/manual").get_data(as_text=True)
        self.assertIn(f"Mumbai | Rohit {self.tag} | Base", form)

        dup = (f"Team Name | Player Name | Version\n"
               f"Mumbai | Rohit {self.tag}\nChennai | Rohit {self.tag}\n")
        self.post(f"/league-seasons/{series.id}/manual", teams_text=dup,
                  league_name=f"New {self.tag}")
        self.assertEqual(1, len(self.seasons(series)))

        text = (f"Mumbai | Rohit {self.tag}\nMumbai | Dhoni {self.tag}\n"
                f"Chennai | Bumrah {self.tag}\nChennai | Jadeja {self.tag}\n")
        r = self.post(f"/league-seasons/{series.id}/manual", teams_text=text,
                      league_name=f"New {self.tag}")
        self.assertEqual(302, r.status_code)
        rows = self.seasons(series)
        self.assertEqual(["existing", "manual"], [s.squad_mode for s in rows])
        new_league = self.session.get(ChallengeLeague, rows[1].league_id)
        self.assertEqual("India", new_league.home_country)

        r = self.post(f"/league-seasons/{series.id}", action="create_tournament",
                      season_id=str(rows[1].id), activate="on", hand_over="on")
        self.assertEqual(302, r.status_code)
        rows = self.seasons(series)
        tour = self.session.get(Tournament, rows[1].tournament_id)
        self.assertEqual(12, tour.overs)
        owners = {t.name: t.owner_tg_id for t in self.session.query(TournamentTeam)
                  .filter(TournamentTeam.tournament_id == tour.id)}
        self.assertEqual({"Mumbai": ALICE, "Chennai": BOB}, owners)
        self.session.expire_all()
        self.assertEqual(f"cold{self.tag}",
                         self.session.get(ChallengeLeague, new_league.id).command)
        self.assertFalse(self.session.get(ChallengeLeague, self.league.id).is_active)

    def test_mini_auction_and_the_release_board(self):
        from models import AuctionFranchise, AuctionLot
        from services import auction_service as A
        series = self.new_series()
        r = self.post(f"/league-seasons/{series.id}", action="start_mini",
                      purse="15")
        self.assertEqual(302, r.status_code)
        self.assertIn("/release", r.headers["Location"])
        season = self.seasons(series)[1]
        self.assertEqual("mini", season.squad_mode)
        board = self.client.get(r.headers["Location"])
        self.assertEqual(200, board.status_code)
        self.assertIn(f"Rohit {self.tag}", board.get_data(as_text=True))

        lot = (self.session.query(AuctionLot)
               .filter(AuctionLot.season_id == season.auction_season_id,
                       AuctionLot.name == f"Rohit {self.tag}").one())
        franchise = self.session.get(AuctionFranchise, lot.sold_to_id)
        self.assertEqual(ALICE, franchise.owner_tg_id)   # from the tournament
        before, price = franchise.purse_remaining_lakh, lot.sold_price_lakh
        self.post(r.headers["Location"], action="release", lot_ids=str(lot.id))
        self.session.expire_all()
        lot = self.session.get(AuctionLot, lot.id)
        self.assertEqual(A.LOT_QUEUED, lot.status)
        self.assertEqual(before + price,
                         self.session.get(AuctionFranchise, franchise.id).purse_remaining_lakh)

    def test_mega_auction_from_the_page(self):
        from models import AuctionSeason
        series = self.new_series()
        r = self.post(f"/league-seasons/{series.id}", action="start_mega",
                      purse="120", keep_max="4", rtm="on")
        self.assertEqual(302, r.status_code)
        season = self.seasons(series)[1]
        auction = self.session.get(AuctionSeason, season.auction_season_id)
        self.assertEqual(12000, auction.opening_purse_lakh)
        self.assertEqual(4, auction.max_retentions)
        page = self.client.get(f"/league-seasons/{series.id}").get_data(as_text=True)
        self.assertIn("Retention open", page)


if __name__ == "__main__":
    unittest.main()

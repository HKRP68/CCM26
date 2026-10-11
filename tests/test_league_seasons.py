"""League Seasons: a competition, its seasons, and the turn of a season.

What is pinned here:

  • seasons attach from a tournament, a saved season or by hand, and the
    stage is read off the auction and the tournament — never stored;
  • a deleted tournament leaves its season, its honours and its stats behind;
  • 🔁 mini: every squad carries at last season's price, the purse is the cap
    minus that, the ledger agrees from the first moment, refusals land in the
    pool, and the room is told once per franchise rather than once per player;
  • 🔨 mega: purses reset, the keep count and Right To Match cards are set;
  • an owner releases only his own kept player, only while the window is open;
  • the league's commands MOVE to the new season's league;
  • next season's tournament carries every rule column (and the list of rule
    columns cannot fall behind the model), its owners and its schedule;
  • all-time stats and a player's season-by-season record span live and
    archived seasons.
"""

import itertools
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap  # noqa: E402

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None

_MODULE_NAMES = ("database", "models", "config",
                 "services.player_service", "services.player_query",
                 "services.auction_service", "services.retention_negotiation",
                 "services.auction_scheduler", "services.tournament_service",
                 "services.season_archive", "services.league_season_service",
                 "services.league_schedule_service", "services.knockout_service",
                 "services.tournament_awards", "services.tournament_mvp",
                 "services.standings", "handlers.auction",
                 "handlers.league_seasons", "handlers.tournament")

_TAG = itertools.count(1)


def setUpModule():
    global _PREV_DATABASE_URL, _SAVED_MODULES, _TMP, _ENGINE
    _PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
    _SAVED_MODULES = _module_swap.save(_MODULE_NAMES)
    _module_swap.unload(_MODULE_NAMES)
    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"
    from database import Base, engine
    import models  # noqa: F401
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


ALICE, BOB, CAROL, DAVE = 111, 222, 333, 444
NOW = datetime(2026, 3, 1, 12, 0, 0)


class LeagueCase(unittest.TestCase):
    """Season 1 played: an auction, its published league, a finished
    tournament with owners and stats — the state a real competition is in
    when its admin opens 🏟 League Seasons."""

    def setUp(self):
        from database import get_session
        from models import Player
        from services import auction_service as A
        from services import league_season_service as LS

        self.session = get_session()
        self.A, self.LS = A, LS
        self.tag = next(_TAG)
        self.players = []
        for i, (name, rating, category) in enumerate([
                ("Virat", 95, "Batsman"), ("Rohit", 92, "Batsman"),
                ("Bumrah", 94, "Bowler"), ("Shami", 85, "Bowler"),
                ("Hardik", 88, "All Rounder"), ("Jadeja", 87, "All Rounder"),
                ("Pant", 86, "Wicket Keeper"), ("Gill", 84, "Batsman")]):
            p = Player(name=f"{name} {self.tag}", rating=rating, category=category,
                       country="India", version="Base", bat_hand="Right",
                       bowl_hand="Right", bowl_style="Medium Pacer",
                       bat_rating=rating, bowl_rating=rating - 10, is_active=True)
            self.session.add(p)
            self.players.append(p)
        self.session.flush()

        self.auction = A.create_season(
            self.session, f"S1 Auction {self.tag}", opening_purse_lakh=1000,
            min_squad_size=1, max_squad_size=6, min_base_price_lakh=20,
            max_overseas=4, home_country="India")
        A.bind_chat(self.session, self.auction, -7000 - self.tag)
        self.mumbai = A.create_franchise(self.session, self.auction, "Mumbai",
                                         owner_tg_id=ALICE, owner_name="Alice",
                                         sort_order=1)
        self.chennai = A.create_franchise(self.session, self.auction, "Chennai",
                                          owner_tg_id=BOB, owner_name="Bob",
                                          sort_order=2)
        self.session.commit()
        self.league = self._run_and_publish()
        self.tour = self._tournament(self.league, f"IPL 2025 {self.tag}")
        self.series = LS.create_series(self.session, f"IPL {self.tag}",
                                       short_name=f"I{self.tag}")
        self.s1 = LS.attach_season(self.session, self.series, tournament=self.tour)
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    # ── helpers ──

    def _run_and_publish(self):
        A = self.A
        A.add_players_to_pool(self.session, self.auction, self.players)
        self.session.commit()
        lot = A.start(self.session, self.auction, now=NOW)
        self.session.commit()
        buyers = (self.mumbai, self.chennai)
        n = 0
        while lot is not None and n < 30:
            # Prices that differ by lot, so "last season's price" is testable.
            A.place_bid(self.session, self.auction, lot, buyers[n % 2],
                        lot.base_price_lakh + 10 * (n + 1), now=NOW)
            A.sell_lot(self.session, self.auction, lot, now=NOW)
            self.session.commit()
            lot = A.open_next_lot(self.session, self.auction, now=NOW)
            self.session.commit()
            n += 1
        self.assertEqual(A.STATUS_COMPLETED, self.auction.status)
        league = A.publish_to_league(self.session, self.auction)
        league.command = f"cipl{self.tag}"
        league.tournament_command = f"tipl{self.tag}"
        league.short_code = f"IP{self.tag}"
        self.session.commit()
        return league

    def _tournament(self, league, name, *, status="completed", stats=True):
        from models import (ChallengeTeam, Tournament, TournamentPlayerStats,
                            TournamentTeam)
        tour = Tournament(name=name, league_id=league.id, league_name=league.name,
                          kind="challenge", status=status, format="IPL Playoffs",
                          league_format="double_rr", knockout_type="ipl_playoffs",
                          overs=10, points_win=4, points_tie=2, tiebreak="h2h",
                          injuries_enabled=True, enforce_team_owner=True,
                          completed_at=NOW if status == "completed" else None)
        self.session.add(tour)
        self.session.flush()
        owners = {"Mumbai": (ALICE, "Alice"), "Chennai": (BOB, "Bob")}
        for team in (self.session.query(ChallengeTeam)
                     .filter(ChallengeTeam.league_id == league.id).all()):
            owner = owners.get(team.name, (None, None))
            self.session.add(TournamentTeam(
                tournament_id=tour.id, challenge_team_id=team.id, name=team.name,
                owner_tg_id=owner[0], owner_name=owner[1],
                home_stadium="Wankhede" if team.name == "Mumbai" else None,
                points=8 if team.name == "Mumbai" else 4))
        if stats:
            from models import User
            user = (self.session.query(User)
                    .filter(User.telegram_id == 900000 + self.tag).first())
            if user is None:
                user = User(telegram_id=900000 + self.tag, first_name="Stats")
                self.session.add(user)
                self.session.flush()
            for i, p in enumerate(self.players[:4]):
                self.session.add(TournamentPlayerStats(
                    tournament_id=tour.id, user_id=user.id, player_id=p.id,
                    name=p.name,
                    team_name="Mumbai" if i % 2 == 0 else "Chennai",
                    matches=5, bat_innings=5, bat_runs=100 + 50 * i, bat_balls=80,
                    bat_sixes=i, bat_outs=3, highest_score=40 + i,
                    bowl_innings=4, bowl_wickets=i * 2, bowl_runs=90,
                    bowl_balls=96, best_bowl_wickets=i, best_bowl_runs=20))
        self.session.flush()
        return tour

    def held_price(self, player):
        from models import AuctionLot
        lot = (self.session.query(AuctionLot)
               .filter(AuctionLot.season_id == self.auction.id,
                       AuctionLot.player_id == player.id).first())
        return lot.sold_price_lakh, lot.sold_to_id


# ══════════════════════════════════════════════════════════════════════

class SeasonsTests(LeagueCase):

    def test_attach_reads_the_league_and_the_auction(self):
        self.assertEqual(1, self.s1.season_no)
        self.assertEqual(self.league.id, self.s1.league_id)
        self.assertEqual(self.auction.id, self.s1.auction_season_id)
        self.assertEqual("completed", self.LS.season_stage(self.session, self.s1)["key"])

    def test_honours_fall_back_to_the_table_and_the_leaders(self):
        h = self.LS.honours(self.s1)
        self.assertEqual("Mumbai", h["champion"]["team"])
        self.assertEqual("Chennai", h["runner_up"]["team"])
        self.assertIn("runs", h["orange_cap"]["value"])

    def test_a_tournament_is_one_season_only_and_numbers_are_unique(self):
        LS = self.LS
        with self.assertRaises(LS.LeagueSeasonError):
            LS.attach_season(self.session, self.series, tournament=self.tour)
        with self.assertRaises(LS.LeagueSeasonError):
            LS.attach_season(self.session, self.series, season_no=1,
                             champion="Someone")

    def test_a_season_before_the_bot_is_recorded_by_hand(self):
        LS = self.LS
        old = LS.attach_season(self.session, self.series, season_no=0 + 9,
                               champion="Deccan", runner_up="Bangalore")
        self.assertEqual("Deccan", LS.honours(old)["champion"]["team"])
        self.assertEqual("completed", LS.season_stage(self.session, old)["key"])

    def test_deleting_the_tournament_keeps_the_season(self):
        from services import season_archive
        LS = self.LS
        saved = season_archive.delete_tournament(self.session, self.tour,
                                                 keep_stats=True)
        self.session.commit()
        self.session.refresh(self.s1)
        self.assertIsNone(self.s1.tournament_id)
        self.assertEqual(saved.id, self.s1.stats_season_id)
        self.assertEqual("Mumbai", LS.honours(self.s1)["champion"]["team"])
        self.assertTrue(LS.season_rows(self.session, self.s1))

    def test_stage_follows_the_auction(self):
        LS = self.LS
        season, auction, _ = LS.start_mini_auction(
            self.session, self.series, purse_lakh=1100,
            deadline=NOW + timedelta(days=2), now=NOW)
        self.session.commit()
        self.assertEqual("window", LS.season_stage(self.session, season, NOW)["key"])
        self.A.lock_retention(self.session, auction, now=NOW)
        self.assertEqual("pool", LS.season_stage(self.session, season, NOW)["key"])


class MiniAuctionTests(LeagueCase):

    def start(self, purse=1100, **kw):
        season, auction, report = self.LS.start_mini_auction(
            self.session, self.series, purse_lakh=purse, now=NOW, **kw)
        self.session.commit()
        return season, auction, report

    def test_squads_carry_at_last_seasons_price(self):
        A = self.A
        season, auction, report = self.start()
        self.assertEqual("mini", season.squad_mode)
        self.assertEqual(2, season.season_no)
        kept = {lot.player_id: lot for lot in A.lots(self.session, auction.id)
                if lot.status == A.LOT_SOLD}
        self.assertEqual(len(self.players), len(kept))
        for player in self.players:
            price, _ = self.held_price(player)
            self.assertEqual(price, kept[player.id].sold_price_lakh)
        self.assertFalse(auction.rtm_enabled)
        for f in A.franchises(self.session, auction.id):
            spent = A.retention_spent(self.session, f.id)
            self.assertEqual(1100 - spent, f.purse_remaining_lakh)
            self.assertEqual(A.ledger_total(self.session, f.id),
                             f.purse_remaining_lakh)
            self.assertEqual(ALICE if f.name == "Mumbai" else BOB, f.owner_tg_id)

    def test_the_room_is_told_once_per_franchise(self):
        from models import AuctionEvent
        _season, auction, _ = self.start()
        kinds = [e.kind for e in self.session.query(AuctionEvent)
                 .filter(AuctionEvent.season_id == auction.id).all()]
        self.assertEqual(2, kinds.count("squads_carried"))
        self.assertNotIn("retained", kinds)

    def test_what_the_purse_cannot_hold_goes_to_the_pool(self):
        A = self.A
        _season, auction, report = self.start(purse=150)
        refused = sum(len(f["refused"]) for f in report["franchises"])
        self.assertGreater(refused, 0)
        self.assertEqual(refused, report["pooled"])
        queued = [l for l in A.lots(self.session, auction.id)
                  if l.status == A.LOT_QUEUED]
        self.assertEqual(refused, len(queued))
        self.assertTrue(all(l.set_name == A.RELEASED_LAST_SEASON_SET for l in queued))
        for f in A.franchises(self.session, auction.id):
            self.assertEqual(A.ledger_total(self.session, f.id),
                             f.purse_remaining_lakh)

    def test_an_owner_releases_his_own_player_and_nobody_elses(self):
        A = self.A
        _season, auction, _ = self.start()
        mumbai = next(f for f in A.franchises(self.session, auction.id)
                      if f.name == "Mumbai")
        lot = A.retained(self.session, mumbai.id)[0]
        before, price = mumbai.purse_remaining_lakh, lot.sold_price_lakh
        with self.assertRaises(A.AuctionError):
            A.release_by_owner(self.session, auction, lot, BOB)
        A.release_by_owner(self.session, auction, lot, ALICE)
        self.session.commit()
        self.session.refresh(mumbai)
        self.session.refresh(lot)
        self.assertEqual(A.LOT_QUEUED, lot.status)
        self.assertEqual(A.RELEASED_LAST_SEASON_SET, lot.set_name)
        self.assertEqual(before + price, mumbai.purse_remaining_lakh)
        self.assertEqual(A.ledger_total(self.session, mumbai.id),
                         mumbai.purse_remaining_lakh)

    def test_no_release_after_the_window(self):
        A = self.A
        _season, auction, _ = self.start(deadline=NOW + timedelta(hours=1))
        mumbai = next(f for f in A.franchises(self.session, auction.id)
                      if f.name == "Mumbai")
        lot = A.retained(self.session, mumbai.id)[0]
        with self.assertRaises(A.AuctionError):
            A.release_by_owner(self.session, auction, lot, ALICE,
                               now=NOW + timedelta(hours=2))
        A.lock_retention(self.session, auction, now=NOW)
        with self.assertRaises(A.AuctionError):
            A.release_by_owner(self.session, auction, lot, ALICE, now=NOW)

    def test_a_hand_made_league_is_priced_off_the_ladder(self):
        self.auction.league_id = None   # as if the league had been typed in
        self.session.commit()
        from models import ChallengePlayer
        for cp in self.session.query(ChallengePlayer).all():
            cp.details_json = None
        self.session.commit()
        self.s1.auction_season_id = None
        self.session.commit()
        _season, auction, _ = self.start()
        for lot in self.A.lots(self.session, auction.id):
            if lot.status == self.A.LOT_SOLD:
                self.assertEqual(self.A.base_price_for(auction, lot.rating),
                                 lot.sold_price_lakh)
        self.assertEqual(ALICE, next(f.owner_tg_id for f in
                                     self.A.franchises(self.session, auction.id)
                                     if f.name == "Mumbai"))

    def test_only_after_the_season_is_completed(self):
        self.tour.status = "active"
        self.session.commit()
        with self.assertRaises(self.LS.LeagueSeasonError):
            self.start()


class MegaAuctionTests(LeagueCase):

    def test_purses_reset_and_keeps_and_cards_are_set(self):
        A, LS = self.A, self.LS
        season, auction = LS.start_mega_auction(
            self.session, self.series, purse_lakh=2000, keep_max=3, now=NOW)
        self.session.commit()
        self.assertEqual("mega", season.squad_mode)
        self.assertEqual(3, auction.max_retentions)
        self.assertTrue(auction.rtm_enabled)
        self.assertEqual(self.league.id, auction.previous_league_id)
        for f in A.franchises(self.session, auction.id):
            self.assertEqual(2000, f.purse_remaining_lakh)
            self.assertEqual(3, f.rtm_cards_total)
        self.assertEqual("window", LS.season_stage(self.session, season, NOW)["key"])

    def test_an_auction_already_following_the_league_is_adopted(self):
        A, LS = self.A, self.LS
        already = A.season_from_league(self.session, self.league, "Made elsewhere")
        self.session.commit()
        _season, auction = LS.start_mega_auction(
            self.session, self.series, purse_lakh=2000, now=NOW)
        self.assertEqual(already.id, auction.id)


class NextTournamentTests(LeagueCase):

    def _publish_season_two(self):
        A, LS = self.A, self.LS
        season, auction, _ = LS.start_mini_auction(
            self.session, self.series, purse_lakh=1100, now=NOW)
        A.lock_retention(self.session, auction, now=NOW)
        # Nothing was released, so the pool is empty; finish it by hand.
        auction.status = A.STATUS_COMPLETED
        self.session.commit()
        league = LS.publish_season_squads(self.session, season)
        self.session.commit()
        return season, league

    def test_the_rule_list_covers_the_model(self):
        from models import Tournament
        LS = self.LS
        columns = {c.name for c in Tournament.__table__.columns}
        listed = set(LS.TOURNAMENT_RULE_FIELDS) | set(LS.TOURNAMENT_RUN_STATE)
        self.assertEqual([], sorted(columns - listed),
                         "classify these Tournament columns as a rule or run state")
        self.assertEqual([], sorted(listed - columns))
        self.assertFalse(set(LS.TOURNAMENT_RULE_FIELDS) & set(LS.TOURNAMENT_RUN_STATE))

    def test_tournament_carries_rules_owners_and_commands(self):
        from models import ChallengeLeague, TournamentTeam
        LS = self.LS
        season, league = self._publish_season_two()
        tour, _message = LS.create_season_tournament(self.session, season,
                                                     activate=True)
        self.session.commit()
        for field in LS.TOURNAMENT_RULE_FIELDS:
            if field == "max_teams":
                continue
            self.assertEqual(getattr(self.tour, field), getattr(tour, field), field)
        teams = {t.name: t for t in self.session.query(TournamentTeam)
                 .filter(TournamentTeam.tournament_id == tour.id).all()}
        self.assertEqual(ALICE, teams["Mumbai"].owner_tg_id)
        self.assertEqual("Wankhede", teams["Mumbai"].home_stadium)
        old = self.session.get(ChallengeLeague, self.league.id)
        self.assertFalse(old.is_active)
        self.assertIsNone(old.command)
        self.assertEqual(f"cipl{self.tag}", league.command)
        self.assertEqual(f"tipl{self.tag}", tour.command_snapshot)
        self.assertTrue(tour.is_active)
        self.assertFalse(self.tour.is_active)
        from services import season_archive
        self.assertIn(season_archive.tour_ref(self.tour.id),
                      season_archive.linked_refs(tour))

    def test_sync_picks_up_a_publish_made_elsewhere(self):
        A, LS = self.A, self.LS
        season, auction, _ = LS.start_mini_auction(
            self.session, self.series, purse_lakh=1100, now=NOW)
        auction.status = A.STATUS_COMPLETED
        league = A.publish_to_league(self.session, auction)
        self.session.commit()
        self.assertTrue(LS.sync_season(self.session, season))
        self.assertEqual(league.id, season.league_id)
        self.assertFalse(LS.sync_season(self.session, season))


class ManualAndHandOverTests(LeagueCase):

    def test_previous_squads_text_lists_every_player_with_version(self):
        text = self.LS.previous_squads_text(self.session, self.league)
        lines = text.splitlines()
        self.assertEqual("Team Name | Player Name | Version", lines[0])
        self.assertEqual(len(self.players) + 1, len(lines))
        self.assertTrue(all(line.endswith("| Base") for line in lines[1:]))

    def test_a_player_on_two_teams_is_refused(self):
        rows = [(2, ("A", "Virat", "Base")), (3, ("B", "virat", "Base")),
                (4, ("A", "Virat", "Base"))]
        self.assertEqual([3], [n for n, _m in self.LS.duplicate_rows(rows)])

    def test_manual_season_league_copies_settings_not_commands(self):
        LS = self.LS
        league = LS.new_season_league(self.session, self.series, self.league,
                                      f"Manual {self.tag}")
        self.assertEqual(self.league.home_country, league.home_country)
        self.assertIsNone(league.command)
        with self.assertRaises(LS.LeagueSeasonError):
            LS.start_manual_season(self.session, self.series, league)  # no teams


class StatsTests(LeagueCase):

    def test_all_time_spans_live_and_archived_seasons(self):
        from services import season_archive
        LS = self.LS
        # Season 2 on the same league for brevity, then Season 1 is deleted.
        tour2 = self._tournament(self.league, f"IPL 2026 {self.tag}")
        LS.attach_season(self.session, self.series, tournament=tour2)
        season_archive.delete_tournament(self.session, self.tour, keep_stats=True)
        self.session.commit()
        board = LS.all_time_leaders(self.session, self.series, "runs")
        top = board[0]
        self.assertEqual(self.players[3].name, top.name)
        self.assertEqual(2 * 250, top.bat_runs)
        self.assertEqual(2, top.seasons)
        hist = LS.player_history(self.session, self.series, self.players[3].name)
        self.assertEqual(2, len(hist["breakdown"]))
        self.assertEqual(500, hist["total"].bat_runs)

    def test_ambiguous_names_ask(self):
        hist = self.LS.player_history(self.session, self.series, str(self.tag))
        self.assertIn("choices", hist)

    def test_titles_count_champions(self):
        LS = self.LS
        LS.attach_season(self.session, self.series, season_no=5,
                         champion="Mumbai")
        self.assertEqual(("Mumbai", 2), LS.titles(self.session, self.series)[0][:2])


if __name__ == "__main__":
    unittest.main()

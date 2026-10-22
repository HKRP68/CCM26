"""Detailed /statstour counters survive rebuilds, legacy reads and archives."""

import itertools
import json
import unittest
from unittest.mock import patch

from tests.test_tournament_rating_rule import setUpModule, tearDownModule  # noqa: F401

_IDS = itertools.count(880_000)


class DetailedStatsTests(unittest.TestCase):
    def setUp(self):
        from database import get_session
        from models import Tournament, User
        from services import tournament_service as ts
        self.ts = ts
        self.session = get_session()
        self.user = User(telegram_id=next(_IDS), first_name="Owner")
        self.tour = Tournament(name=f"Detailed {next(_IDS)}")
        self.session.add_all([self.user, self.tour])
        self.session.flush()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def line(self, runs=0, wickets=0, out=False, **extra):
        return dict(user_id=self.user.id, roster_id=123, name="Player", team_name="XI",
                    batted=True, bat_runs=runs, bat_balls=20, bat_out=out,
                    bowled=True, bowl_wickets=wickets, bowl_balls=1, bowl_runs=6,
                    hattricks=0, **extra)

    def store(self, lines, potm=True):
        from models import TournamentMatch
        if potm:
            lines[0]["match_potm"] = {"name": "Player", "team": "XI"}
        match = TournamentMatch(tournament_id=self.tour.id, scorecard_json=json.dumps(lines))
        self.session.add(match)
        self.session.flush()
        return match

    def rebuild(self):
        from models import TournamentPlayerStats
        self.ts.recompute_player_stats(self.session, self.tour.id)
        self.session.flush()
        return self.session.query(TournamentPlayerStats).filter_by(tournament_id=self.tour.id).one()

    def test_milestones_ducks_hauls_and_not_out_tie_are_counted_per_innings(self):
        for runs, wickets, out in [(50, 3, True), (100, 5, True), (0, 0, False),
                                   (0, 0, True), (100, 0, False)]:
            self.store([self.line(runs, wickets, out)])
        row = self.rebuild()
        self.assertEqual((row.fifties, row.hundreds, row.ducks), (1, 2, 1))
        self.assertEqual((row.three_fers, row.five_fers), (1, 1))
        self.assertEqual((row.highest_score, row.highest_score_not_out), (100, True))
        self.assertEqual(row.potm, 5)
        self.assertEqual(self.rebuild().potm, 5)  # Rebuilding does not double awards.

    def test_legacy_unknown_awards_and_hattricks_remain_unknown_in_database(self):
        line = self.line(80, 3)
        line.pop("hattricks")
        self.store([line], potm=False)
        row = self.rebuild()
        self.session.expire(row)
        self.assertIsNone(row.hattricks)
        self.assertIsNone(row.potm)
        self.assertEqual(row.fifties, 1)

    def test_legacy_read_recovers_counters_and_archive_preserves_them(self):
        from services import season_archive
        self.store([self.line(100, 5)])
        row = self.rebuild()
        row.hundreds = None
        row.five_fers = None
        self.session.flush()
        recovered = self.ts.detailed_player_stats(self.session, self.tour.id, [row])[0]
        self.assertEqual((recovered.hundreds, recovered.five_fers), (1, 1))
        self.assertIsNone(row.hundreds)  # A stats read does not mutate the stored row.
        saved = season_archive.archive_tournament(self.session, self.tour)
        self.session.expire(saved, ["players"])
        archived = saved.players[0]
        self.assertEqual((archived.hundreds, archived.five_fers, archived.potm), (1, 1, 1))
        career = self.ts.CareerStats()
        career.add(archived)
        self.assertEqual((career.hundreds, career.highest_score_not_out), (1, True))

    def test_hattrick_flag_is_kept_in_match_snapshot(self):
        player = {"roster_id": 123, "name": "Player"}
        state = dict(bowl_team_id=self.user.id, bowl_team_name="XI", bowl_xi=[player],
                     bowl_stats={"123": {"balls": 3, "wickets": 3, "hattrick": True}})
        self.assertEqual(self.ts._collect_player_lines(state)[0]["hattricks"], 1)

    def test_requested_display_has_every_field_and_uses_legal_balls_for_rates(self):
        from handlers import tournament
        self.store([self.line(260, 0)])
        row = self.rebuild()
        batting, bowling = tournament._stat_pairs(row)
        self.assertEqual(dict(batting)["HS"], "260*")
        self.assertEqual(dict(bowling)["Overs"], "0.1")
        self.assertEqual(dict(bowling)["Econ"], "36.00")
        self.assertEqual(dict(bowling)["SR"], "0.0")
        for label in ("Inns:", "50s:", "100s:", "4s/6s:", "Ducks:", "3-Fers:",
                      "5-Fers:", "Hattricks:", "BBF:", "POTM awards:"):
            self.assertIn(label, tournament._stats_html(row))
        blocks = str(tournament._stat_tables(row))
        self.assertIn("Hattricks", blocks)
        self.assertIn("POTM awards", blocks)

    def test_no_dismissals_and_no_bowling_do_not_divide_by_zero(self):
        from handlers import tournament
        row = self.ts.CareerStats()
        batting, bowling = tournament._stat_pairs(row)
        self.assertEqual(dict(batting)["Avg"], "—")
        self.assertEqual(dict(bowling)["Overs"], "0.0")
        self.assertEqual(dict(bowling)["Econ"], "0.00")

    def test_unknown_saved_season_does_not_become_zero_in_career_total(self):
        row = self.ts.CareerStats()
        row.hattricks = None
        total = self.ts.CareerStats()
        total.add(row)
        self.assertIsNone(total.hattricks)

    def test_existing_database_migration_preserves_runs_and_leaves_new_counters_unknown(self):
        import database
        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import sessionmaker
        from models import StatsSeasonPlayer, TournamentPlayerStats

        engine = create_engine("sqlite://")
        database.Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine)
        try:
            with sessions() as session:
                session.add(TournamentPlayerStats(tournament_id=1, user_id=1, bat_runs=123))
                session.add(StatsSeasonPlayer(season_id=1, bat_runs=456))
                session.commit()
            with engine.begin() as conn:
                for table in ("tournament_player_stats", "stats_season_players"):
                    for field in self.ts.DETAILED_STAT_FIELDS:
                        conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {field}"))
            with patch.object(database, "engine", engine), patch.object(database, "SessionLocal", sessions):
                database._migrate_add_columns()
                database._migrate_add_columns()  # A second startup remains safe.
            with sessions() as session:
                current = session.query(TournamentPlayerStats).one()
                saved = session.query(StatsSeasonPlayer).one()
                self.assertEqual((current.bat_runs, saved.bat_runs), (123, 456))
                for row in (current, saved):
                    for field in self.ts.DETAILED_STAT_FIELDS:
                        self.assertIsNone(getattr(row, field), field)
        finally:
            engine.dispose()

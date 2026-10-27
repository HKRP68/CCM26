"""Preview, persistence, migrations and match/bracket integration for 1.5 RR."""

import itertools
import json
import os
import random
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap

_MODULE_NAMES = ("database", "models", "config", "admin",
                 "services.tournament_service", "services.league_schedule_service",
                 "services.knockout_service", "services.tournament_webapp",
                 "services.tournament_recap", "services.season_archive",
                 "services.league_record_publications", "services.injury_service",
                 "services.tournament_awards", "services.lp_tournament_service")
_PID = itertools.count(1)


def setUpModule():
    global _SAVED, _PREV_URL, _TMP, _ENGINE
    _SAVED = _module_swap.save(_MODULE_NAMES)
    _module_swap.unload(_MODULE_NAMES)
    _PREV_URL = os.environ.get("DATABASE_URL")
    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    os.environ["DATABASE_URL"] = "sqlite:///" + _TMP.name
    os.environ.setdefault("BOT_TOKEN", "test-token")
    from database import Base, engine
    import models  # noqa: F401
    _ENGINE = engine
    Base.metadata.create_all(engine)


def tearDownModule():
    _ENGINE.dispose()
    if _PREV_URL is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _PREV_URL
    _module_swap.restore(_SAVED)
    os.unlink(_TMP.name)


class PartialRoundRobinAdminTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import admin
        cls.admin = admin
        admin.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    def setUp(self):
        from database import get_session
        from models import ChallengeMode, ChallengeLeague, ChallengeTeam
        self.session = get_session()
        tag = next(_PID)
        mode = ChallengeMode(name=f"Partial mode {tag}")
        self.session.add(mode)
        self.session.flush()
        self.league = ChallengeLeague(mode_id=mode.id, name=f"Partial league {tag}", short_code="PRR")
        self.session.add(self.league)
        self.session.flush()
        self.cteams = []
        for i in range(11):
            ct = ChallengeTeam(league_id=self.league.id, name=f"Team {tag}-{i + 1}", sort_order=i)
            self.session.add(ct)
            self.cteams.append(ct)
        self.session.commit()
        self.client = self.admin.app.test_client()
        with self.client.session_transaction() as login:
            login["admin"] = True

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def create(self, n=11, **extra):
        from models import Tournament
        data = {"action": "create_tournament", "name": f"Partial cup {next(_PID)}",
                "league_id": self.league.id, "tour_format": "one_half_round_robin",
                "team_ids": [str(t.id) for t in self.cteams[:n]], "pitch_mode": "round"}
        data.update(extra)
        response = self.client.post("/tournaments", data=data)
        self.assertEqual(response.status_code, 302)
        self.session.expire_all()
        tour = self.session.query(Tournament).filter_by(league_id=self.league.id).first()
        self.assertIsNotNone(tour)
        self.assertIn("/schedule/preview", response.location)
        return tour

    def fixtures(self, tour):
        from models import TournamentMatch
        return (self.session.query(TournamentMatch).filter_by(tournament_id=tour.id)
                .order_by(TournamentMatch.match_no).all())

    def preview(self, tour):
        response = self.client.get(f"/tournaments/{tour.id}/schedule/preview")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        token = re.search(r'name="preview_fingerprint" value="([a-f0-9]+)"', html)
        self.assertIsNotNone(token)
        return html, token.group(1)

    def confirm(self, tour, token=None):
        token = token or self.preview(tour)[1]
        response = self.client.post(f"/tournaments/{tour.id}", data={
            "action": "generate_schedule", "preview_fingerprint": token, "return_to": "schedule"})
        self.assertEqual(response.status_code, 302)
        self.session.expire_all()
        return response

    def test_creation_and_preview_do_not_save_fixtures(self):
        tour = self.create()
        self.assertEqual(tour.league_format, "one_half_rr")
        self.assertEqual(tour.tiebreak, "nrr_wins_h2h")
        self.assertGreaterEqual(tour.max_teams, 11)
        self.assertFalse(tour.schedule_generated)
        self.assertEqual(self.fixtures(tour), [])
        html, token = self.preview(tour)
        self.assertIn("83</strong> league fixtures", html)
        self.assertIn("17 rounds", html)
        self.assertIn("10 teams play 15, 1 team plays 16", html)
        self.assertIn("Byes:", html)
        self.assertEqual(self.preview(tour)[1], token)
        self.assertEqual(self.fixtures(tour), [])

    def test_confirmation_saves_the_exact_preview_and_survives_a_new_session(self):
        from database import get_session
        from models import Tournament, TournamentMatch
        from services import league_schedule_service as schedule
        tour = self.create()
        expected = schedule.preview_schedule(self.session, tour.id)
        self.confirm(tour)
        actual = [(f.round_no, f.league_phase, f.team1_id, f.team2_id) for f in self.fixtures(tour)]
        self.assertEqual(actual, [(r["round_no"], r["phase"], a, b)
                                  for r in expected["rounds"] for a, b in r["pairs"]])
        self.assertTrue(tour.schedule_generated)
        for rnd in range(1, 18):
            pitches = {f.pitch_type for f in self.fixtures(tour) if f.round_no == rnd}
            self.assertEqual(len(pitches), 1)
            self.assertNotEqual(pitches, {None})
        with get_session() as restarted:
            saved = restarted.query(TournamentMatch).filter_by(tournament_id=tour.id).all()
            self.assertEqual(len(saved), 83)
            self.assertTrue(restarted.get(Tournament, tour.id).schedule_generated)
        for page in ("", "/schedule", "/dashboard", f"/schedule?team={actual[0][2]}"):
            response = self.client.get(f"/tournaments/{tour.id}{page}")
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True)[:500])

    def test_custom_target_and_playoff_places(self):
        from services import qualification, tournament_webapp
        tour = self.create(n=8, league_match_target="11", partial_knockout_type="top_n", playoff_qualifiers="6")
        self.assertEqual(tour.league_match_target, 11)
        self.assertEqual(qualification.spots_for(tour), 6)
        self.assertEqual(json.loads(tour.knockout_config_json), {"qualifiers": 6})
        self.confirm(tour)
        self.assertEqual(len(self.fixtures(tour)), 44)
        self.assertEqual(tournament_webapp.tournament_payload(self.session, tour)["tournament"]["qualify"], 6)

    def test_existing_tournament_can_select_format_playoffs_and_priority(self):
        from models import TournamentTeam
        from services import qualification, tournament_service
        tour = self.create(n=5)
        self.confirm(tour)
        self.client.post(f"/tournaments/{tour.id}", data={
            "action": "save_settings", "league_format": "single_rr"})
        self.session.expire_all()
        self.assertEqual(tour.league_format, "single_rr")
        self.assertFalse(tour.schedule_generated)
        self.client.post(f"/tournaments/{tour.id}", data={
            "action": "save_settings", "league_format": "one_half_rr",
            "league_match_target": "7", "tiebreak": "nrr_wins_h2h"})
        self.client.post(f"/tournaments/{tour.id}", data={
            "action": "save_knockout", "knockout_type": "top_n", "playoff_qualifiers": "3"})
        self.session.expire_all()
        self.assertEqual(tour.league_format, "one_half_rr")
        self.assertEqual(tour.league_match_target, 7)
        self.assertEqual(qualification.spots_for(tour), 3)
        team = self.session.query(TournamentTeam).filter_by(tournament_id=tour.id).order_by(TournamentTeam.id.desc()).first()
        self.client.post(f"/tournaments/{tour.id}", data={
            "action": "set_team_priority", "team_id": team.id, "priority": "-10"})
        self.session.expire_all()
        self.assertEqual(tournament_service.points_table(self.session, tour.id)[0].id, team.id)
        self.confirm(tour)
        self.assertEqual(len(self.fixtures(tour)), 18)

    def test_invalid_playoff_qualifiers_do_not_change_saved_settings(self):
        tour = self.create(n=5, partial_knockout_type="top_n", playoff_qualifiers="3")
        for invalid in ("1", "6", "3.5", "bad"):
            self.client.post(f"/tournaments/{tour.id}", data={
                "action": "save_knockout", "knockout_type": "top_n", "playoff_qualifiers": invalid})
            self.session.expire_all()
            self.assertEqual(json.loads(tour.knockout_config_json), {"qualifiers": 3})

    def test_unconfirmed_or_stale_requests_cannot_replace_fixtures(self):
        tour = self.create(n=8)
        old_token = self.preview(tour)[1]
        response = self.client.post(f"/tournaments/{tour.id}", data={"action": "generate_schedule"})
        self.assertIn("/schedule/preview", response.location)
        self.assertEqual(self.fixtures(tour), [])
        self.client.post(f"/tournaments/{tour.id}", data={"action": "save_settings", "league_match_target": "11"})
        self.session.expire_all()
        response = self.confirm(tour, old_token)
        self.assertIn("/schedule/preview", response.location)
        self.assertEqual(self.fixtures(tour), [])
        self.confirm(tour)
        self.assertEqual(len(self.fixtures(tour)), 44)

    def test_invalid_target_cannot_clear_an_existing_schedule(self):
        from services import league_schedule_service as schedule
        tour = self.create(n=8)
        self.confirm(tour)
        before = [f.id for f in self.fixtures(tour)]
        self.client.post(f"/tournaments/{tour.id}", data={"action": "save_settings", "league_match_target": "2"})
        self.session.expire_all()
        self.assertIsNone(tour.league_match_target)
        self.assertEqual([f.id for f in self.fixtures(tour)], before)
        tour.league_match_target = 2
        with self.assertRaises(ValueError):
            schedule.generate_schedule(self.session, tour.id)
        self.assertEqual([f.id for f in self.fixtures(tour)], before)

    def test_played_and_live_fixtures_cannot_be_regenerated(self):
        from services import league_schedule_service as schedule
        tour = self.create(n=5)
        self.confirm(tour)
        fixtures = self.fixtures(tour)
        before = [f.id for f in fixtures]
        for status in ("live", "completed"):
            fixtures[0].status = status
            self.session.flush()
            with self.assertRaises(ValueError):
                schedule.generate_schedule(self.session, tour.id)
            self.assertEqual([f.id for f in self.fixtures(tour)], before)

    def test_ties_and_no_results_award_points_without_polluting_nrr(self):
        from services import tournament_service as ts, standings
        tour = self.create(n=5)
        self.confirm(tour)
        fixtures = self.fixtures(tour)
        nr = fixtures[0]
        tour.points_no_result = 3  # distinguish NR points from tie points
        ts.record_manual_result(self.session, nr.id, inn1_runs=120, inn1_wickets=2,
                                inn1_overs="10", inn2_runs=50, inn2_wickets=1,
                                inn2_overs="4", winner_slot="no_result")
        table = ts.points_table(self.session, tour.id)
        involved = [t for t in table if t.id in (nr.team1_id, nr.team2_id)]
        self.assertTrue(all((t.played, t.no_result, t.tied, t.points, t._nrr) == (1, 1, 0, 3, 0)
                            for t in involved))
        tie = fixtures[1]
        ts.record_manual_result(self.session, tie.id, inn1_runs=130, inn1_wickets=3,
                                inn1_overs="20", inn2_runs=130, inn2_wickets=8,
                                inn2_overs="20", winner_slot="tie")
        table = ts.points_table(self.session, tour.id)
        self.assertEqual(sum(t.points for t in table), 8)
        self.assertEqual(sum(t.tied for t in table), 2)
        rebuilt = standings.tally([(t.id, t.name, 0) for t in table], [nr, tie], points_no_result=3)
        for team in table:
            self.assertEqual((rebuilt[team.id].points, standings.nrr(rebuilt[team.id])),
                             (team.points, team._nrr_sort))

    def test_both_phases_feed_standings_then_auto_seed_and_finish_playoffs(self):
        from services import tournament_service as ts
        tour = self.create(n=5, partial_knockout_type="top_n", playoff_qualifiers="3")
        self.confirm(tour)
        rng = random.Random(120)
        first = [f for f in self.fixtures(tour) if f.league_phase == 1]
        second = [f for f in self.fixtures(tour) if f.league_phase == 2]
        for fx in first:
            ts.simulate_fixture(self.session, fx.id, "1", rng)
        self.assertFalse(tour.knockout_generated)
        self.assertEqual(sum(t.played for t in ts.points_table(self.session, tour.id)), 20)
        for fx in second:
            ts.simulate_fixture(self.session, fx.id, "2", rng)
        self.assertTrue(tour.knockout_generated)
        table = ts.points_table(self.session, tour.id)
        self.assertEqual(sum(t.played for t in table), 30)
        self.assertEqual(sum(t.points for t in table), 30)
        self.assertTrue(any(t._nrr != 0 for t in table))
        league_stats = [(t.id, t.played, t.points, t._nrr) for t in table]
        knockout = [f for f in self.fixtures(tour) if f.stage != "league"]
        self.assertEqual(len(knockout), 2)
        self.assertEqual({t for f in knockout for t in (f.team1_id, f.team2_id) if t},
                         {t.id for t in table[:3]})
        for fx in sorted(knockout, key=lambda f: f.round_no):
            ts.simulate_fixture(self.session, fx.id, "1", rng)
        self.assertIsNotNone(ts.tournament_champion(self.session, tour.id))
        self.assertEqual([(t.id, t.played, t.points, t._nrr) for t in ts.points_table(self.session, tour.id)], league_stats)
        self.session.commit()

    def test_legacy_schema_migrates_idempotently_without_regenerating(self):
        import database
        from sqlalchemy import inspect, text
        tour = self.create(n=5)
        self.confirm(tour)
        before = [(f.id, f.team1_id, f.team2_id) for f in self.fixtures(tour)]
        self.session.commit()
        with database.engine.begin() as connection:
            connection.execute(text("ALTER TABLE tournaments DROP COLUMN league_match_target"))
            connection.execute(text("ALTER TABLE tournament_matches DROP COLUMN league_phase"))
            connection.execute(text("ALTER TABLE tournament_matches DROP COLUMN result_type"))
        database._migrate_add_columns()
        database._migrate_add_columns()
        self.assertIn("league_match_target", {c["name"] for c in inspect(database.engine).get_columns("tournaments")})
        self.assertIn("league_phase", {c["name"] for c in inspect(database.engine).get_columns("tournament_matches")})
        self.assertIn("result_type", {c["name"] for c in inspect(database.engine).get_columns("tournament_matches")})
        self.session.expire_all()
        self.assertEqual([(f.id, f.team1_id, f.team2_id) for f in self.fixtures(tour)], before)

    def test_cross_group_create_preview_and_exact_persistence(self):
        from models import TournamentGroup, TournamentTeam
        from services import league_schedule_service as schedule, qualification
        tour = self.create(n=10, tour_format="cross_group_round_robin")
        groups = self.session.query(TournamentGroup).filter_by(tournament_id=tour.id).all()
        self.assertEqual(len(groups), 2)
        self.assertEqual(tour.group_points_mode, "combined")
        self.assertEqual(tour.knockout_type, "ipl_playoffs")
        self.assertEqual(qualification.spots_for(tour), 4)
        self.assertEqual(self.fixtures(tour), [])
        for group in groups:
            self.assertEqual(self.session.query(TournamentTeam).filter_by(group_id=group.id).count(), 5)
        html, _ = self.preview(tour)
        self.assertIn("70</strong> league fixtures", html)
        self.assertIn("14 rounds", html)
        self.assertIn("one combined points table", html)
        plan = schedule.preview_schedule(self.session, tour.id)
        self.confirm(tour)
        actual = [(fx.round_no, fx.team1_id, fx.team2_id) for fx in self.fixtures(tour)]
        self.assertEqual(actual, [(r["round_no"], a, b) for r in plan["rounds"] for a, b in r["pairs"]])
        for page in ("", "/schedule", "/dashboard"):
            self.assertEqual(self.client.get(f"/tournaments/{tour.id}{page}").status_code, 200)

    def test_cross_group_membership_changes_require_a_fresh_preview(self):
        from models import TournamentTeam
        tour = self.create(n=10, tour_format="cross_group_round_robin")
        old_token = self.preview(tour)[1]
        teams = self.session.query(TournamentTeam).filter_by(tournament_id=tour.id).all()
        a = teams[0]
        b = next(t for t in teams if t.group_id != a.group_id)
        a.group_id, b.group_id = b.group_id, a.group_id
        self.session.commit()
        response = self.confirm(tour, old_token)
        self.assertIn("/schedule/preview", response.location)
        self.assertEqual(self.fixtures(tour), [])
        self.confirm(tour)
        self.assertEqual(len(self.fixtures(tour)), 70)

    def test_cross_group_draw_is_blocked_after_play_starts(self):
        from services import league_schedule_service as schedule
        tour = self.create(n=10, tour_format="cross_group_round_robin")
        self.confirm(tour)
        fixtures = self.fixtures(tour)
        before = [f.id for f in fixtures]
        fixtures[0].status = "live"
        self.session.commit()
        with self.assertRaises(ValueError):
            schedule.draw_balanced_groups(self.session, tour.id)
        self.assertEqual([f.id for f in self.fixtures(tour)], before)

    def test_cross_group_unequal_groups_do_not_replace_saved_fixtures(self):
        from models import TournamentTeam
        from services import league_schedule_service as schedule
        tour = self.create(n=10, tour_format="cross_group_round_robin")
        self.confirm(tour)
        before = [f.id for f in self.fixtures(tour)]
        team = self.session.query(TournamentTeam).filter_by(tournament_id=tour.id).first()
        team.group_id = None
        self.session.flush()
        with self.assertRaises(ValueError):
            schedule.generate_schedule(self.session, tour.id)
        self.assertEqual([f.id for f in self.fixtures(tour)], before)

    def test_cross_group_can_be_selected_and_redrawn_on_manage(self):
        from models import TournamentTeam
        tour = self.create(n=10)
        self.confirm(tour)
        self.client.post(f"/tournaments/{tour.id}", data={"action": "save_settings", "league_format": "cross_group_rr"})
        self.session.expire_all()
        self.assertEqual(tour.league_format, "cross_group_rr")
        self.assertFalse(tour.schedule_generated)
        self.assertEqual(self.fixtures(tour), [])
        self.confirm(tour)
        self.client.post(f"/tournaments/{tour.id}", data={"action": "draw_balanced_groups"})
        self.session.expire_all()
        self.assertEqual(self.fixtures(tour), [])
        self.assertEqual(len({t.group_id for t in self.session.query(TournamentTeam).filter_by(tournament_id=tour.id)}), 2)

    def test_cross_group_odd_teams_are_rejected_without_creating_a_draft(self):
        from models import Tournament
        response = self.client.post("/tournaments", data={
            "action": "create_tournament", "name": "Odd groups", "league_id": self.league.id,
            "tour_format": "cross_group_round_robin", "team_ids": [str(t.id) for t in self.cteams]})
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.session.query(Tournament).filter_by(league_id=self.league.id).first())

    def test_cross_group_waits_for_all_seventy_games_then_finishes_ipl_playoffs(self):
        from services import tournament_service as ts, tournament_webapp
        tour = self.create(n=10, tour_format="cross_group_round_robin")
        self.confirm(tour)
        rng = random.Random(410)
        league = self.fixtures(tour)
        for fixture in league:
            ts.simulate_fixture(self.session, fixture.id, "1", rng)
            if fixture.id != league[-1].id:
                self.assertFalse(tour.knockout_generated)
        self.assertTrue(tour.knockout_generated)
        table = ts.points_table(self.session, tour.id)
        self.assertTrue(all(t.played == 14 for t in table))
        self.assertEqual(sum(t.points for t in table), 140)
        payload = tournament_webapp.tournament_payload(self.session, tour)
        self.assertEqual(payload["tournament"]["qualify"], 4)
        knockout = [f for f in self.fixtures(tour) if f.stage != "league"]
        self.assertEqual({f.stage for f in knockout}, {"qualifier1", "eliminator", "qualifier2", "final"})
        for fixture in sorted(knockout, key=lambda f: (f.round_no, f.match_no)):
            ts.simulate_fixture(self.session, fixture.id, "1", rng)
        self.assertIsNotNone(ts.tournament_champion(self.session, tour.id))
        self.assertTrue(all(t.played == 14 for t in ts.points_table(self.session, tour.id)))

    def test_cross_group_odd_conversion_preserves_the_existing_schedule(self):
        tour = self.create(n=5)
        self.confirm(tour)
        before = [f.id for f in self.fixtures(tour)]
        self.client.post(f"/tournaments/{tour.id}", data={"action": "save_settings", "league_format": "cross_group_rr"})
        self.session.expire_all()
        self.assertEqual(tour.league_format, "one_half_rr")
        self.assertEqual([f.id for f in self.fixtures(tour)], before)

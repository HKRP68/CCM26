"""Editing a running tournament: points adjustments, and fixtures stuck on live.

What these pin down:

  • a manual points adjustment survives every rebuild of the table — which is
    the whole reason it is a column of its own and not a hand-edit of ``points``
  • it moves the team in the standings, is bounded, and clears cleanly
  • the table says an adjustment happened, and says why
  • a fixture left on 'live' by a match that ended without recording a result is
    put back to 'scheduled', while a fixture whose match is genuinely being
    played is left exactly where it is
  • recording a result fills the fixture the match actually reserved, not
    whichever fixture for that pair happens to come first
"""

import itertools
import os
import sys
import tempfile
import unittest

_TG = itertools.count(770_001)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config",
                 "services.tournament_service", "services.cl_tournament_view",
                 "services.league_schedule_service", "services.knockout_service")


def setUpModule():
    global _PREV_DATABASE_URL, _SAVED_MODULES, _TMP, _ENGINE

    _PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
    _SAVED_MODULES = {name: sys.modules.get(name) for name in _MODULE_NAMES}
    for name in _MODULE_NAMES:
        sys.modules.pop(name, None)

    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"

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
    for name, module in _SAVED_MODULES.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


class TournamentCase(unittest.TestCase):
    """Four teams in a single round-robin, with a schedule generated."""

    def setUp(self):
        from database import get_session
        from models import (ChallengeMode, ChallengeLeague, ChallengeTeam,
                            Tournament, TournamentTeam)
        from services import tournament_service, league_schedule_service

        self.session = get_session()
        self.ts = tournament_service
        self.lss = league_schedule_service

        mode = ChallengeMode(name=f"Mode {next(_TG)}")
        self.session.add(mode)
        self.session.flush()
        self.league = ChallengeLeague(mode_id=mode.id, name=f"League {next(_TG)}",
                                      short_code="TBL")
        self.session.add(self.league)
        self.session.flush()

        self.tour = Tournament(
            name="Table Cup", league_id=self.league.id,
            league_name=self.league.name, kind="challenge", status="active",
            league_format="single_rr", overs=20, max_teams=8)
        self.session.add(self.tour)
        self.session.flush()

        self.teams = {}
        for i, name in enumerate(("Alpha", "Bravo", "Charlie", "Delta")):
            ct = ChallengeTeam(league_id=self.league.id, name=name, sort_order=i)
            self.session.add(ct)
            self.session.flush()
            tt = TournamentTeam(tournament_id=self.tour.id,
                                challenge_team_id=ct.id, name=name, sort_order=i)
            self.session.add(tt)
            self.session.flush()
            self.teams[name] = tt
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    # ── helpers ──

    def _record(self, first, second, runs1, runs2, overs="20"):
        """Record a completed league match between two teams by name."""
        from models import TournamentMatch
        tm = TournamentMatch(
            tournament_id=self.tour.id, stage="league", status="completed",
            team1_id=self.teams[first].id, team2_id=self.teams[second].id,
            inn1_runs=runs1, inn1_wickets=5,
            inn1_balls=self.ts._overs_to_balls(overs),
            inn2_runs=runs2, inn2_wickets=6,
            inn2_balls=self.ts._overs_to_balls(overs),
            winner_team_id=(self.teams[first].id if runs1 > runs2 else
                            self.teams[second].id if runs2 > runs1 else None))
        self.session.add(tm)
        self.session.flush()
        self.ts.recompute_standings(self.session, self.tour.id)
        self.session.commit()
        return tm

    def _points(self, name):
        self.session.refresh(self.teams[name])
        return int(self.teams[name].points or 0)


class PointsAdjustmentTests(TournamentCase):
    def test_a_penalty_survives_the_next_recompute(self):
        self._record("Alpha", "Bravo", 180, 150)      # Alpha +2
        self.assertEqual(self._points("Alpha"), 2)

        self.ts.adjust_points(self.session, self.teams["Alpha"].id, -2,
                              note="slow over rate")
        self.session.commit()
        self.assertEqual(self._points("Alpha"), 0)

        # A second result rebuilds the whole table from the matches. The earned
        # points move; the penalty must still be there underneath them.
        self._record("Alpha", "Charlie", 200, 120)    # Alpha +2 again
        self.assertEqual(self._points("Alpha"), 2)    # 4 earned, -2 applied
        self.session.refresh(self.teams["Alpha"])
        self.assertEqual(self.teams["Alpha"].points_adjust, -2)
        self.assertEqual(self.teams["Alpha"].points_adjust_note, "slow over rate")

    def test_an_award_lifts_a_team_up_the_table(self):
        self._record("Alpha", "Bravo", 180, 150)      # Alpha 2, Bravo 0
        table = [tt.name for tt in self.ts.points_table(self.session, self.tour.id)]
        self.assertEqual(table[0], "Alpha")

        self.ts.adjust_points(self.session, self.teams["Bravo"].id, 4,
                              note="walkover awarded")
        self.session.commit()
        table = [tt.name for tt in self.ts.points_table(self.session, self.tour.id)]
        self.assertEqual(table[0], "Bravo")

    def test_deltas_accumulate_and_set_replaces(self):
        team_id = self.teams["Delta"].id
        self.ts.adjust_points(self.session, team_id, -2, note="first")
        self.ts.adjust_points(self.session, team_id, -2, note="second")
        self.session.commit()
        self.session.refresh(self.teams["Delta"])
        self.assertEqual(self.teams["Delta"].points_adjust, -4)
        self.assertEqual(self.teams["Delta"].points_adjust_note, "second")

        self.ts.adjust_points(self.session, team_id, set_to=-1, note="reduced")
        self.session.commit()
        self.session.refresh(self.teams["Delta"])
        self.assertEqual(self.teams["Delta"].points_adjust, -1)

    def test_clearing_removes_the_adjustment_and_its_reason(self):
        team_id = self.teams["Charlie"].id
        self.ts.adjust_points(self.session, team_id, -6, note="conduct")
        self.session.commit()
        self.ts.clear_points_adjust(self.session, team_id)
        self.session.commit()
        self.session.refresh(self.teams["Charlie"])
        self.assertEqual(self.teams["Charlie"].points_adjust, 0)
        self.assertIsNone(self.teams["Charlie"].points_adjust_note)

    def test_an_absurd_adjustment_is_refused(self):
        with self.assertRaises(ValueError):
            self.ts.adjust_points(self.session, self.teams["Alpha"].id, -500)
        with self.assertRaises(ValueError):
            self.ts.adjust_points(self.session, self.teams["Alpha"].id, "two")
        # Both a delta and a value is a contradiction, not a silent preference.
        with self.assertRaises(ValueError):
            self.ts.adjust_points(self.session, self.teams["Alpha"].id, -2, set_to=0)

    def test_the_table_says_an_adjustment_happened_and_why(self):
        from services import cl_tournament_view as ctv
        self._record("Alpha", "Bravo", 180, 150)
        self.ts.adjust_points(self.session, self.teams["Alpha"].id, -2,
                              note="slow over rate")
        self.session.commit()
        body = ctv.render_table(self.session, self.tour)
        self.assertIn("slow over rate", body)
        self.assertIn("Alpha*", body)     # starred in the fixed-width table
        # Bravo has no adjustment, so it carries no star.
        self.assertNotIn("Bravo*", body)

    def test_a_team_with_no_adjustment_is_untouched(self):
        self._record("Alpha", "Bravo", 180, 150)
        self.ts.adjust_points(self.session, self.teams["Alpha"].id, -2)
        self.session.commit()
        self.assertEqual(self._points("Bravo"), 0)
        self.session.refresh(self.teams["Bravo"])
        self.assertEqual(self.teams["Bravo"].points_adjust, 0)


class StaleFixtureTests(TournamentCase):
    """Fixtures that a finished match left showing as 'live'."""

    def setUp(self):
        super().setUp()
        from models import User
        self.lss.generate_schedule(self.session, self.tour.id)
        self.users = []
        for _ in range(2):
            user = User(telegram_id=next(_TG), username=f"u{next(_TG)}")
            self.session.add(user)
            self.users.append(user)
        self.session.commit()

    def _match(self, status="active"):
        from datetime import datetime
        from models import Match
        m = Match(user1_id=self.users[0].id, user2_id=self.users[1].id,
                  status=status, overs=20, tournament_id=self.tour.id,
                  created_at=datetime.utcnow())
        self.session.add(m)
        self.session.flush()
        return m

    def _reserve(self, first, second):
        fx = self.lss.reserve_fixture(self.session, self.tour.id,
                                      self.teams[first].id, self.teams[second].id)
        self.assertIsNotNone(fx)
        self.assertEqual(fx.status, "live")
        return fx

    def test_a_fixture_whose_match_is_over_goes_back_to_scheduled(self):
        fx = self._reserve("Alpha", "Bravo")
        match = self._match(status="active")
        self.lss.bind_fixture_match(self.session, fx.id, match.id)
        self.session.commit()

        # While the match is being played, nothing is touched.
        self.assertEqual(self.lss.heal_live_fixtures(self.session, self.tour.id), [])

        match.status = "completed"
        self.session.commit()
        healed = self.lss.heal_live_fixtures(self.session, self.tour.id)
        self.session.commit()
        self.assertEqual([f.id for f in healed], [fx.id])
        self.session.refresh(fx)
        self.assertEqual(fx.status, "scheduled")
        self.assertIsNone(fx.match_id)

    def test_an_abandoned_match_frees_its_fixture(self):
        fx = self._reserve("Charlie", "Delta")
        match = self._match(status="abandoned")
        self.lss.bind_fixture_match(self.session, fx.id, match.id)
        self.session.commit()
        self.assertEqual(len(self.lss.heal_live_fixtures(self.session, self.tour.id)), 1)

    def test_an_unbound_fixture_is_given_a_grace_period(self):
        fx = self._reserve("Alpha", "Charlie")
        self.session.commit()
        # Just reserved: the match may still be a second away from launching.
        self.assertEqual(self.lss.heal_live_fixtures(self.session, self.tour.id), [])
        # Hours later, with nothing ever bound, it is stale.
        healed = self.lss.heal_live_fixtures(self.session, self.tour.id,
                                             grace_minutes=0)
        self.session.commit()
        self.assertEqual([f.id for f in healed], [fx.id])
        self.session.refresh(fx)
        self.assertEqual(fx.status, "scheduled")

    def test_a_completed_fixture_is_never_healed(self):
        fx = self._reserve("Alpha", "Delta")
        fx.status = "completed"
        self.session.commit()
        self.assertEqual(
            self.lss.heal_live_fixtures(self.session, self.tour.id,
                                        grace_minutes=0), [])
        self.session.refresh(fx)
        self.assertEqual(fx.status, "completed")

    def test_binding_never_overwrites_a_recorded_match(self):
        fx = self._reserve("Bravo", "Charlie")
        first, second = self._match(), self._match()
        self.lss.bind_fixture_match(self.session, fx.id, first.id)
        # A second attempt must not steal the fixture from the match on it.
        self.assertIsNone(self.lss.bind_fixture_match(self.session, fx.id, second.id))
        self.session.refresh(fx)
        self.assertEqual(fx.match_id, first.id)

    def test_releasing_a_fixture_clears_the_match_it_was_bound_to(self):
        fx = self._reserve("Bravo", "Delta")
        match = self._match()
        self.lss.bind_fixture_match(self.session, fx.id, match.id)
        self.session.commit()
        self.lss.release_fixture(self.session, fx.id)
        self.session.commit()
        self.session.refresh(fx)
        self.assertEqual(fx.status, "scheduled")
        self.assertIsNone(fx.match_id)


class ReservedFixtureRecordingTests(TournamentCase):
    """A result fills the fixture its match reserved, not just any open one."""

    def setUp(self):
        super().setUp()
        self.tour.league_format = "double_rr"
        self.session.commit()
        self.lss.generate_schedule(self.session, self.tour.id)
        self.session.commit()

    def _legs(self, first, second):
        from models import TournamentMatch
        from sqlalchemy import and_, or_
        a, b = self.teams[first].id, self.teams[second].id
        return (self.session.query(TournamentMatch)
                .filter_by(tournament_id=self.tour.id)
                .filter(or_(and_(TournamentMatch.team1_id == a,
                                 TournamentMatch.team2_id == b),
                            and_(TournamentMatch.team1_id == b,
                                 TournamentMatch.team2_id == a)))
                .order_by(TournamentMatch.round_no, TournamentMatch.match_no).all())

    def test_the_second_leg_can_be_played_first(self):
        legs = self._legs("Alpha", "Bravo")
        self.assertEqual(len(legs), 2, "a double round-robin has two legs")
        second_leg = legs[1]
        second_leg.status = "live"
        self.session.commit()

        state = {
            "tournament_id": self.tour.id,
            "match_id": None,
            "reserved_fixture_id": second_leg.id,
            "tournament_team_by_user": {
                101: self.teams["Alpha"].challenge_team_id,
                202: self.teams["Bravo"].challenge_team_id,
            },
            "inn1_bat_team_id": 101, "inn1_bowl_team_id": 202,
            "bat_team_id": 202, "bowl_team_id": 101,
            "inn1_runs": 180, "inn1_wickets": 5, "total_runs": 150,
            "total_wickets": 8,
        }
        recorded = self.ts.record_tournament_match(self.session, state)
        self.session.commit()
        self.assertIsNotNone(recorded)
        self.assertEqual(recorded.id, second_leg.id)
        # The first leg is untouched and still there to be played.
        self.session.refresh(legs[0])
        self.assertEqual(legs[0].status, "scheduled")


if __name__ == "__main__":
    unittest.main()

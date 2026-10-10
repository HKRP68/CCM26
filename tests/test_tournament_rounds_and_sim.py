"""Round-by-round schedules, simulated fixtures and self-seeding playoffs.

What these pin down:

  • a generated league opens one round at a time: Round 2 can't be found,
    reserved or shown until every Round 1 fixture is completed
  • a fixture with ``round_no`` 0 and knockout fixtures are never round-locked
  • /tsim's service settles a fixture for team 1, team 2 or at random, with a
    score line that fits the overs and agrees with the winner
  • a fixture in a later round can't be simulated early
  • the last league result seeds the playoff bracket on its own — and does
    nothing when the tournament has no playoffs configured
"""

import itertools
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap  # noqa: E402  (sibling helper; see its docstring)

_TG = itertools.count(880_001)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config",
                 "services.tournament_service", "services.cl_tournament_view",
                 "services.cl_tournament_rich",
                 "services.league_schedule_service", "services.knockout_service")


def setUpModule():
    global _PREV_DATABASE_URL, _SAVED_MODULES, _TMP, _ENGINE

    _PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
    _SAVED_MODULES = _module_swap.save(_MODULE_NAMES)
    _module_swap.unload(_MODULE_NAMES)

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
    _module_swap.restore(_SAVED_MODULES)
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


class RoundCase(unittest.TestCase):
    """Four teams, single round-robin (3 rounds of 2), schedule generated."""

    KNOCKOUT = "top4_sf"

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
        league = ChallengeLeague(mode_id=mode.id, name=f"League {next(_TG)}",
                                 short_code="RND")
        self.session.add(league)
        self.session.flush()
        self.tour = Tournament(
            name="Round Cup", league_id=league.id, league_name=league.name,
            kind="challenge", status="active", league_format="single_rr",
            overs=20, max_teams=8, knockout_type=self.KNOCKOUT)
        self.session.add(self.tour)
        self.session.flush()
        self.teams = {}
        for i, name in enumerate(("Alpha", "Bravo", "Charlie", "Delta")):
            ct = ChallengeTeam(league_id=league.id, name=name, sort_order=i)
            self.session.add(ct)
            self.session.flush()
            tt = TournamentTeam(tournament_id=self.tour.id,
                                challenge_team_id=ct.id, name=name, sort_order=i)
            self.session.add(tt)
            self.session.flush()
            self.teams[name] = tt
        self.lss.generate_schedule(self.session, self.tour.id)
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def fixtures(self, round_no=None, stage="league"):
        from models import TournamentMatch
        q = (self.session.query(TournamentMatch)
             .filter_by(tournament_id=self.tour.id, stage=stage))
        if round_no is not None:
            q = q.filter_by(round_no=round_no)
        return q.order_by(TournamentMatch.match_no).all()

    def finish_round(self, round_no):
        last = None
        for fx in self.fixtures(round_no):
            if fx.status != "completed":
                last = self.ts.simulate_fixture(self.session, fx.id, "1")
        self.session.commit()
        return last


class RoundLockTests(RoundCase):
    def test_round_one_is_open_first(self):
        self.assertEqual(self.lss.current_round(self.session, self.tour.id), 1)
        progress = self.lss.round_progress(self.session, self.tour.id)
        self.assertEqual((progress["round"], progress["rounds"],
                          progress["played"], progress["total"]), (1, 3, 0, 2))

    def test_a_later_round_cannot_be_found_or_reserved(self):
        later = self.fixtures(2)[0]
        a, b = later.team1_id, later.team2_id
        self.assertIsNone(self.lss.find_open_fixture(self.session, self.tour.id, a, b))
        self.assertIsNone(self.lss.reserve_fixture(self.session, self.tour.id, a, b))
        self.assertNotIn(b, self.lss.remaining_opponents(self.session, self.tour.id, a))
        self.assertIsNotNone(
            self.lss.find_open_fixture(self.session, self.tour.id, a, b,
                                       include_locked=True))
        msg = self.lss.round_lock_message(self.session, self.tour.id, a, b)
        self.assertIn("Round 2", msg)
        self.assertIn("Round 1", msg)

    def test_finishing_a_round_opens_the_next(self):
        last = self.finish_round(1)
        self.assertEqual(self.lss.current_round(self.session, self.tour.id), 2)
        later = self.fixtures(2)[0]
        self.assertIsNotNone(self.lss.find_open_fixture(
            self.session, self.tour.id, later.team1_id, later.team2_id))
        self.assertIn("Round 2 is now open", self.ts.schedule_news(self.session, last))

    def test_a_round_zero_fixture_is_never_locked(self):
        extra = self.lss.add_fixture(self.session, self.tour.id,
                                     self.teams["Alpha"].id, self.teams["Bravo"].id)
        self.assertTrue(self.lss.fixture_in_open_round(self.session, extra))

    def test_the_fixture_list_shows_only_the_open_round(self):
        from services import cl_tournament_view as ctv
        text = ctv.render_fixtures(self.session, self.tour)
        self.assertIn("Round 1 of 3", text)
        self.assertEqual(text.count("⚪"), 2)
        self.assertIn("🔒 4 matches in later rounds", text)

    def test_a_live_fixture_in_a_closed_round_is_still_recordable(self):
        # A match reserved in Round 1 is recorded even if an admin settles the
        # rest of the round meanwhile: the result lookup ignores the lock.
        fx = self.fixtures(1)[0]
        self.lss.reserve_fixture(self.session, self.tour.id, fx.team1_id, fx.team2_id)
        self.assertEqual(self.ts._find_open_fixture(
            self.session, self.tour.id, fx.team1_id, fx.team2_id).id, fx.id)


class AllAtOnceTests(RoundCase):
    """``fixtures_all_at_once``: every fixture shown and playable, no lock."""

    def setUp(self):
        super().setUp()
        self.tour.fixtures_all_at_once = True
        self.session.commit()

    def test_a_later_round_can_be_found_and_reserved(self):
        later = self.fixtures(3)[0]
        a, b = later.team1_id, later.team2_id
        self.assertIsNotNone(self.lss.find_open_fixture(
            self.session, self.tour.id, a, b))
        self.assertIn(b, self.lss.remaining_opponents(self.session, self.tour.id, a))
        self.assertIsNone(self.lss.round_lock_message(
            self.session, self.tour.id, a, b))
        self.assertTrue(self.lss.fixture_in_open_round(self.session, later))
        self.assertIsNotNone(self.lss.reserve_fixture(
            self.session, self.tour.id, a, b))

    def test_a_later_round_can_be_simulated(self):
        self.ts.simulate_fixture(self.session, self.fixtures(3)[0].id, "1")

    def test_the_fixture_list_shows_every_fixture(self):
        from services import cl_tournament_view as ctv
        text = ctv.render_fixtures(self.session, self.tour)
        self.assertEqual(text.count("⚪"), 6)
        self.assertNotIn("🔒", text)
        self.assertNotIn("Round 1 of 3", text)
        self.assertIn("Full schedule", text)

    def test_no_round_opened_news(self):
        last = self.finish_round(1)
        self.assertNotIn("now open", self.ts.schedule_news(self.session, last))

    def test_switching_back_restores_the_round_lock(self):
        self.tour.fixtures_all_at_once = False
        self.session.commit()
        later = self.fixtures(2)[0]
        self.assertIsNone(self.lss.find_open_fixture(
            self.session, self.tour.id, later.team1_id, later.team2_id))


class SimulateTests(RoundCase):
    def test_each_outcome_picks_the_right_winner(self):
        a, b = self.fixtures(1)
        self.ts.simulate_fixture(self.session, a.id, "1")
        self.ts.simulate_fixture(self.session, b.id, "2")
        self.assertEqual(a.winner_team_id, a.team1_id)
        self.assertEqual(b.winner_team_id, b.team2_id)
        self.assertGreater(a.inn1_runs, a.inn2_runs)
        self.assertGreater(b.inn2_runs, b.inn1_runs)
        for fx in (a, b):
            self.assertEqual(fx.status, "completed")
            self.assertIn("(simulated)", fx.result_text)
            self.assertLessEqual(fx.inn1_balls, 120)
            self.assertLessEqual(fx.inn2_balls, 120)
            self.assertLessEqual(fx.inn2_wickets, 10)

    def test_random_still_decides_a_winner(self):
        fx = self.fixtures(1)[0]
        self.ts.simulate_fixture(self.session, fx.id, "random", rng=random.Random(3))
        self.assertIn(fx.winner_team_id, (fx.team1_id, fx.team2_id))

    def test_the_table_moves(self):
        fx = self.fixtures(1)[0]
        self.ts.simulate_fixture(self.session, fx.id, "1")
        winner = [t for t in self.teams.values() if t.id == fx.team1_id][0]
        self.session.refresh(winner)
        self.assertEqual(winner.won, 1)

    def test_a_later_round_cannot_be_simulated_early(self):
        with self.assertRaises(ValueError) as ctx:
            self.ts.simulate_fixture(self.session, self.fixtures(2)[0].id, "1")
        self.assertIn("Round 1", str(ctx.exception))

    def test_a_completed_fixture_cannot_be_simulated_again(self):
        fx = self.fixtures(1)[0]
        self.ts.simulate_fixture(self.session, fx.id, "1")
        with self.assertRaises(ValueError):
            self.ts.simulate_fixture(self.session, fx.id, "2")

    def test_a_bad_outcome_is_refused(self):
        with self.assertRaises(ValueError):
            self.ts.simulate_fixture(self.session, self.fixtures(1)[0].id, "3")

    def test_scorelines_scale_with_overs(self):
        rng = random.Random(7)
        for overs in (5, 10, 20, 50):
            for chaser_wins in (True, False):
                (r1, w1, b1), (r2, w2, b2) = self.ts.simulated_scoreline(
                    overs, chaser_wins, rng=rng)
                self.assertLessEqual(b1, overs * 6)
                self.assertLessEqual(b2, overs * 6)
                self.assertTrue(0 <= w1 <= 10 and 0 <= w2 <= 10)
                self.assertEqual(r2 > r1, chaser_wins)


class TsimCommandTests(RoundCase):
    """The /tsim pieces that don't need Telegram: listing, parsing, settling."""

    def setUp(self):
        super().setUp()
        import importlib
        self.ta = importlib.import_module("handlers.tournament_admin")

    def test_the_listing_offers_only_the_open_round(self):
        text, kb = self.ta._sim_listing(self.session, self.tour)
        self.assertIn("Round 1 of 3", text)
        rows = kb.inline_keyboard
        # two fixtures with three buttons each, then "simulate all"
        self.assertEqual(len(rows), 3)
        self.assertTrue(rows[0][0].callback_data.startswith(self.ta.CB_SIM))
        self.assertTrue(rows[-1][0].callback_data.startswith(self.ta.CB_SIM + "round"))

    def test_a_team_name_picks_its_slot(self):
        fx = self.fixtures(1)[0]
        name = [t.name for t in self.teams.values() if t.id == fx.team2_id][0]
        self.assertEqual(self.ta._sim_outcome(self.session, self.tour, fx, name), "2")
        self.assertEqual(self.ta._sim_outcome(self.session, self.tour, fx, "rand"),
                         "random")

    def test_simulating_the_round_reports_the_next_one(self):
        text = self.ta._simulate(self.session, self.tour,
                                 self.ta._open_fixtures(self.session, self.tour),
                                 "random")
        self.assertIn("2 fixtures simulated", text)
        self.assertIn("Round 2 is now open", text)


class AutoPlayoffTests(RoundCase):
    def test_the_last_league_result_seeds_the_playoffs(self):
        self.finish_round(1)
        self.finish_round(2)
        self.assertEqual(self.fixtures(stage="semifinal"), [])
        last = self.finish_round(3)
        self.session.refresh(self.tour)
        self.assertTrue(self.tour.knockout_generated)
        semis = self.fixtures(stage="semifinal")
        self.assertEqual(len(semis), 2)
        self.assertTrue(all(fx.team1_id and fx.team2_id for fx in semis))
        self.assertEqual(len(self.fixtures(stage="final")), 1)
        self.assertIn("Playoffs are set", self.ts.schedule_news(self.session, last))

    def test_the_bracket_is_simulable_through_to_a_champion(self):
        for rnd in (1, 2, 3):
            self.finish_round(rnd)
        for fx in self.fixtures(stage="semifinal"):
            self.ts.simulate_fixture(self.session, fx.id, "1")
        final = self.fixtures(stage="final")[0]
        self.assertTrue(final.team1_id and final.team2_id)
        self.ts.simulate_fixture(self.session, final.id, "2")
        self.assertEqual(self.ts.tournament_champion(self.session, self.tour.id).id,
                         final.team2_id)


class IplPlayoffTests(RoundCase):
    KNOCKOUT = "ipl_playoffs"

    def test_ipl_playoffs_seed_too(self):
        for rnd in (1, 2, 3):
            self.finish_round(rnd)
        self.session.refresh(self.tour)
        self.assertTrue(self.tour.knockout_generated)


class NoPlayoffTests(RoundCase):
    KNOCKOUT = None

    def test_nothing_is_seeded_without_a_knockout_type(self):
        for rnd in (1, 2, 3):
            self.finish_round(rnd)
        self.session.refresh(self.tour)
        self.assertFalse(self.tour.knockout_generated)
        self.assertEqual(self.ts.maybe_auto_knockout(self.session, self.tour.id), 0)



class ReplayTests(RoundCase):
    """A fixture settled by /tsim can still be played by its two teams: the
    simulated result stays until the real one is recorded over it."""

    def name_of(self, team_id):
        return next(n for n, t in self.teams.items() if t.id == team_id)

    def play(self, fx, *, first_wins=False, reserved=True):
        """Record a bot-played match between ``fx``'s teams (team 2 bats first)."""
        t1, t2 = self.teams[self.name_of(fx.team2_id)], self.teams[self.name_of(fx.team1_id)]
        state = {
            "tournament_id": self.tour.id, "match_id": None,
            "reserved_fixture_id": fx.id if reserved else None,
            "tournament_team_by_user": {101: t1.challenge_team_id,
                                        202: t2.challenge_team_id},
            "inn1_bat_team_id": 101, "inn1_bowl_team_id": 202,
            "bat_team_id": 202, "bowl_team_id": 101,
            "inn1_runs": 180, "inn1_wickets": 5,
            "total_runs": 150 if first_wins else 181, "total_wickets": 8,
        }
        recorded = self.ts.record_tournament_match(self.session, state)
        self.session.commit()
        return recorded

    def test_a_simulated_fixture_is_flagged(self):
        fx = self.fixtures(1)[0]
        self.ts.simulate_fixture(self.session, fx.id, "1")
        self.assertTrue(fx.is_simulated)

    def test_it_can_be_found_and_reserved_without_losing_its_result(self):
        fx = self.fixtures(1)[0]
        self.ts.simulate_fixture(self.session, fx.id, "1")
        self.session.commit()
        a, b = fx.team1_id, fx.team2_id
        self.assertEqual(self.lss.find_open_fixture(self.session, self.tour.id, a, b).id, fx.id)
        self.assertIn(b, self.lss.remaining_opponents(self.session, self.tour.id, a))
        reserved = self.lss.reserve_fixture(self.session, self.tour.id, a, b)
        self.assertEqual(reserved.id, fx.id)
        self.session.refresh(fx)
        self.assertEqual(fx.status, "completed")       # still on the table
        self.assertIn("(simulated)", fx.result_text)

    def test_the_played_result_replaces_the_simulated_one(self):
        fx = self.fixtures(1)[0]
        self.ts.simulate_fixture(self.session, fx.id, "1")   # team 1 "won"
        self.session.commit()
        recorded = self.play(fx, first_wins=True)             # team 2 really won
        self.assertEqual(recorded.id, fx.id)
        self.session.refresh(fx)
        self.assertFalse(fx.is_simulated)
        self.assertNotIn("(simulated)", fx.result_text or "")
        self.assertEqual((fx.inn1_runs, fx.inn2_runs), (180, 150))
        winner = self.teams[self.name_of(fx.winner_team_id)]
        loser_id = fx.team1_id if fx.winner_team_id == fx.team2_id else fx.team2_id
        loser = self.teams[self.name_of(loser_id)]
        self.session.refresh(winner)
        self.session.refresh(loser)
        # Counted once — the simulated result is gone, not added to.
        self.assertEqual((winner.played, winner.won), (1, 1))
        self.assertEqual((loser.played, loser.won), (1, 0))
        # Played for real now: no second replay.
        self.assertIsNone(self.lss.replayable_fixture(
            self.session, self.tour.id, fx.team1_id, fx.team2_id))

    def test_a_pair_search_finds_it_without_a_reservation(self):
        fx = self.fixtures(1)[0]
        self.ts.simulate_fixture(self.session, fx.id, "1")
        self.session.commit()
        self.assertEqual(self.play(fx, reserved=False).id, fx.id)

    def test_an_abandoned_replay_keeps_the_simulated_result(self):
        fx = self.fixtures(1)[0]
        self.ts.simulate_fixture(self.session, fx.id, "1")
        self.session.commit()
        self.lss.reserve_fixture(self.session, self.tour.id, fx.team1_id, fx.team2_id)
        self.lss.release_fixture(self.session, fx.id)
        self.lss.heal_live_fixtures(self.session, self.tour.id)
        self.session.commit()
        self.session.refresh(fx)
        self.assertEqual(fx.status, "completed")
        self.assertTrue(fx.is_simulated)

    def test_a_real_result_is_never_replayable(self):
        fx = self.fixtures(1)[0]
        self.play(fx, reserved=False)
        self.session.refresh(fx)
        self.assertFalse(fx.is_simulated)
        self.assertIsNone(self.lss.find_open_fixture(
            self.session, self.tour.id, fx.team1_id, fx.team2_id))

    def test_league_replays_stop_once_the_playoffs_are_drawn(self):
        for rnd in (1, 2, 3):
            self.finish_round(rnd)
        self.session.refresh(self.tour)
        self.assertTrue(self.tour.knockout_generated)
        fx = self.fixtures(1)[0]
        self.assertIsNone(self.lss.replayable_fixture(
            self.session, self.tour.id, fx.team1_id, fx.team2_id))

    def test_a_knockout_replay_stops_once_the_next_round_is_played(self):
        for rnd in (1, 2, 3):
            self.finish_round(rnd)
        semis = self.fixtures(stage="semifinal")
        for semi in semis:
            self.ts.simulate_fixture(self.session, semi.id, "1")
        self.session.commit()
        semi = semis[0]
        self.assertIsNotNone(self.lss.replayable_fixture(
            self.session, self.tour.id, semi.team1_id, semi.team2_id))
        final = self.fixtures(stage="final")[0]
        self.ts.simulate_fixture(self.session, final.id, "1")
        self.session.commit()
        self.assertIsNone(self.lss.replayable_fixture(
            self.session, self.tour.id, semi.team1_id, semi.team2_id))

    def test_a_knockout_replay_moves_the_real_winner_on(self):
        for rnd in (1, 2, 3):
            self.finish_round(rnd)
        semi = self.fixtures(stage="semifinal")[0]
        self.ts.simulate_fixture(self.session, semi.id, "1")
        self.session.commit()
        final = self.fixtures(stage="final")[0]
        self.assertIn(semi.team1_id, (final.team1_id, final.team2_id))
        sim_winner, real_winner = semi.team1_id, semi.team2_id
        self.play(semi, first_wins=True)      # team 2 bats first and wins
        self.session.refresh(final)
        self.session.refresh(semi)
        self.assertEqual(semi.winner_team_id, real_winner)
        self.assertIn(real_winner, (final.team1_id, final.team2_id))
        self.assertNotIn(sim_winner, (final.team1_id, final.team2_id))

if __name__ == "__main__":
    unittest.main()

"""Most Valuable Player — one impact-point total for a whole tournament.

Every other board ranks one column, so the all-rounder who wins three matches
with 40-odd and two wickets each time is invisible on all of them. This is the
board that sees him, and these tests pin the three things that make it mean
something rather than just being another sort:

  • **the scoring is per innings.** Three fifties are three milestone bonuses;
    one 150 is one hundred. Scoring off the aggregated player row would merge
    them and quietly reward the wrong thing.
  • **winning counts, and so does being the best player on the field.** A
    tournament MVP is the sum of every match's Player of the Match race — the
    player who keeps coming second in it still climbs.
  • **it is rebuilt from the scorecards.** Nothing is stored, so deleting or
    correcting a match moves the MVP table exactly as it moves the points table.
"""

import itertools
import json
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
                 "services.tournament_service", "services.tournament_mvp")


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


# ══════════════════════════════════════════════════════════════════════
# The scoring itself — no database in sight
# ══════════════════════════════════════════════════════════════════════

class ImpactScoringTests(unittest.TestCase):
    """The rubric the card prints, checked against the numbers it produces."""

    def setUp(self):
        from services import tournament_mvp
        self.mvp = tournament_mvp

    def test_a_player_who_never_faced_a_ball_scores_nothing(self):
        """A DNB is not a performance — letting it score puts the whole tail on
        the board."""
        self.assertEqual(self.mvp.bat_impact(0, 0, 0, 0), 0.0)
        self.assertEqual(self.mvp.bowl_impact(0, 0, 0), 0.0)

    def test_boundaries_are_worth_more_than_the_runs_alone(self):
        pushed = self.mvp.bat_impact(runs=20, balls=20, fours=0, sixes=0)
        struck = self.mvp.bat_impact(runs=20, balls=20, fours=2, sixes=2)
        self.assertGreater(struck, pushed)
        # 20 runs + 2 fours × 1 + 2 sixes × 2 = 26, no SR or milestone bonus.
        self.assertEqual(struck, 26.0)

    def test_a_hundred_is_a_hundred_not_also_a_fifty(self):
        """The scorecard convention: a century is never double-counted."""
        ton = self.mvp.bat_impact(runs=100, balls=100, fours=0, sixes=0)
        self.assertEqual(ton, 100 + self.mvp.HUNDRED_BONUS)

    def test_the_same_runs_score_more_when_they_come_quicker(self):
        slow = self.mvp.bat_impact(runs=60, balls=60, fours=0, sixes=0)
        quick = self.mvp.bat_impact(runs=60, balls=30, fours=0, sixes=0)
        self.assertGreater(quick, slow)

    def test_wickets_are_the_bulk_of_a_bowling_score(self):
        spell = self.mvp.bowl_impact(wickets=3, runs=24, balls=24)
        # 3 × 25 = 75, economy 6.00 beats the 8.00 baseline by 2 over 4 overs
        # (+16), plus the 3-for bonus.
        self.assertEqual(spell, 75 + 16 + self.mvp.THREE_FOR_BONUS)

    def test_going_for_plenty_costs_points_but_never_below_zero(self):
        """One bad day must not drag a player's whole tournament backwards."""
        mauled = self.mvp.bowl_impact(wickets=0, runs=60, balls=24)
        self.assertEqual(mauled, 0.0)

    def test_the_printed_rubric_is_generated_from_the_constants(self):
        """A board nobody can explain gets argued with instead of chased."""
        text = " ".join(self.mvp.rubric_lines())
        self.assertIn(str(self.mvp.WICKET_POINTS), text)
        self.assertIn(str(self.mvp.POTM_BONUS), text)
        self.assertIn(str(self.mvp.WIN_BONUS), text)


# ══════════════════════════════════════════════════════════════════════
# The table, built from real matches
# ══════════════════════════════════════════════════════════════════════

class MvpTableCase(unittest.TestCase):
    """Two teams in one active tournament, with hand-written scorecards."""

    def setUp(self):
        from database import get_session
        from models import (ChallengeMode, ChallengeLeague, ChallengeTeam,
                            Tournament, TournamentTeam)
        from services import tournament_mvp

        self.session = get_session()
        self.mvp = tournament_mvp

        mode = ChallengeMode(name=f"Mode {next(_TG)}")
        self.session.add(mode)
        self.session.flush()
        league = ChallengeLeague(mode_id=mode.id, name=f"League {next(_TG)}",
                                 short_code="MVP")
        self.session.add(league)
        self.session.flush()
        self.tour = Tournament(name="Impact Cup", league_id=league.id,
                               kind="challenge", status="active",
                               format="League", league_format="single_rr",
                               overs=20, max_teams=8)
        self.session.add(self.tour)
        self.session.flush()

        self.teams = {}
        for i, name in enumerate(("Alpha", "Bravo")):
            ct = ChallengeTeam(league_id=league.id, name=name, sort_order=i)
            self.session.add(ct)
            self.session.flush()
            tt = TournamentTeam(tournament_id=self.tour.id, name=name,
                                challenge_team_id=ct.id, sort_order=i)
            self.session.add(tt)
            self.session.flush()
            self.teams[name] = tt
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    # ── scorecard helpers ──

    def line(self, name, team, *, user_id, runs=0, balls=0, fours=0, sixes=0,
             wickets=0, conceded=0, bowl_balls=0, roster_id=None):
        return {
            "name": name, "team_name": team, "user_id": user_id,
            "roster_id": roster_id if roster_id is not None else abs(hash(name)) % 10000,
            "player_id": None,
            "batted": balls > 0, "bat_runs": runs, "bat_balls": balls,
            "bat_fours": fours, "bat_sixes": sixes, "bat_out": True,
            "bowled": bowl_balls > 0, "bowl_wickets": wickets,
            "bowl_runs": conceded, "bowl_balls": bowl_balls,
        }

    def record(self, lines, winner="Alpha", host_uid=1, target_uid=2, match_no=1):
        """Store one completed match with the given scorecard lines."""
        from models import TournamentMatch
        a, b = self.teams["Alpha"], self.teams["Bravo"]
        fixture = TournamentMatch(
            tournament_id=self.tour.id, team1_id=a.id, team2_id=b.id,
            winner_team_id=self.teams[winner].id if winner else None,
            status="completed", stage="league", match_no=match_no,
            host_user_id=host_uid, target_user_id=target_uid,
            scorecard_json=json.dumps(lines))
        self.session.add(fixture)
        self.session.commit()
        return fixture

    def by_name(self, rows, name):
        return next(r for r in rows if r.name == name)


class MvpTableTests(MvpTableCase):
    def test_an_empty_tournament_has_an_empty_table(self):
        self.assertEqual(self.mvp.mvp_table(self.session, self.tour.id), [])

    def test_the_all_rounder_beats_the_bigger_scorer(self):
        """The whole point of the board: 40 and two wickets is worth more than
        60 and nothing, and no single-column board can say so."""
        self.record([
            self.line("Batter", "Alpha", user_id=1, runs=60, balls=50),
            self.line("Allrounder", "Alpha", user_id=1, runs=40, balls=25,
                      wickets=2, conceded=20, bowl_balls=24),
        ])
        rows = self.mvp.mvp_table(self.session, self.tour.id)
        self.assertEqual(rows[0].name, "Allrounder")
        self.assertGreater(rows[0].points, self.by_name(rows, "Batter").points)

    def test_winning_is_worth_points_and_losing_is_not(self):
        self.record([
            self.line("Winner", "Alpha", user_id=1, runs=30, balls=30),
            self.line("Loser", "Bravo", user_id=2, runs=30, balls=30),
        ], winner="Alpha")
        rows = self.mvp.mvp_table(self.session, self.tour.id)
        won, lost = self.by_name(rows, "Winner"), self.by_name(rows, "Loser")
        self.assertEqual(won.wins, 1)
        self.assertEqual(lost.wins, 0)
        self.assertEqual(won.result_points - lost.result_points,
                         self.mvp.WIN_BONUS)

    def test_a_line_the_match_cannot_place_by_id_falls_back_to_the_team_name(self):
        """An imported or hand-corrected scorecard can carry user ids the
        fixture never saw. A line like that is not "the loser" by default —
        the team name decides, or nothing does."""
        self.record([
            self.line("Stranger", "Alpha", user_id=99, runs=40, balls=30),
        ], winner="Alpha", host_uid=1, target_uid=2)
        row = self.mvp.mvp_table(self.session, self.tour.id)[0]
        self.assertEqual(row.wins, 1)
        self.assertEqual(row.result_points, self.mvp.WIN_BONUS)

    def test_a_tie_leaves_both_sides_with_the_consolation(self):
        self.record([
            self.line("One", "Alpha", user_id=1, runs=30, balls=30),
            self.line("Two", "Bravo", user_id=2, runs=30, balls=30),
        ], winner=None)
        rows = self.mvp.mvp_table(self.session, self.tour.id)
        for row in rows:
            self.assertEqual(row.wins, 0)
            self.assertEqual(row.result_points, self.mvp.TIE_BONUS)

    def test_the_best_player_on_the_field_takes_the_award(self):
        self.record([
            self.line("Star", "Alpha", user_id=1, runs=80, balls=45),
            self.line("Filler", "Alpha", user_id=1, runs=5, balls=9),
            self.line("Loser", "Bravo", user_id=2, runs=20, balls=25),
        ], winner="Alpha")
        rows = self.mvp.mvp_table(self.session, self.tour.id)
        self.assertEqual(self.by_name(rows, "Star").awards, 1)
        self.assertEqual(self.by_name(rows, "Filler").awards, 0)
        self.assertEqual(self.by_name(rows, "Star").award_points,
                         self.mvp.POTM_BONUS)

    def test_a_quiet_winner_does_not_beat_a_losing_match_winner(self):
        """The live match card's own rule: a losing player with a real
        performance behind them is still in the race."""
        self.record([
            self.line("Quiet", "Alpha", user_id=1, runs=8, balls=10),
            self.line("Heroic", "Bravo", user_id=2, runs=95, balls=50),
        ], winner="Alpha")
        rows = self.mvp.mvp_table(self.session, self.tour.id)
        self.assertEqual(self.by_name(rows, "Heroic").awards, 1)
        self.assertEqual(self.by_name(rows, "Quiet").awards, 0)

    def test_three_fifties_beat_one_big_score_of_the_same_runs(self):
        """Per innings, not per aggregate — three milestone bonuses, not one."""
        for i in range(3):
            self.record([
                self.line("Steady", "Alpha", user_id=1, runs=50, balls=40),
            ], winner=None, match_no=i + 1)
        self.record([
            self.line("Streaky", "Bravo", user_id=2, runs=150, balls=120),
        ], winner=None, match_no=9)
        rows = self.mvp.mvp_table(self.session, self.tour.id)
        steady = self.by_name(rows, "Steady")
        streaky = self.by_name(rows, "Streaky")
        self.assertEqual(steady.matches, 3)
        self.assertGreater(steady.bat_points, streaky.bat_points)

    def test_a_players_figures_accumulate_across_matches(self):
        for i in range(2):
            self.record([
                self.line("Regular", "Alpha", user_id=1, runs=30, balls=20,
                          roster_id=77),
            ], winner="Alpha", match_no=i + 1)
        rows = self.mvp.mvp_table(self.session, self.tour.id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].matches, 2)
        self.assertEqual(rows[0].bat_runs, 60)

    def test_the_batting_board_ranks_on_batting_alone(self):
        self.record([
            self.line("Bat", "Alpha", user_id=1, runs=70, balls=40),
            self.line("Ball", "Alpha", user_id=1, wickets=5, conceded=15,
                      bowl_balls=24),
        ], winner="Alpha")
        batting = self.mvp.mvp_table(self.session, self.tour.id, board="batting")
        bowling = self.mvp.mvp_table(self.session, self.tour.id, board="bowling")
        self.assertEqual([r.name for r in batting], ["Bat"])
        self.assertEqual([r.name for r in bowling], ["Ball"])

    def test_deleting_a_match_moves_the_table_with_it(self):
        """Nothing is stored, so a corrected result corrects the MVP table too."""
        fixture = self.record([
            self.line("Gone", "Alpha", user_id=1, runs=90, balls=40),
        ], winner="Alpha")
        self.assertTrue(self.mvp.mvp_table(self.session, self.tour.id))
        self.session.delete(fixture)
        self.session.commit()
        self.assertEqual(self.mvp.mvp_table(self.session, self.tour.id), [])

    def test_an_unfinished_fixture_contributes_nothing(self):
        from models import TournamentMatch
        self.session.add(TournamentMatch(
            tournament_id=self.tour.id, team1_id=self.teams["Alpha"].id,
            team2_id=self.teams["Bravo"].id, status="scheduled", stage="league",
            match_no=4))
        self.session.commit()
        self.assertEqual(self.mvp.mvp_table(self.session, self.tour.id), [])

    def test_a_scorecard_that_will_not_parse_does_not_take_the_board_down(self):
        from models import TournamentMatch
        self.session.add(TournamentMatch(
            tournament_id=self.tour.id, team1_id=self.teams["Alpha"].id,
            team2_id=self.teams["Bravo"].id, status="completed", stage="league",
            match_no=5, scorecard_json="{not json"))
        self.session.commit()
        self.record([self.line("Fine", "Alpha", user_id=1, runs=40, balls=30)],
                    winner="Alpha", match_no=6)
        rows = self.mvp.mvp_table(self.session, self.tour.id)
        self.assertEqual([r.name for r in rows], ["Fine"])

    def test_the_leaderboard_dict_carries_the_board(self):
        """/tournamentstats and /lptstats both read it from here."""
        from services import tournament_service
        self.record([self.line("Someone", "Alpha", user_id=1, runs=44, balls=30)],
                    winner="Alpha")
        leaders = tournament_service.stat_leaders(self.session, self.tour.id)
        self.assertIn("mvp", leaders)
        self.assertEqual(leaders["mvp"][0].name, "Someone")


if __name__ == "__main__":
    unittest.main()

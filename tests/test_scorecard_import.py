"""Recording a tournament fixture from a written scorecard.

Two halves. The grammar is pure Python and tested on its own — what a person
actually types, including the forms they type by accident. The import is tested
against a real fixture: the points table, the net run rate and the per-player
leaderboards have to come out exactly as they would from a bot-played match,
because they share a leaderboard row with one.

What these pin down:

  • the batting and bowling line forms, and the not-out shorthand
  • a bowling line is never mistaken for an innings header (both are a name
    followed by numbers), and neither are "Total" and "Extras"
  • a card that can't be read says which line and why, rather than dropping a
    batter silently
  • bowlers in an innings block belong to the *fielding* side
  • the innings that batted first ends up in the fixture's first slot, so net
    run rate is credited the right way round
  • a written "Result:" line decides the winner, so a Super Over can be recorded
  • an imported match aggregates into the same leaderboard row as a played one,
    and removing it rebuilds the tournament without it
"""

import itertools
import os
import sys
import tempfile
import unittest

from services.scorecard_import import (
    ScorecardError, parse_batting_line, parse_bowling_line, parse_scorecard,
)

_TG = itertools.count(880_001)


# ══════════════════════════════════════════════════════════════════════
# The grammar (no database)
# ══════════════════════════════════════════════════════════════════════

class BattingLineTests(unittest.TestCase):
    def test_compact_form_with_boundary_markers(self):
        line = parse_batting_line("Rohit Sharma 62 (41) 6x4 2x6 c Dhoni b Jadeja")
        self.assertEqual(line["name"], "Rohit Sharma")
        self.assertEqual((line["runs"], line["balls"]), (62, 41))
        self.assertEqual((line["fours"], line["sixes"]), (6, 2))
        self.assertTrue(line["out"])

    def test_not_out_forms(self):
        for text in ("Ishan Kishan 45 (30) not out",
                     "Ishan Kishan 45* (30)",
                     "Ishan Kishan 45 (30) 4x4 1x6 n.o."):
            self.assertFalse(parse_batting_line(text)["out"], text)

    def test_a_batter_is_out_unless_the_line_says_otherwise(self):
        # The safe default: assuming not-out would quietly inflate averages.
        self.assertTrue(parse_batting_line("Virat Kohli 30 (25)")["out"])

    def test_bare_boundary_numbers_after_the_figures(self):
        line = parse_batting_line("MS Dhoni 30 (12) 1 3 not out")
        self.assertEqual((line["fours"], line["sixes"]), (1, 3))

    def test_comma_form(self):
        line = parse_batting_line("Suryakumar Yadav, 40, 20, 3, 2, out")
        self.assertEqual(line["name"], "Suryakumar Yadav")
        self.assertEqual((line["runs"], line["balls"]), (40, 20))
        self.assertEqual((line["fours"], line["sixes"]), (3, 2))
        self.assertTrue(line["out"])

    def test_comma_form_not_out(self):
        self.assertFalse(
            parse_batting_line("Hardik Pandya, 12, 6, 1, 1, not out")["out"])

    def test_pipe_form(self):
        line = parse_batting_line("Shubman Gill | 90 | 55 | 9 | 3")
        self.assertEqual(line["name"], "Shubman Gill")
        self.assertEqual((line["runs"], line["balls"]), (90, 55))

    def test_a_line_without_figures_is_not_a_batting_line(self):
        self.assertIsNone(parse_batting_line("Batting"))
        self.assertIsNone(parse_batting_line("Mumbai Indians 187/5 (20)"))

    def test_unreadable_comma_figures_are_reported(self):
        with self.assertRaises(ScorecardError):
            parse_batting_line("Rohit Sharma, sixty two, 41, 6, 2")


class BowlingLineTests(unittest.TestCase):
    def test_dashed_figures(self):
        line = parse_bowling_line("Jasprit Bumrah 4-0-24-3")
        self.assertEqual(line["name"], "Jasprit Bumrah")
        self.assertEqual((line["overs"], line["maidens"]), ("4", 0))
        self.assertEqual((line["runs"], line["wickets"]), (24, 3))

    def test_spaced_figures_and_partial_overs(self):
        line = parse_bowling_line("Ravindra Jadeja 3.4 0 28 2")
        self.assertEqual(line["overs"], "3.4")
        self.assertEqual(line["wickets"], 2)

    def test_comma_form(self):
        line = parse_bowling_line("Deepak Chahar, 4, 1, 31, 1")
        self.assertEqual((line["maidens"], line["runs"]), (1, 31))

    def test_a_batting_line_is_not_a_bowling_line(self):
        self.assertIsNone(parse_bowling_line("Rohit Sharma 62 (41) 6x4 2x6"))


CARD = """
Match 45 — Wankhede Stadium
Innings 1: Mumbai Indians 187/5 (20)
Batting
Rohit Sharma 62* (41) 6x4 2x6
Ishan Kishan 45 (30) 4x4 1x6 c Dhoni b Jadeja
Suryakumar Yadav, 40, 20, 3, 2, out
Extras 12
Total 187/5 (20)
Bowling
Deepak Chahar 4-0-31-1
Ravindra Jadeja 4 0 28 2

[2nd Innings] Chennai Super Kings 180-8 in 20 overs
Batting
Ruturaj Gaikwad 55 (38) 5x4 1x6 b Bumrah
MS Dhoni 30 (12) 1 3 not out
Bowling
Jasprit Bumrah 4-0-24-3

Result: Mumbai Indians won by 7 runs
"""


class WholeCardTests(unittest.TestCase):
    def test_a_full_card_reads_cleanly(self):
        parsed = parse_scorecard(CARD)
        first, second = parsed["innings"]
        self.assertEqual(first["team"], "Mumbai Indians")
        self.assertEqual((first["runs"], first["wickets"], first["overs"]),
                         (187, 5, "20"))
        self.assertEqual(second["team"], "Chennai Super Kings")
        self.assertEqual((second["runs"], second["wickets"]), (180, 8))
        self.assertEqual(parsed["result_text"], "Mumbai Indians won by 7 runs")

    def test_bowling_and_total_lines_do_not_start_new_innings(self):
        # "Deepak Chahar 4-0-31-1" and "Total 187/5 (20)" both end in numbers
        # that a scoreline regex will happily eat.
        self.assertEqual(len(parse_scorecard(CARD)["innings"]), 2)

    def test_players_land_in_the_right_lists(self):
        first, second = parse_scorecard(CARD)["innings"]
        self.assertEqual([b["name"] for b in first["batting"]],
                         ["Rohit Sharma", "Ishan Kishan", "Suryakumar Yadav"])
        self.assertEqual([b["name"] for b in first["bowling"]],
                         ["Deepak Chahar", "Ravindra Jadeja"])
        self.assertEqual([b["name"] for b in second["bowling"]],
                         ["Jasprit Bumrah"])

    def test_a_label_on_its_own_line_is_picked_up(self):
        parsed = parse_scorecard(
            "Innings 1\nAlpha 150/7 (20)\nBatting\nA1 50 (40)\n"
            "Innings 2\nBravo 151/4 (19.2)\nBatting\nB1 60 (45)")
        self.assertEqual([i["team"] for i in parsed["innings"]], ["Alpha", "Bravo"])

    def test_numbered_innings_are_put_back_in_order(self):
        parsed = parse_scorecard(
            "Innings 2: Bravo 151/4 (19.2)\nInnings 1: Alpha 150/7 (20)")
        self.assertEqual([i["team"] for i in parsed["innings"]], ["Alpha", "Bravo"])

    def test_a_bare_total_means_all_out(self):
        parsed = parse_scorecard(
            "Innings 1: Alpha 150 (20)\nInnings 2: Bravo 151/4 (19.2)")
        self.assertEqual(parsed["innings"][0]["wickets"], 10)

    def test_an_empty_card_is_refused(self):
        with self.assertRaises(ScorecardError):
            parse_scorecard("   \n\n")

    def test_one_innings_is_not_a_match(self):
        with self.assertRaises(ScorecardError) as caught:
            parse_scorecard("Innings 1: Alpha 150/7 (20)\nBatting\nA1 50 (40)")
        self.assertIn("2 innings", str(caught.exception))

    def test_two_innings_by_the_same_side_is_refused(self):
        with self.assertRaises(ScorecardError):
            parse_scorecard("Innings 1: Alpha 150/7 (20)\n"
                            "Innings 2: Alpha 151/4 (19.2)")

    def test_a_junk_line_names_itself(self):
        with self.assertRaises(ScorecardError) as caught:
            parse_scorecard("Innings 1: Alpha 150/7 (20)\nBatting\n"
                            "who even knows\nInnings 2: Bravo 151/4 (19.2)")
        self.assertIn("who even knows", str(caught.exception))


# ══════════════════════════════════════════════════════════════════════
# Recording it against a real fixture
# ══════════════════════════════════════════════════════════════════════

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config",
                 "services.tournament_service", "services.scorecard_import",
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


SQUAD_A = ["Rohit Sharma", "Ishan Kishan", "Suryakumar Yadav", "Jasprit Bumrah"]
SQUAD_B = ["Ruturaj Gaikwad", "MS Dhoni", "Deepak Chahar", "Ravindra Jadeja"]


class ImportCase(unittest.TestCase):
    """Two owned Challenge League teams with one scheduled fixture between them."""

    def setUp(self):
        from database import get_session
        from models import (ChallengeMode, ChallengeLeague, ChallengeTeam,
                            ChallengePlayer, Tournament, TournamentTeam, User)
        from services import (tournament_service, scorecard_import,
                              league_schedule_service)

        self.session = get_session()
        self.ts = tournament_service
        self.si = scorecard_import
        self.lss = league_schedule_service

        mode = ChallengeMode(name=f"Mode {next(_TG)}")
        self.session.add(mode)
        self.session.flush()
        self.league = ChallengeLeague(mode_id=mode.id, name=f"League {next(_TG)}",
                                      short_code="IMP")
        self.session.add(self.league)
        self.session.flush()
        self.tour = Tournament(
            name="Import Cup", league_id=self.league.id,
            league_name=self.league.name, kind="challenge", status="active",
            league_format="single_rr", overs=20, max_teams=4)
        self.session.add(self.tour)
        self.session.flush()

        self.tteams, self.users = {}, {}
        for i, (name, squad) in enumerate((("Mumbai Indians", SQUAD_A),
                                           ("Chennai Super Kings", SQUAD_B))):
            user = User(telegram_id=next(_TG), username=f"own{i}")
            self.session.add(user)
            self.session.flush()
            ct = ChallengeTeam(league_id=self.league.id, name=name, sort_order=i)
            self.session.add(ct)
            self.session.flush()
            for k, player in enumerate(squad):
                self.session.add(ChallengePlayer(team_id=ct.id, name=player,
                                                 sort_order=k))
            tt = TournamentTeam(tournament_id=self.tour.id,
                                challenge_team_id=ct.id, name=name,
                                short_name=("MI" if i == 0 else "CSK"),
                                owner_tg_id=user.telegram_id, sort_order=i)
            self.session.add(tt)
            self.session.flush()
            self.tteams[name] = tt
            self.users[name] = user
        self.session.commit()
        self.lss.generate_schedule(self.session, self.tour.id)
        self.session.commit()
        self.fixture = self._fixture()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def _fixture(self):
        from models import TournamentMatch
        return (self.session.query(TournamentMatch)
                .filter_by(tournament_id=self.tour.id)
                .order_by(TournamentMatch.match_no, TournamentMatch.id).first())

    def _import(self, card=CARD, fixture=None):
        plan = self.si.plan_import(self.session, fixture or self.fixture,
                                   parse_scorecard(card))
        self.si.record_import(self.session, plan)
        self.session.commit()
        return plan

    def _stat(self, name):
        from models import TournamentPlayerStats
        return (self.session.query(TournamentPlayerStats)
                .filter_by(tournament_id=self.tour.id, name=name).one_or_none())


class ImportRecordingTests(ImportCase):
    def test_the_scoreline_and_the_winner_land_on_the_fixture(self):
        self._import()
        fx = self._fixture()
        self.assertEqual(fx.status, "completed")
        self.assertEqual((fx.inn1_runs, fx.inn1_wickets, fx.inn1_balls),
                         (187, 5, 120))
        self.assertEqual((fx.inn2_runs, fx.inn2_wickets), (180, 8))
        self.assertEqual(fx.winner_team_id, self.tteams["Mumbai Indians"].id)
        self.assertEqual(fx.result_text, "Mumbai Indians won by 7 runs")

    def test_the_points_table_moves(self):
        self._import()
        mi = self.tteams["Mumbai Indians"]
        csk = self.tteams["Chennai Super Kings"]
        self.session.refresh(mi)
        self.session.refresh(csk)
        self.assertEqual((mi.played, mi.won, mi.points), (1, 1, 2))
        self.assertEqual((csk.played, csk.lost, csk.points), (1, 1, 0))
        # Net run rate is real, not zero: the overs came off the innings headers.
        self.assertEqual((mi.runs_for, mi.balls_for), (187, 120))
        self.assertEqual((mi.runs_against, mi.balls_against), (180, 120))

    def test_batting_figures_reach_the_leaderboards(self):
        self._import()
        rohit = self._stat("Rohit Sharma")
        self.assertIsNotNone(rohit)
        self.assertEqual((rohit.bat_runs, rohit.bat_balls), (62, 41))
        self.assertEqual((rohit.bat_fours, rohit.bat_sixes), (6, 2))
        self.assertEqual(rohit.bat_outs, 0)           # 62* — not out
        self.assertEqual(rohit.highest_score, 62)
        self.assertEqual(rohit.team_name, "Mumbai Indians")

    def test_bowling_figures_are_credited_to_the_fielding_side(self):
        self._import()
        # Bumrah bowled in Chennai's innings; he is a Mumbai player.
        bumrah = self._stat("Jasprit Bumrah")
        self.assertEqual((bumrah.bowl_wickets, bumrah.bowl_runs), (3, 24))
        self.assertEqual(bumrah.bowl_balls, 24)
        self.assertEqual(bumrah.team_name, "Mumbai Indians")
        self.assertEqual((bumrah.best_bowl_wickets, bumrah.best_bowl_runs), (3, 24))
        # Chahar bowled in Mumbai's innings; he is a Chennai player.
        self.assertEqual(self._stat("Deepak Chahar").team_name,
                         "Chennai Super Kings")

    def test_players_are_matched_to_the_squad_so_stats_merge(self):
        from models import ChallengePlayer
        self._import()
        rohit_roster = (self.session.query(ChallengePlayer)
                        .filter_by(name="Rohit Sharma",
                                   team_id=self.tteams["Mumbai Indians"]
                                   .challenge_team_id).one())
        self.assertEqual(self._stat("Rohit Sharma").roster_id, rohit_roster.id)
        self.assertEqual(self._stat("Rohit Sharma").user_id,
                         self.users["Mumbai Indians"].id)

    def test_removing_the_match_rebuilds_the_tournament_without_it(self):
        self._import()
        self.ts.delete_tournament_match(self.session, self._fixture().id)
        self.session.commit()
        mi = self.tteams["Mumbai Indians"]
        self.session.refresh(mi)
        self.assertEqual((mi.played, mi.points), (0, 0))
        self.assertIsNone(self._stat("Rohit Sharma"))
        self.assertEqual(self._fixture().status, "scheduled")

    def test_an_already_played_fixture_is_refused(self):
        self._import()
        with self.assertRaises(ScorecardError) as caught:
            self._import()
        self.assertIn("already completed", str(caught.exception))

    def test_a_live_fixture_is_refused(self):
        fx = self._fixture()
        fx.status = "live"
        self.session.commit()
        with self.assertRaises(ScorecardError):
            self._import()


class ImportOrientationTests(ImportCase):
    def test_the_side_that_batted_first_takes_the_first_slot(self):
        # Write the card with Chennai batting first — the opposite way round to
        # the generated fixture, whose team1 is Mumbai.
        card = CARD.replace("Innings 1: Mumbai Indians 187/5 (20)",
                            "Innings 1: Chennai Super Kings 187/5 (20)")
        card = card.replace("[2nd Innings] Chennai Super Kings 180-8 in 20 overs",
                            "[2nd Innings] Mumbai Indians 180-8 in 20 overs")
        card = card.replace("Result: Mumbai Indians won by 7 runs",
                            "Result: Chennai Super Kings won by 7 runs")
        original_first = self._fixture().team1_id
        self.assertEqual(original_first, self.tteams["Mumbai Indians"].id)

        plan = self._import(card)
        self.assertTrue(plan["swap_sides"])
        fx = self._fixture()
        self.assertEqual(fx.team1_id, self.tteams["Chennai Super Kings"].id)
        # …and the net run rate follows the swap rather than the old slots.
        csk = self.tteams["Chennai Super Kings"]
        self.session.refresh(csk)
        self.assertEqual((csk.runs_for, csk.runs_against), (187, 180))


class ImportResultLineTests(ImportCase):
    def _level_card(self, result_line):
        return ("Innings 1: Mumbai Indians 150/7 (20)\nBatting\n"
                "Rohit Sharma 50 (40)\n"
                "Innings 2: Chennai Super Kings 150/6 (20)\nBatting\n"
                "MS Dhoni 60 (45)\n" + result_line)

    def test_level_scores_with_no_result_line_are_a_tie(self):
        self._import(self._level_card(""))
        fx = self._fixture()
        self.assertIsNone(fx.winner_team_id)
        self.assertEqual(fx.result_text, "Match Tied")
        mi = self.tteams["Mumbai Indians"]
        self.session.refresh(mi)
        self.assertEqual((mi.tied, mi.points), (1, 1))

    def test_a_result_line_can_record_a_super_over_winner(self):
        self._import(self._level_card(
            "Result: Chennai Super Kings won the Super Over"))
        fx = self._fixture()
        self.assertEqual(fx.winner_team_id, self.tteams["Chennai Super Kings"].id)
        csk = self.tteams["Chennai Super Kings"]
        self.session.refresh(csk)
        self.assertEqual((csk.won, csk.points), (1, 2))

    def test_a_result_line_that_contradicts_the_scores_is_flagged(self):
        plan = self.si.plan_import(
            self.session, self.fixture,
            parse_scorecard(CARD.replace(
                "Result: Mumbai Indians won by 7 runs",
                "Result: Chennai Super Kings won by 7 runs")))
        self.assertEqual(plan["winner_team_id"],
                         self.tteams["Chennai Super Kings"].id)
        self.assertTrue(any("scores say" in w for w in plan["warnings"]))


class ImportRefusalTests(ImportCase):
    def test_a_team_not_in_the_fixture_is_refused(self):
        card = CARD.replace("Chennai Super Kings", "Rajasthan Royals")
        with self.assertRaises(ScorecardError) as caught:
            self._import(card)
        self.assertIn("Rajasthan Royals", str(caught.exception))

    def test_more_overs_than_the_format_allows_is_refused(self):
        card = CARD.replace("Innings 1: Mumbai Indians 187/5 (20)",
                            "Innings 1: Mumbai Indians 187/5 (50)")
        with self.assertRaises(ScorecardError) as caught:
            self._import(card)
        self.assertIn("20-over", str(caught.exception))

    def test_a_short_name_still_finds_the_team(self):
        card = CARD.replace("Innings 1: Mumbai Indians 187/5 (20)",
                            "Innings 1: MI 187/5 (20)")
        card = card.replace("[2nd Innings] Chennai Super Kings 180-8 in 20 overs",
                            "[2nd Innings] CSK 180-8 in 20 overs")
        self._import(card)
        self.assertEqual(self._fixture().status, "completed")

    def test_a_player_outside_the_squad_is_warned_about_but_still_counted(self):
        card = CARD.replace("Suryakumar Yadav, 40, 20, 3, 2, out",
                            "Mystery Guest, 40, 20, 3, 2, out")
        plan = self.si.plan_import(self.session, self.fixture,
                                   parse_scorecard(card))
        self.assertTrue(any("Mystery Guest" in w for w in plan["warnings"]))
        self.si.record_import(self.session, plan)
        self.session.commit()
        self.assertEqual(self._stat("Mystery Guest").bat_runs, 40)

    def test_missing_overs_warn_because_net_run_rate_depends_on_them(self):
        card = ("Innings 1: Mumbai Indians 150/7\nBatting\nRohit Sharma 50 (40)\n"
                "Innings 2: Chennai Super Kings 151/4\nBatting\nMS Dhoni 60 (45)")
        plan = self.si.plan_import(self.session, self.fixture,
                                   parse_scorecard(card))
        self.assertEqual(len(plan["warnings"]), 2)
        self.assertTrue(all("net run rate" in w for w in plan["warnings"]))


if __name__ == "__main__":
    unittest.main()

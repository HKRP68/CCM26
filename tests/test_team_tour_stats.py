"""``/teamtourstats`` — one team's tournament by the numbers.

/tournamentstats ranks individual players across the whole competition and
/clsd lists a team's fixtures. Neither answers what a team owner opens the bot
to ask: *how is my team doing?* This card does, and these tests pin the parts of
it that are easy to get subtly, invisibly wrong:

  • **which innings is ours.** Innings 1 belongs to ``team1`` and innings 2 to
    ``team2`` — the same mapping the net run-rate is built from. Read it the
    wrong way round and every number on the card is the opponent's.
  • **economy is bowled off the balls we bowled.** The overs a team bats and the
    overs it bowls are different numbers, and using its own batting balls for
    the runs it conceded quietly reports the wrong rate.
  • **a match with no score is not a zero.** A walkover recorded without runs
    must be skipped, not averaged in as an innings of 0.
"""

import itertools
import json
import os
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
                 "services.tournament_service", "services.cl_tournament_view")


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


class TeamStatsCase(unittest.TestCase):
    """Two teams in one live tournament, with hand-written results."""

    def setUp(self):
        from database import get_session
        from models import (ChallengeMode, ChallengeLeague, ChallengeTeam,
                            Tournament, TournamentTeam)
        from services import cl_tournament_view as ctv

        self.session = get_session()
        self.ctv = ctv

        mode = ChallengeMode(name=f"Mode {next(_TG)}")
        self.session.add(mode)
        self.session.flush()
        league = ChallengeLeague(mode_id=mode.id, name=f"League {next(_TG)}",
                                 short_code="TTS")
        self.session.add(league)
        self.session.flush()
        self.tour = Tournament(name="Stats Cup", league_id=league.id,
                               kind="challenge", status="active",
                               format="League", league_format="single_rr",
                               overs=20, max_teams=8,
                               points_win=2, points_tie=1, points_loss=0)
        self.session.add(self.tour)
        self.session.flush()

        self.owner_tg = next(_TG)
        self.teams = {}
        for i, name in enumerate(("Alpha", "Bravo")):
            ct = ChallengeTeam(league_id=league.id, name=name, sort_order=i)
            self.session.add(ct)
            self.session.flush()
            tt = TournamentTeam(tournament_id=self.tour.id, name=name,
                                challenge_team_id=ct.id, sort_order=i,
                                owner_tg_id=self.owner_tg if name == "Alpha" else None,
                                owner_name="Skipper" if name == "Alpha" else None)
            self.session.add(tt)
            self.session.flush()
            self.teams[name] = tt
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    # ── helpers ───────────────────────────────────────────────────────

    def record(self, *, team1, team2, inn1, inn2, winner, match_no=1,
               scorecard=None):
        """One completed fixture. ``inn1``/``inn2`` are ``(runs, wkts, balls)``."""
        from models import TournamentMatch
        m = TournamentMatch(
            tournament_id=self.tour.id,
            team1_id=self.teams[team1].id, team2_id=self.teams[team2].id,
            winner_team_id=self.teams[winner].id if winner else None,
            status="completed", stage="league", match_no=match_no,
            inn1_runs=inn1[0], inn1_wickets=inn1[1], inn1_balls=inn1[2],
            inn2_runs=inn2[0], inn2_wickets=inn2[1], inn2_balls=inn2[2],
            scorecard_json=json.dumps(scorecard) if scorecard else None,
        )
        self.session.add(m)
        self.session.commit()
        return m

    def line(self, name, team, *, user_id=1, runs=0, balls=0, wickets=0,
             conceded=0, bowl_balls=0):
        return {
            "name": name, "team_name": team, "user_id": user_id,
            "roster_id": abs(hash(name)) % 10000, "player_id": None,
            "batted": balls > 0, "bat_runs": runs, "bat_balls": balls,
            "bat_fours": 0, "bat_sixes": 0, "bat_out": True,
            "bowled": bowl_balls > 0, "bowl_wickets": wickets,
            "bowl_runs": conceded, "bowl_balls": bowl_balls,
        }

    def render(self, team="Alpha", viewer=None):
        return self.ctv.render_team_stats(
            self.session, self.tour, self.teams[team], viewer_tg_id=viewer)


class InningsSideTests(TeamStatsCase):
    """Whose innings is whose — the mistake that makes every number wrong."""

    def test_a_team_batting_first_owns_innings_one(self):
        self.record(team1="Alpha", team2="Bravo",
                    inn1=(190, 4, 120), inn2=(150, 10, 108), winner="Alpha")
        mine = self.ctv.team_innings(self.session, self.tour.id,
                                     self.teams["Alpha"].id)
        self.assertEqual([(e["runs"], e["conceded"]) for e in mine],
                         [(190, 150)])

    def test_a_team_batting_second_owns_innings_two(self):
        self.record(team1="Alpha", team2="Bravo",
                    inn1=(190, 4, 120), inn2=(150, 10, 108), winner="Alpha")
        theirs = self.ctv.team_innings(self.session, self.tour.id,
                                       self.teams["Bravo"].id)
        self.assertEqual([(e["runs"], e["conceded"]) for e in theirs],
                         [(150, 190)])

    def test_the_balls_bowled_are_the_other_innings(self):
        """Economy off our own batting overs is a different, wrong number."""
        self.record(team1="Alpha", team2="Bravo",
                    inn1=(190, 4, 120), inn2=(150, 10, 108), winner="Alpha")
        entry = self.ctv.team_innings(self.session, self.tour.id,
                                      self.teams["Alpha"].id)[0]
        self.assertEqual(entry["balls"], 120)
        self.assertEqual(entry["conceded_balls"], 108)

    def test_a_result_with_no_score_is_skipped_not_counted_as_nought(self):
        """A walkover entered without runs is not an innings of 0."""
        self.record(team1="Alpha", team2="Bravo",
                    inn1=(None, None, None), inn2=(None, None, None),
                    winner="Alpha", match_no=1)
        self.assertEqual(
            self.ctv.team_innings(self.session, self.tour.id,
                                  self.teams["Alpha"].id), [])


class CardTests(TeamStatsCase):
    """What the card actually says."""

    def two_matches(self):
        self.record(team1="Alpha", team2="Bravo", match_no=1,
                    inn1=(190, 4, 120), inn2=(150, 10, 108), winner="Alpha")
        self.record(team1="Bravo", team2="Alpha", match_no=2,
                    inn1=(170, 6, 120), inn2=(120, 10, 96), winner="Bravo")

    def test_it_leads_with_where_the_team_stands(self):
        from services import tournament_service
        self.two_matches()
        tournament_service.recompute_standings(self.session, self.tour.id)
        self.session.commit()
        card = self.render("Alpha")
        self.assertIn("Alpha", card)
        self.assertIn("Stats Cup", card)
        self.assertIn("Played 2", card)
        self.assertIn("W 1", card)
        self.assertIn("L 1", card)
        self.assertIn("Net run rate", card)

    def test_the_owner_is_told_it_is_their_team(self):
        self.two_matches()
        self.assertIn("your team", self.render("Alpha", viewer=self.owner_tg))
        self.assertNotIn("your team", self.render("Alpha", viewer=self.owner_tg + 1))

    def test_the_batting_block_reports_the_real_totals(self):
        self.two_matches()
        card = self.render("Alpha")
        # 190 + 120 = 310 runs, from 120 + 96 = 216 balls (36 overs).
        self.assertIn("310", card)
        self.assertIn("36 overs", card)
        self.assertIn("Highest: <b>190/4</b> vs Bravo", card)
        self.assertIn("Lowest: <b>120/10</b> vs Bravo", card)

    def test_the_bowling_block_reports_what_was_conceded(self):
        self.two_matches()
        card = self.render("Alpha")
        # 150 + 170 = 320 conceded, and the tightest day was the 150.
        self.assertIn("320", card)
        self.assertIn("Best defence: <b>150/10</b> vs Bravo", card)

    def test_one_innings_does_not_print_a_lowest_that_is_the_highest(self):
        self.record(team1="Alpha", team2="Bravo",
                    inn1=(190, 4, 120), inn2=(150, 10, 108), winner="Alpha")
        card = self.render("Alpha")
        self.assertIn("Highest: <b>190/4</b>", card)
        self.assertNotIn("Lowest:", card)

    def test_a_team_with_no_completed_match_says_so_instead_of_dividing_by_zero(self):
        card = self.render("Alpha")
        self.assertIn("No completed match", card)
        self.assertNotIn("With the bat", card)

    def test_the_top_performers_are_this_teams_players_only(self):
        from services import tournament_service
        self.record(
            team1="Alpha", team2="Bravo", match_no=1,
            inn1=(190, 4, 120), inn2=(150, 10, 108), winner="Alpha",
            scorecard=[
                self.line("Ours Opener", "Alpha", user_id=1, runs=95, balls=52),
                self.line("Ours Seamer", "Alpha", user_id=1, wickets=4,
                          conceded=21, bowl_balls=24),
                self.line("Theirs Star", "Bravo", user_id=2, runs=140, balls=60),
                self.line("Theirs Spinner", "Bravo", user_id=2, wickets=6,
                          conceded=18, bowl_balls=24),
            ])
        tournament_service.recompute_player_stats(self.session, self.tour.id)
        self.session.commit()
        card = self.render("Alpha")
        self.assertIn("Ours Opener", card)
        self.assertIn("Ours Seamer", card)
        self.assertNotIn("Theirs Star", card)
        self.assertNotIn("Theirs Spinner", card)

    def test_a_manual_points_penalty_is_shown_rather_than_left_unexplained(self):
        from services import tournament_service
        self.two_matches()
        tournament_service.adjust_points(self.session, self.teams["Alpha"].id,
                                         delta=-2, note="Slow over rate")
        tournament_service.recompute_standings(self.session, self.tour.id)
        self.session.commit()
        card = self.render("Alpha")
        self.assertIn("-2", card)
        self.assertIn("Slow over rate", card)


class LetsPlayTeamTests(TeamStatsCase):
    """A Lets Play team *is* a person, and may have renamed itself since."""

    def test_a_renamed_team_still_finds_its_own_players(self):
        from models import TournamentTeam, User
        from services import tournament_service

        user = User(telegram_id=next(_TG), username="skip", first_name="Skip")
        self.session.add(user)
        self.session.flush()
        solo = TournamentTeam(tournament_id=self.tour.id, name="New Name",
                              user_tg_id=user.telegram_id, sort_order=5)
        self.session.add(solo)
        self.session.commit()

        # The scorecard was written under the team's OLD name.
        self.record(team1="Alpha", team2="Bravo", match_no=1,
                    inn1=(150, 8, 120), inn2=(120, 10, 120), winner="Alpha",
                    scorecard=[self.line("Their Opener", "Old Name",
                                         user_id=user.id, runs=70, balls=40)])
        tournament_service.recompute_player_stats(self.session, self.tour.id)
        self.session.commit()

        rows = self.ctv.team_player_stats(self.session, self.tour, solo)
        self.assertEqual([r.name for r in rows], ["Their Opener"])


class CommandWiringTests(unittest.TestCase):
    """The card is only reachable if the command is registered for it.

    Source-level: importing ``bot`` starts the whole application, which is far
    more than an assertion about which handlers exist needs.
    """

    @staticmethod
    def _read(*parts):
        path = os.path.join(os.path.dirname(__file__), "..", *parts)
        with open(path) as fh:
            return fh.read()

    def test_the_command_and_its_picker_are_registered(self):
        bot = self._read("bot.py")
        self.assertIn('"teamtourstats"', bot)
        self.assertIn("teamtourstats_handler))", bot)
        self.assertIn(r'pattern=r"^ctts_"', bot)

    def test_the_picker_does_not_collide_with_the_schedule_one(self):
        """``ctsd_`` is /clsd's; a shared prefix would cross the two cards.

        Read from source rather than imported: ``handlers.cl_tournament`` binds
        whichever ``database``/``models`` modules are live when it first loads,
        and this file runs against a throwaway one.
        """
        import re
        handler = self._read("handlers", "cl_tournament.py")
        prefixes = dict(re.findall(r'^(TS_PREFIX|SD_PREFIX) = "([^"]+)"',
                                   handler, re.M))
        self.assertEqual(set(prefixes), {"TS_PREFIX", "SD_PREFIX"})
        ts, sd = prefixes["TS_PREFIX"], prefixes["SD_PREFIX"]
        self.assertFalse(ts.startswith(sd))
        self.assertFalse(sd.startswith(ts))

    def test_it_is_advertised_in_help(self):
        self.assertIn("/teamtourstats", self._read("bot.py"))


if __name__ == "__main__":
    unittest.main()

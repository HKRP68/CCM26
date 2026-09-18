"""``/clsd <TEAM NAME>`` — one team's tournament on one card.

/ctfixtures answers "what is the schedule?". This answers "what do WE play
next, and how are we doing?", which is the question a team owner actually
opens the bot for — and the one the full fixture list buries under forty lines
belonging to everyone else.

Two halves are pinned here:

  • **finding the team.** A name typed by hand, on a phone, in a chat: case,
    the short form everybody says out loud ("MI"), and half-typed names all
    have to land. When a query genuinely could mean two teams the lookup must
    say so rather than guess — guessing shows somebody the wrong schedule and
    they act on it.
  • **the card.** Every fixture written from THIS team's side: won or lost, not
    "team1 beat team2"; home or away, not "🏠 is on the left"; and the next
    matches separated from the results, because those are two different things
    to do with the card.
"""

import itertools
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap  # noqa: E402  (sibling helper; see its docstring)

_TG = itertools.count(980_001)

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


class ScheduleCase(unittest.TestCase):
    """Four franchises in one active Challenge League tournament."""

    TEAM_NAMES = ("Mumbai Indians", "Chennai Kings", "Chennai Chargers",
                  "Delhi Capitals")

    def setUp(self):
        from database import get_session
        from models import (ChallengeMode, ChallengeLeague, ChallengeTeam,
                            Tournament, TournamentTeam)
        from services import cl_tournament_view, tournament_service

        self.session = get_session()
        self.ctv = cl_tournament_view
        self.ts = tournament_service

        mode = ChallengeMode(name=f"Mode {next(_TG)}")
        self.session.add(mode)
        self.session.flush()
        self.league = ChallengeLeague(mode_id=mode.id, name=f"League {next(_TG)}",
                                      short_code="SCH")
        self.session.add(self.league)
        self.session.flush()
        self.tour = Tournament(name="Summer Trophy", league_id=self.league.id,
                               league_name=self.league.name, kind="challenge",
                               status="active", format="League",
                               league_format="single_rr", overs=20, max_teams=8)
        self.session.add(self.tour)
        self.session.flush()

        self.tteams = []
        for i, name in enumerate(self.TEAM_NAMES):
            ct = ChallengeTeam(league_id=self.league.id, name=name, sort_order=i)
            self.session.add(ct)
            self.session.flush()
            tt = TournamentTeam(tournament_id=self.tour.id,
                                challenge_team_id=ct.id, name=name, sort_order=i)
            self.session.add(tt)
            self.tteams.append(tt)
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def by_name(self, name):
        return next(tt for tt in self.tteams if tt.name == name)

    def add_fixture(self, team_a, team_b, **kwargs):
        from models import TournamentMatch
        fixture = TournamentMatch(
            tournament_id=self.tour.id, team1_id=team_a.id, team2_id=team_b.id,
            stage="league", **kwargs)
        self.session.add(fixture)
        self.session.commit()
        return fixture


# ══════════════════════════════════════════════════════════════════════
# Finding the team somebody typed
# ══════════════════════════════════════════════════════════════════════

class FindTeamTests(ScheduleCase):
    def test_an_exact_name_resolves(self):
        team, candidates = self.ctv.find_team(
            self.session, self.tour.id, "Mumbai Indians")
        self.assertEqual(team.name, "Mumbai Indians")
        self.assertEqual(candidates, [])

    def test_case_and_stray_spaces_do_not_matter(self):
        for typed in ("mumbai indians", "  MUMBAI   INDIANS  ", "Mumbai indians"):
            team, _ = self.ctv.find_team(self.session, self.tour.id, typed)
            self.assertIsNotNone(team, f"{typed!r} should find the team")
            self.assertEqual(team.name, "Mumbai Indians")

    def test_the_initials_everybody_says_out_loud_find_the_team(self):
        team, _ = self.ctv.find_team(self.session, self.tour.id, "MI")
        self.assertEqual(team.name, "Mumbai Indians")

    def test_a_short_name_set_by_an_admin_wins_too(self):
        self.by_name("Delhi Capitals").short_name = "DC"
        self.session.commit()
        team, _ = self.ctv.find_team(self.session, self.tour.id, "dc")
        self.assertEqual(team.name, "Delhi Capitals")

    def test_a_prefix_resolves_when_it_is_unambiguous(self):
        team, _ = self.ctv.find_team(self.session, self.tour.id, "Delhi")
        self.assertEqual(team.name, "Delhi Capitals")

    def test_an_ambiguous_query_offers_the_candidates_instead_of_guessing(self):
        team, candidates = self.ctv.find_team(
            self.session, self.tour.id, "Chennai")
        self.assertIsNone(team)
        self.assertEqual({tt.name for tt in candidates},
                         {"Chennai Kings", "Chennai Chargers"})

    def test_a_full_name_beats_being_the_prefix_of_another(self):
        """Typing a whole name must land on that team, not open a menu."""
        from models import ChallengeTeam, TournamentTeam
        ct = ChallengeTeam(league_id=self.league.id, name="Delhi Capitals XI",
                           sort_order=9)
        self.session.add(ct)
        self.session.flush()
        self.session.add(TournamentTeam(tournament_id=self.tour.id,
                                        challenge_team_id=ct.id,
                                        name="Delhi Capitals XI", sort_order=9))
        self.session.commit()
        team, candidates = self.ctv.find_team(
            self.session, self.tour.id, "Delhi Capitals")
        self.assertIsNotNone(team, f"ambiguous instead: {candidates}")
        self.assertEqual(team.name, "Delhi Capitals")

    def test_an_unknown_name_matches_nothing_at_all(self):
        team, candidates = self.ctv.find_team(
            self.session, self.tour.id, "Auckland Aces")
        self.assertIsNone(team)
        self.assertEqual(candidates, [])

    def test_an_empty_query_offers_the_whole_field(self):
        team, candidates = self.ctv.find_team(self.session, self.tour.id, "  ")
        self.assertIsNone(team)
        self.assertEqual(len(candidates), len(self.TEAM_NAMES))


# ══════════════════════════════════════════════════════════════════════
# Form
# ══════════════════════════════════════════════════════════════════════

class FormTests(ScheduleCase):
    def test_form_reads_from_this_teams_side_most_recent_first(self):
        mi, ck = self.by_name("Mumbai Indians"), self.by_name("Chennai Kings")
        dc = self.by_name("Delhi Capitals")
        self.add_fixture(mi, ck, match_no=1, status="completed",
                         winner_team_id=mi.id)
        self.add_fixture(dc, mi, match_no=2, status="completed",
                         winner_team_id=dc.id)
        self.add_fixture(mi, dc, match_no=3, status="completed")  # tied
        self.assertEqual(
            self.ctv.team_form(self.session, self.tour.id, mi.id),
            ["T", "L", "W"])

    def test_an_unplayed_fixture_is_not_form(self):
        mi, ck = self.by_name("Mumbai Indians"), self.by_name("Chennai Kings")
        self.add_fixture(mi, ck, match_no=1, status="scheduled")
        self.assertEqual(
            self.ctv.team_form(self.session, self.tour.id, mi.id), [])


# ══════════════════════════════════════════════════════════════════════
# The card
# ══════════════════════════════════════════════════════════════════════

class TeamScheduleCardTests(ScheduleCase):
    def setUp(self):
        super().setUp()
        self.mi = self.by_name("Mumbai Indians")
        self.ck = self.by_name("Chennai Kings")
        self.dc = self.by_name("Delhi Capitals")

    def render(self, team=None, viewer_tg_id=None):
        return self.ctv.render_team_schedule(
            self.session, self.tour, team or self.mi, viewer_tg_id=viewer_tg_id)

    def test_a_team_with_no_fixtures_says_so_rather_than_showing_a_blank(self):
        text = self.render()
        self.assertIn("Mumbai Indians", text)
        self.assertIn("free-play", text)

    def test_a_win_is_marked_won_whichever_side_of_the_fixture_it_was_on(self):
        self.add_fixture(self.ck, self.mi, match_no=1, status="completed",
                         winner_team_id=self.mi.id,
                         result_text="Mumbai Indians won by 12 runs")
        text = self.render()
        self.assertIn("WON", text)
        self.assertNotIn("LOST", text)
        # …and the same fixture is a loss on the other team's card.
        self.assertIn("LOST", self.render(team=self.ck))

    def test_a_tie_is_neither_a_win_nor_a_loss(self):
        self.add_fixture(self.mi, self.ck, match_no=1, status="completed",
                         result_text="Match tied")
        text = self.render()
        self.assertIn("TIED", text)
        self.assertNotIn("LOST", text)

    def test_home_and_away_are_written_from_this_teams_side(self):
        self.add_fixture(self.mi, self.ck, match_no=1, status="scheduled",
                         home_team_id=self.mi.id, pitch_type="Dusty")
        self.assertIn("🏠 vs Chennai Kings", self.render())
        self.assertIn("✈️ at Mumbai Indians", self.render(team=self.ck))

    def test_an_unplayed_fixture_carries_the_pitch_it_is_pinned_to(self):
        self.add_fixture(self.mi, self.ck, match_no=1, status="scheduled",
                         pitch_type="Dusty")
        self.assertIn("Dusty", self.render())

    def test_next_up_and_results_are_separated(self):
        self.add_fixture(self.mi, self.ck, match_no=1, status="completed",
                         winner_team_id=self.mi.id, result_text="won by 5")
        self.add_fixture(self.mi, self.dc, match_no=2, status="scheduled")
        text = self.render()
        self.assertIn("Next up", text)
        self.assertIn("Results", text)
        self.assertLess(text.index("Next up"), text.index("Results"),
                        "what is left to play comes before what is done")

    def test_a_live_fixture_is_flagged_rather_than_counted_as_played(self):
        self.add_fixture(self.mi, self.ck, match_no=1, status="live")
        text = self.render()
        self.assertIn("in progress", text)
        self.assertNotIn("Results", text)

    def test_a_finished_team_is_told_it_has_nothing_left(self):
        self.add_fixture(self.mi, self.ck, match_no=1, status="completed",
                         winner_team_id=self.mi.id, result_text="won by 5")
        self.assertIn("Nothing left to play", self.render())

    def test_other_teams_fixtures_stay_off_the_card(self):
        self.add_fixture(self.ck, self.dc, match_no=1, status="scheduled")
        self.assertIn("free-play", self.render(),
                      "a fixture between two other teams is not this team's")

    def test_the_standing_and_owner_are_on_the_card(self):
        self.mi.played, self.mi.won, self.mi.points = 3, 2, 4
        self.ts.set_team_owner(self.session, self.mi.id, 111, "Ana")
        self.ts.set_co_owners(self.session, self.mi.id, [222])
        self.session.commit()
        text = self.render()
        self.assertIn("Ana", text)
        self.assertIn("🤝 +1", text)
        self.assertIn("pts", text)

    def test_a_co_owner_is_told_it_is_their_team(self):
        self.ts.set_team_owner(self.session, self.mi.id, 111, "Ana")
        self.ts.set_co_owners(self.session, self.mi.id, [222])
        self.session.commit()
        self.assertIn("your team", self.render(viewer_tg_id=222))
        self.assertNotIn("your team", self.render(viewer_tg_id=999))

    def test_a_tbd_knockout_slot_renders_rather_than_breaking_the_card(self):
        from models import TournamentMatch
        self.session.add(TournamentMatch(
            tournament_id=self.tour.id, team1_id=self.mi.id, team2_id=None,
            stage="final", match_no=9, status="scheduled",
            slot2_label="Winner of Q1"))
        self.session.commit()
        self.assertIn("Winner of Q1", self.render())

    def test_the_home_pitch_is_shown_when_the_team_has_one(self):
        self.mi.home_pitch = "Green"
        self.session.commit()
        self.assertIn("Green", self.render())


if __name__ == "__main__":
    unittest.main()

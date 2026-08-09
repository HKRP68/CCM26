"""The post-match analysis report (services/match_analysis.py).

The report is a pure function of a finished match state, so these are ordinary
unit tests with a hand-built state — no Telegram, no DB, no simulation. What
they pin is the promise the file makes: it is self-contained (nothing to fetch
when a player opens it offline), it reports what actually happened rather than a
plausible-looking default, and a state missing one of the v3.0 traces still
produces a usable document instead of an exception.
"""

import re
import unittest

from engine import momentum as momentum_engine
from services import match_analysis as ma


def _state(**over):
    """A finished 20-over match: 186/6 defended against 171/8."""
    state = {
        "match_id": 77, "overs": 20, "ball_format": "T20",
        "pitch_type": "Dry", "stadium": "Eden Gardens",
        "innings": 2, "target": 187,
        "inn1_bat_team": "Royal Rangers", "inn1_runs": 186, "inn1_wickets": 6,
        "inn1_overs": "20.0",
        "inn1_over_runs": [6, 12, 4, 9, 15, 8, 7, 11, 5, 9,
                           6, 13, 8, 4, 10, 12, 9, 14, 11, 13],
        "inn1_fow": [[34, 1, "A Sharma", "4.2"], [70, 2, "B Iyer", "8.5"],
                     [95, 3, "C Rahul", "11.1"], [140, 4, "D Pant", "16.3"],
                     [161, 5, "E Jadeja", "18.1"], [180, 6, "F Bumrah", "19.4"]],
        "inn1_partnership_history": [
            {"wicket": 1, "batsman1": "A Sharma", "batsman2": "B Iyer",
             "runs": 34, "balls": 26, "notout": False},
            {"wicket": 2, "batsman1": "B Iyer", "batsman2": "C Rahul",
             "runs": 36, "balls": 27, "notout": False}],
        "inn1_momentum_history": [0.2, 0.5, 0.1, 0.4, 1.1, 0.9, 0.6, 1.0,
                                  0.5, 0.8, 0.4, 1.2, 0.9, 0.4, 0.9, 1.3,
                                  1.0, 1.6, 1.4, 1.8],
        "inn1_approach_log": [
            {"over": 1, "bat": "balanced", "bowl": "aggressive", "runs": 6,
             "wickets": 0, "combo": None},
            {"over": 2, "bat": "ultra", "bowl": "variation", "runs": 12,
             "wickets": 0, "combo": "The Chess Match"},
            {"over": 3, "bat": "rotate", "bowl": "defensive", "runs": 4,
             "wickets": 1, "combo": "The Grind"}],

        "bat_team_name": "Titan Kings", "total_runs": 171, "total_wickets": 8,
        "inn2_overs": "20.0",
        "over_runs": [8, 7, 11, 6, 9, 12, 5, 8, 7, 10,
                      4, 9, 8, 6, 11, 7, 9, 12, 10, 12],
        "fow": [[40, 1, "G Gill", "5.1"], [88, 2, "H Kishan", "10.4"],
                [150, 3, "I Surya", "17.2"]],
        "partnership_history": [
            {"wicket": 1, "batsman1": "G Gill", "batsman2": "H Kishan",
             "runs": 40, "balls": 31, "notout": False}],
        "momentum_history": [0.3, 0.1, 0.6, 0.2, 0.5, 0.9, 0.3, 0.6, 0.2,
                             0.7, -0.1, 0.4, 0.2, -0.2, 0.6, 0.1, 0.4, 0.8,
                             0.5, 0.2],
        "pressure_history": [0.1, 0.3, 0.2, 0.5, 0.4, 0.2, 0.6, 0.5, 0.8,
                             0.6, 1.0, 0.8, 0.9, 1.2, 0.8, 1.1, 1.3, 1.0,
                             1.4, 1.7],
        "chase_history": [{"over": i + 1, "chasing": c, "runs_needed": 187 - c}
                          for i, c in enumerate(
                              [55, 52, 58, 54, 57, 62, 58, 60, 56, 61,
                               50, 53, 51, 46, 52, 45, 44, 41, 30, 12])],
        "approach_log": [
            {"over": 1, "bat": "aggressive", "bowl": "balanced", "runs": 8,
             "wickets": 0, "combo": None},
            {"over": 2, "bat": "aggressive", "bowl": "mixed", "runs": 7,
             "wickets": 0, "combo": None}],
        "dps_trace": [{"over": 17, "effects": ["cracks open (+10% variable "
                                               "bounce) — death overs dangerous"]}],
    }
    state.update(over)
    return state


_RESULT = {"winner": "Royal Rangers", "loser": "Titan Kings",
           "margin_type": "runs", "margin": 15, "tie": False}

_SCORECARD = {
    "ok": True, "current_innings": 2, "target": 187,
    "innings": [
        {"number": 1, "bat_team": "Royal Rangers", "bowl_team": "Titan Kings",
         "runs": 186, "wickets": 6, "overs": "20.0",
         "batting": [{"name": "A Sharma", "runs": 44, "balls": 28, "fours": 5,
                      "sixes": 2, "out": True, "how_out": "c Gill b Bumrah",
                      "sr": 157.1}],
         "bowling": [{"name": "J Bumrah", "overs": "4", "runs": 28,
                      "wickets": 3, "maidens": 0, "econ": 7.0}]},
        {"number": 2, "bat_team": "Titan Kings", "bowl_team": "Royal Rangers",
         "runs": 171, "wickets": 8, "overs": "20.0",
         "batting": [{"name": "G Gill", "runs": 61, "balls": 40, "fours": 7,
                      "sixes": 1, "out": True, "how_out": "b Jadeja",
                      "sr": 152.5}],
         "bowling": [{"name": "R Jadeja", "overs": "4", "runs": 24,
                      "wickets": 2, "maidens": 0, "econ": 6.0}]},
    ]}


class SelfContainmentTests(unittest.TestCase):
    """A player opens this file from their downloads, possibly offline. Anything
    it needs from the network is a blank section."""

    def setUp(self):
        self.html = ma.build_match_analysis_html(
            _state(), _RESULT, _SCORECARD, ("A Sharma", "44(28)", "Royal Rangers"))

    def test_no_external_references(self):
        for token in ("http://", "https://", "//cdn", "src=", "@import"):
            self.assertNotIn(token, self.html,
                             f"report reaches outside itself via {token!r}")

    def test_it_is_a_complete_document(self):
        self.assertTrue(self.html.lstrip().startswith("<!doctype html>"))
        self.assertIn("</html>", self.html)
        self.assertIn("<style>", self.html)

    def test_it_declares_a_viewport_and_both_themes(self):
        self.assertIn("width=device-width", self.html)
        self.assertIn("prefers-color-scheme: dark", self.html)


class ContentTests(unittest.TestCase):
    def setUp(self):
        self.html = ma.build_match_analysis_html(
            _state(), _RESULT, _SCORECARD, ("A Sharma", "44(28)", "Royal Rangers"))

    def test_both_innings_totals_are_reported(self):
        self.assertIn("186/6", self.html)
        self.assertIn("171/8", self.html)

    def test_the_result_and_potm_are_reported(self):
        self.assertIn("Royal Rangers beat Titan Kings by 15 runs", self.html)
        self.assertIn("A Sharma", self.html)
        self.assertIn("44(28)", self.html)

    def test_every_section_is_present(self):
        for heading in ("Phase by phase", "Runs per over", "The worm",
                        "Scorecards", "Momentum", "Win probability",
                        "Turning points", "Partnerships", "approach duel",
                        "How the pitch changed"):
            self.assertIn(heading, self.html, f"missing section: {heading}")

    def test_the_phase_split_adds_up_to_the_innings(self):
        """The phase table is derived, so it is the one place the report could
        quietly disagree with the scoreboard."""
        rows = re.findall(
            r"<tr><td>Royal Rangers</td><td>\w+</td><td class='n'>(\d+)</td>",
            self.html)
        self.assertEqual(len(rows), 3)               # powerplay, middle, death
        self.assertEqual(sum(int(r) for r in rows), 186)

    def test_the_approach_duel_names_both_sides_picks(self):
        self.assertIn("Ultra Attack", self.html)     # innings 1 batting intent
        self.assertIn("Variation", self.html)        # the plan it faced
        self.assertIn("The Chess Match", self.html)  # and the combination it made

    def test_pitch_evolution_is_described(self):
        self.assertIn("cracks open", self.html.lower())

    def test_names_are_escaped_not_injected(self):
        html = ma.build_match_analysis_html(
            _state(bat_team_name="<script>alert(1)</script>"), _RESULT)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)


class ResilienceTests(unittest.TestCase):
    """A report is worth more than an exception. Every input is optional."""

    def test_a_state_with_no_v3_traces_still_renders(self):
        bare = _state()
        for key in ("momentum_history", "pressure_history", "chase_history",
                    "approach_log", "inn1_approach_log", "inn1_momentum_history",
                    "dps_trace"):
            bare.pop(key)
        html = ma.build_match_analysis_html(bare, _RESULT, _SCORECARD)
        self.assertIn("186/6", html)
        self.assertNotIn("Win probability", html)   # dropped, not faked
        self.assertIn("Phase by phase", html)

    def test_no_result_and_no_scorecard_still_render(self):
        html = ma.build_match_analysis_html(_state(), None, None)
        self.assertIn("Result unavailable", html)
        self.assertIn("Scorecard unavailable", html)

    def test_an_empty_state_does_not_raise(self):
        html = ma.build_match_analysis_html({}, None)
        self.assertIn("</html>", html)

    def test_a_match_abandoned_in_the_first_innings_renders(self):
        first = {"match_id": 3, "overs": 20, "innings": 1, "pitch_type": "Even",
                 "bat_team_name": "Solo XI", "total_runs": 61, "total_wickets": 2,
                 "over_runs": [10, 8, 12, 9, 11, 11], "fow": [], "momentum_history": [],
                 "partnership_history": [], "approach_log": [], "pressure_history": []}
        html = ma.build_match_analysis_html(first, None)
        self.assertIn("61/2", html)


class HelperTests(unittest.TestCase):
    def test_wickets_by_over_reads_both_over_and_ball_markers(self):
        # T20 "12.3" is the 13th over; The Hundred counts balls.
        self.assertEqual(ma._wickets_by_over([[50, 1, "X", "12.3"]]), {13: 1})
        self.assertEqual(ma._wickets_by_over([[50, 1, "X", "12.0"]]), {12: 1})
        self.assertEqual(ma._wickets_by_over([[50, 1, "X", "30 balls"]], bpu=5),
                         {6: 1})
        # Two in the same over are counted, not overwritten.
        self.assertEqual(
            ma._wickets_by_over([[50, 1, "X", "9.2"], [52, 2, "Y", "9.5"]]),
            {10: 2})
        # Junk never raises.
        self.assertEqual(ma._wickets_by_over([[1, 1], None, "nonsense"]), {})

    def test_phase_boundaries_scale_to_short_formats(self):
        self.assertEqual(ma._phase_of(1, 20), "Powerplay")
        self.assertEqual(ma._phase_of(6, 20), "Powerplay")
        self.assertEqual(ma._phase_of(7, 20), "Middle")
        self.assertEqual(ma._phase_of(15, 20), "Middle")
        self.assertEqual(ma._phase_of(16, 20), "Death")
        # A 5-over game still has all three phases.
        self.assertEqual({ma._phase_of(o, 5) for o in range(1, 6)},
                         {"Powerplay", "Middle", "Death"})

    def test_the_filename_carries_the_match_id(self):
        self.assertEqual(ma.analysis_filename(412), "MatchAnalysis412.html")

    def test_momentum_axis_matches_the_engine_cap(self):
        """The chart is drawn to +/-MOMENTUM_CAP; if the engine's cap moves and
        this does not, every momentum line silently clips."""
        html = ma.build_match_analysis_html(_state(), _RESULT)
        self.assertIn(f">{momentum_engine.MOMENTUM_CAP:g}<", html)


if __name__ == "__main__":
    unittest.main()

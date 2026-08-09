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
             "wickets": 0, "combo": None,
             "timeline": ["0", "1", "4", "0", "1", "0"]},
            {"over": 2, "bat": "ultra", "bowl": "variation", "runs": 12,
             "wickets": 0, "combo": "The Chess Match",
             "timeline": ["6", "0", "4", "1", "0", "1"]},
            {"over": 3, "bat": "rotate", "bowl": "defensive", "runs": 4,
             "wickets": 1, "combo": "The Grind",
             "timeline": ["1", "W", "0", "WD", "2", "0", "1"]},
            # An over logged before the ball marks were recorded — a match
            # already in flight when that column shipped.
            {"over": 4, "bat": "ultra", "bowl": "variation", "runs": 18,
             "wickets": 0, "combo": "The Chess Match"}],

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
             "wickets": 0, "combo": None,
             "timeline": ["1", "1", "4", "0", "1", "1"]},
            {"over": 2, "bat": "aggressive", "bowl": "mixed", "runs": 7,
             "wickets": 0, "combo": None,
             "timeline": ["0", "6", "0", "0", "1", "0"]}],
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


class ApproachDuelTests(unittest.TestCase):
    """The over-by-over duel — the section that answers "which approach met which".

    Everything it draws is already in ``state["approach_log"]``; these pin that it
    is *listed* rather than summarised away, and that a row survives an entry
    written before the ball marks were recorded.
    """

    def setUp(self):
        self.html = ma.build_match_analysis_html(_state(), _RESULT, _SCORECARD)
        # The block for the side that batted first.
        start = self.html.index("Royal Rangers batting")
        self.first = self.html[start:self.html.index("Titan Kings batting")]

    def _rows(self, block):
        return re.findall(r"<tr class='(?:tr-\w+)?'>(.*?)</tr>", block, re.S)

    def test_one_row_per_over_in_order(self):
        overs = re.findall(r"<tr class='[^']*'><td class='n'>(\d+)</td>", self.first)
        self.assertEqual(overs, ["1", "2", "3", "4"])

    def test_a_row_carries_both_picks_and_its_own_result(self):
        """Over 3 was Rotate Strike against Defensive for 4 and a wicket."""
        row = [r for r in self._rows(self.first) if r.startswith("<td class='n'>3<")]
        self.assertEqual(len(row), 1)
        row = row[0]
        self.assertIn("Rotate Strike", row)
        self.assertIn("Defensive", row)
        self.assertIn(">4</td>", row)   # runs
        self.assertIn(">1</td>", row)   # wickets

    def test_runs_and_wickets_come_before_the_ball_sequence(self):
        """Six columns do not fit a phone. The two the table exists for have to
        be the ones on screen; the sequence is what you scroll to."""
        duel = self.first[self.first.index("<table class='duel'>"):]
        head = duel[:duel.index("</thead>")]
        self.assertLess(head.index("<th>R</th>"), head.index("<th>Balls</th>"))
        self.assertLess(head.index("<th>W</th>"), head.index("<th>Balls</th>"))

    def test_the_ball_sequence_is_drawn(self):
        self.assertIn("b-four", self.first)     # the 4 in over 1
        self.assertIn("b-six", self.first)      # the 6 in over 2
        self.assertIn("b-wkt", self.first)      # the W in over 3
        self.assertIn("b-extra", self.first)    # the wide in over 3
        self.assertIn("b-dot", self.first)

    def test_an_over_with_no_recorded_balls_still_gets_a_row(self):
        """Over 4 of the fixture predates the column. It must not vanish, and it
        must not take the rest of the table with it."""
        overs = re.findall(r"<tr class='[^']*'><td class='n'>(\d+)</td>", self.first)
        self.assertIn("4", overs)

    def test_the_balls_column_disappears_when_no_over_has_one(self):
        """An entirely pre-column match gets a five-column table, not a column of
        blanks."""
        state = _state()
        for entry in state["inn1_approach_log"]:
            entry.pop("timeline", None)
        for entry in state["approach_log"]:
            entry.pop("timeline", None)
        html = ma.build_match_analysis_html(state, _RESULT, _SCORECARD)
        # Scoped to the duel table — the partnerships table has a Balls column
        # of its own and is not what this is about.
        duel = html[html.index("<table class='duel'>"):]
        duel = duel[:duel.index("</table>")]
        self.assertNotIn("<th>Balls</th>", duel)
        self.assertNotIn("class='balls'", duel)
        self.assertIn("Rotate Strike", html)

    def test_rows_are_tinted_by_who_won_the_over(self):
        # Over 2 went for 12 — the batting side's. Over 3 took a wicket.
        self.assertIn("tr-bat", self.first)
        self.assertIn("tr-bowl", self.first)

    def test_the_tint_is_never_the_only_signal(self):
        """Colour is decoration: every tinted row states its runs and wickets in
        text, so the table reads identically with colour off."""
        for row in self._rows(self.first):
            self.assertGreaterEqual(row.count("class='n'"), 3)


class MatchupGridTests(unittest.TestCase):
    """Approach vs approach: what each intent returned against each plan."""

    def setUp(self):
        self.html = ma.build_match_analysis_html(_state(), _RESULT, _SCORECARD)

    def test_a_matchup_cell_aggregates_every_over_of_that_pairing(self):
        """Ultra Attack met Variation twice in innings 1 — 12 and 18 off two
        overs, so the cell reads 15.0 RPO over 30 runs."""
        self.assertIn("<b>15.0</b>", self.html)
        self.assertIn("30r 0w · 2", self.html)

    def test_a_matchup_that_never_happened_is_empty_not_zero(self):
        """A blank cell means "not tried". Printing 0.0 would mean "tried and
        scored nothing", which is a different and wrong statement."""
        self.assertIn("cell empty", self.html)
        self.assertIn(">–</td>", self.html)

    def test_every_intent_and_plan_has_a_row_and_a_column(self):
        for _k, _emoji, name in ma.approach_modifiers.BATTING_APPROACHES:
            self.assertIn(f"<th class='rowhead'>{name}</th>", self.html)
        for _k, _emoji, name in ma.approach_modifiers.BOWLING_APPROACHES:
            self.assertIn(f'title="{name}"', self.html)

    def test_shading_is_banded_by_runs_per_over_not_by_the_innings(self):
        """Absolute bands, so a cell means the same thing in both innings and in
        every match — a scale relative to the innings would paint the best of a
        bad set of overs as a good one."""
        self.assertEqual(ma._rpo_band(4.0), 0)
        self.assertEqual(ma._rpo_band(7.0), 1)
        self.assertEqual(ma._rpo_band(9.0), 2)
        self.assertEqual(ma._rpo_band(11.0), 3)
        self.assertEqual(ma._rpo_band(15.0), 4)
        # Monotone: a more expensive over never gets a quieter band.
        bands = [ma._rpo_band(r) for r in (0, 3, 6, 8, 10, 12, 20)]
        self.assertEqual(bands, sorted(bands))

    def test_the_shading_has_a_legend_and_the_number_is_always_present(self):
        self.assertIn("Shading runs quiet", self.html)
        self.assertIn("DEF Defensive", self.html)


class BotMatchRevealTests(unittest.TestCase):
    """The file reveals both captains' picks in every match, bot ones included.

    That is a deliberate line — mid-match against post-match. `_render_over_summary`
    still withholds the bot's plan while the match is being played, because a mix
    read off the screen can be countered over the next few overs; a finished
    match's record has nothing left to protect. Pinned here so a future change
    back to hiding is a conscious one rather than a silent regression.
    """

    def test_a_bot_match_shows_both_columns(self):
        state = _state(is_bot_match=True, bot_user_id=99, bot_persona="tactician")
        html = ma.build_match_analysis_html(state, _RESULT, _SCORECARD)
        self.assertIn("Ultra Attack", html)
        self.assertIn("Variation", html)
        self.assertIn("The Chess Match", html)


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

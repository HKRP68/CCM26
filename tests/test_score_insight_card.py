"""The projection / score-comparison section on the /cipl approach card.

Exercises both live renderings end-to-end on a real simulated match:
``_approach_card`` (HTML) and ``_approach_card_blocks`` (Bot API rich text).
Confirms the first-innings PROJECTED SCORE block and the second-innings SCORE
COMPARISON block appear in both, carry consistent numbers, keep the block tree
well-formed (so Telegram accepts it), and that the existing scorecard sections
and approach buttons are preserved.
"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import cipl_match as cm  # noqa: E402
from services import score_projection as sp  # noqa: E402
from tests.test_cipl_approach import _make_state  # noqa: E402
from tests.test_rich_text_surfaces import BlockTreeAssertions, flatten  # noqa: E402

try:
    import handlers.cipl_play as cp  # needs python-telegram-bot
    _HAVE_CP = True
except Exception:  # pragma: no cover - environment without Telegram deps
    _HAVE_CP = False


def _play_over(s, approach="balanced"):
    s["current_bowler"] = cm.eligible_bowlers(s)[0]
    s["bowling_approach"] = "balanced"
    s["batting_approach"] = approach
    cm.simulate_over(s)


def _first_innings(overs_played, *, overs=20, seed=7):
    random.seed(seed)
    s = _make_state(overs=overs)
    s.update(bat_team_name="Vindhya", bowl_team_name="Tamil Dragon",
             bat_team_code="VIN", bowl_team_code="TAM",
             bat_team_emoji="🔵", bowl_team_emoji="🟢")
    for _ in range(overs_played):
        if cm.is_innings_over(s):
            break
        _play_over(s, "aggressive")
    s["current_bowler"] = cm.eligible_bowlers(s)[0]
    return s


def _second_innings(overs_played, *, overs=20, seed=7):
    random.seed(seed)
    s = _make_state(overs=overs)
    s.update(bat_team_name="Vindhya", bowl_team_name="Tamil Dragon",
             bat_team_code="VIN", bowl_team_code="TAM",
             bat_team_emoji="🔵", bowl_team_emoji="🟢")
    guard = 0
    while not cm.is_innings_over(s) and guard < 60:
        _play_over(s, "balanced")
        guard += 1
    cm.end_first_innings(s)
    for _ in range(overs_played):
        if cm.is_innings_over(s):
            break
        _play_over(s, "aggressive")
    s["current_bowler"] = cm.eligible_bowlers(s)[0]
    return s


@unittest.skipUnless(_HAVE_CP, "handlers.cipl_play (python-telegram-bot) not importable")
class ProjectionCardTests(unittest.TestCase, BlockTreeAssertions):

    def test_projection_in_html_and_rich_agree(self):
        s = _first_innings(12)
        proj = sp.project_first_innings(s)
        self.assertIsNotNone(proj)

        html_card = cp._approach_card(s)
        # Projected rides on the compact CRR | Projected line; the par/range/
        # outlook detail and the disclaimer are intentionally not on the card.
        self.assertIn(f"<b>Projected</b> {proj['expected']}", html_card)
        self.assertNotIn("Illustrative", html_card)
        self.assertNotIn("Range ", html_card)

        blocks = cp._approach_card_blocks(s)
        self.assertGreater(self.assertWellFormed(blocks), 0)
        text = flatten(blocks)
        self.assertIn("Projected", text)
        self.assertIn(str(proj["expected"]), text)

    def test_no_projection_before_first_ball(self):
        s = _first_innings(0)
        self.assertNotIn("Projected", cp._approach_card(s))
        self.assertNotIn("Projected", flatten(cp._approach_card_blocks(s)))

    def test_projection_updates_after_an_over(self):
        s = _first_innings(6)
        first = sp.project_first_innings(s)["expected"]
        self.assertIn(f"<b>Projected</b> {first}", cp._approach_card(s))
        for _ in range(4):
            _play_over(s, "aggressive")
        s["current_bowler"] = cm.eligible_bowlers(s)[0]
        later = sp.project_first_innings(s)["expected"]
        self.assertIn(f"<b>Projected</b> {later}", cp._approach_card(s))

    def test_par_outlook_detail_not_shown_on_first_innings_card(self):
        # The par/range/outlook detail (and disclaimer) are deliberately kept
        # off the first-innings card; only the Projected figure is surfaced.
        s = _first_innings(10)
        self.assertIsNotNone(sp.par_outlook(s))   # the helper still works
        card = cp._approach_card(s)
        self.assertNotIn("Par ", card)
        self.assertNotIn("Current outlook", card)
        self.assertNotIn("Illustrative", card)
        text = flatten(cp._approach_card_blocks(s))
        self.assertNotIn("Illustrative", text)
        self.assertNotIn("Current outlook", text)

    def test_rrr_trend_and_chase_chance_shown(self):
        s = _second_innings(8)
        card = cp._approach_card(s)
        trend = sp.rrr_trend(s)
        if trend:
            self.assertIn("RRR", card)
            self.assertIn(f"{trend['then']:.2f}→{trend['now']:.2f}", card)
        chance = sp.chase_win_chance(s)
        if chance is not None:
            self.assertIn(f"🏆 {chance}%", card)

    def test_comparison_in_html_and_rich(self):
        s = _second_innings(12)
        comp = sp.compare_innings(s)
        self.assertIsNotNone(comp)
        stage = comp["stage"]
        # Codes used in the compact comparison: defending (bowl) / chasing (bat).
        d_code = s.get("bowl_team_code")
        c_code = s.get("bat_team_code")

        html_card = cp._approach_card(s)
        self.assertIn(f"@{stage} ov", html_card)
        self.assertIn(f"🔵 {d_code} {cp._comp_score(comp['inn1'])}", html_card)
        self.assertIn(f"🟢 {c_code} {cp._comp_score(comp['inn2'])}", html_card)
        # Chase keeps its Need / RRR / CRR line.
        self.assertIn("Need", html_card)
        self.assertIn("RRR", html_card)

        blocks = cp._approach_card_blocks(s)
        self.assertGreater(self.assertWellFormed(blocks), 0)
        text = flatten(blocks)
        self.assertIn("Comparison", text)
        self.assertIn(d_code, text)
        self.assertIn(c_code, text)

    def test_comparison_numbers_match_history(self):
        s = _second_innings(12)
        comp = sp.compare_innings(s)
        html_card = cp._approach_card(s)
        d_code = s.get("bowl_team_code")
        c_code = s.get("bat_team_code")
        self.assertIn(f"🔵 {d_code} {cp._comp_score(comp['inn1'])}", html_card)
        self.assertIn(f"🟢 {c_code} {cp._comp_score(comp['inn2'])}", html_card)

    def test_last_three_overs_block_when_deep_enough(self):
        s = _second_innings(12)
        comp = sp.compare_innings(s)
        if comp["last"]:
            self.assertIn(f"L{comp['last']['n']}:", cp._approach_card(s))
            self.assertIn(f"L{comp['last']['n']}:",
                          flatten(cp._approach_card_blocks(s)))

    def test_prompt_and_scorecard_preserved(self):
        # The projection is additive: the batting/bowling scorecard sections and
        # the trailing approach prompt the captain acts on must still be there.
        s = _first_innings(8)
        prompt = [["Pick your ", cp.R.bold("batting approach"), "."]]
        blocks = cp._approach_card_blocks(s, prompt)
        text = flatten(blocks)
        self.assertIn("BATTER", text)              # batting table
        self.assertIn("Bowlers", text)             # expandable bowler list
        self.assertIn("batting approach", text)    # the prompt survived
        self.assertIn("Projected", text)           # new section present too
        html_card = cp._approach_card(s)
        self.assertIn("BOWLERS", html_card)
        self.assertIn("Projected", html_card)

    def test_the_hundred_projection_renders(self):
        random.seed(3)
        s = _make_state(overs=20, ball_format="The100")
        s.update(bat_team_name="Vindhya", bowl_team_name="Tamil Dragon",
                 bat_team_code="VIN", bowl_team_code="TAM")
        for _ in range(8):
            if cm.is_innings_over(s):
                break
            _play_over(s, "aggressive")
        s["current_bowler"] = cm.eligible_bowlers(s)[0]
        card = cp._approach_card(s)
        self.assertIn("Projected", card)
        self.assertGreater(self.assertWellFormed(cp._approach_card_blocks(s)), 0)

    def test_recent_bowlers_show_last_two_used(self):
        # After a few overs the bowling table lists the last two bowlers used,
        # current first.
        s = _first_innings(3)
        entries = cp._recent_bowler_entries(s)
        self.assertTrue(entries)
        self.assertLessEqual(len(entries), 2)
        self.assertTrue(entries[0]["current"])
        card = cp._approach_card(s)
        for e in entries:
            self.assertIn(e["name"], card)


if __name__ == "__main__":
    unittest.main()

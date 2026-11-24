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
        self.assertIn("📊 <b>PROJECTED SCORE</b>", html_card)
        self.assertIn(f"Expected: <b>{proj['expected']}</b>", html_card)
        self.assertIn("illustrative, not fixed values", html_card)
        if proj["low"] is not None:
            self.assertIn(f"Likely range: {proj['low']}–{proj['high']}", html_card)

        blocks = cp._approach_card_blocks(s)
        self.assertGreater(self.assertWellFormed(blocks), 0)
        text = flatten(blocks)
        self.assertIn("Projected", text)
        self.assertIn(str(proj["expected"]), text)

    def test_no_projection_before_first_ball(self):
        s = _first_innings(0)
        self.assertNotIn("PROJECTED SCORE", cp._approach_card(s))
        self.assertNotIn("Projected", flatten(cp._approach_card_blocks(s)))

    def test_projection_updates_after_an_over(self):
        s = _first_innings(6)
        first = sp.project_first_innings(s)["expected"]
        self.assertIn(f"Expected: <b>{first}</b>", cp._approach_card(s))
        for _ in range(4):
            _play_over(s, "aggressive")
        s["current_bowler"] = cm.eligible_bowlers(s)[0]
        later = sp.project_first_innings(s)["expected"]
        self.assertIn(f"Expected: <b>{later}</b>", cp._approach_card(s))

    def test_comparison_in_html_and_rich(self):
        s = _second_innings(12)
        comp = sp.compare_innings(s)
        self.assertIsNotNone(comp)
        stage = comp["stage"]

        html_card = cp._approach_card(s)
        self.assertIn(f"SCORE COMPARISON — {stage} OVERS", html_card)
        self.assertIn("🔵 Vindhya:", html_card)
        self.assertIn(f"🟢 {comp['inn2']['name']}:", html_card)
        # Chase keeps its target / CRR / RRR line.
        self.assertIn("RRR", html_card)

        blocks = cp._approach_card_blocks(s)
        self.assertGreater(self.assertWellFormed(blocks), 0)
        text = flatten(blocks)
        self.assertIn("SCORE COMPARISON", text)
        self.assertIn(comp["inn1"]["name"], text)
        self.assertIn(comp["inn2"]["name"], text)

    def test_comparison_numbers_match_history(self):
        s = _second_innings(12)
        comp = sp.compare_innings(s)
        html_card = cp._approach_card(s)
        i1, i2 = comp["inn1"], comp["inn2"]
        exp1 = f"{i1['runs']}/{i1['wkts']}" if i1["wkts"] is not None else str(i1["runs"])
        exp2 = f"{i2['runs']}/{i2['wkts']}" if i2["wkts"] is not None else str(i2["runs"])
        self.assertIn(f"🔵 {i1['name']}: {exp1}", html_card)
        self.assertIn(f"🟢 {i2['name']}: {exp2}", html_card)

    def test_last_three_overs_block_when_deep_enough(self):
        s = _second_innings(12)
        comp = sp.compare_innings(s)
        if comp["last"]:
            self.assertIn("LAST 3 OVERS", cp._approach_card(s))
            self.assertIn("LAST 3 OVERS", flatten(cp._approach_card_blocks(s)))

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
        self.assertIn("PROJECTED SCORE", html_card)

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
        self.assertIn("PROJECTED SCORE", card)
        self.assertGreater(self.assertWellFormed(cp._approach_card_blocks(s)), 0)


if __name__ == "__main__":
    unittest.main()

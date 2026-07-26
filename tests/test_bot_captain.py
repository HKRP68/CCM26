"""Tests for the AI captain that plays the bot's side in /lpbot and /ciplbot.

Pure state-in/decision-out, so these run under plain unittest with no Telegram
and no DB. The point of each test is a tactical guarantee a player would notice
if it broke: the bot never bowls an illegal over, it saves something for the
death, and its batting intent actually tracks the match situation.
"""

import random
import unittest

from engine.approach_modifiers import BATTING_KEYS, BOWLING_KEYS
from services import bot_captain as bc
from services import cipl_match as cm


class SeededTestCase(unittest.TestCase):
    """Bowler choice and the toss are deliberately random, and several tests
    below assert on the shape of a 200-sample distribution. Seeding keeps those
    assertions reproducible instead of leaving a small chance of a red CI run on
    an unlucky draw."""

    def setUp(self):
        random.seed(20260726)


def _player(rid, name, category, bowl_rating, bat_rating=50, traits=()):
    return {
        "roster_id": rid, "player_id": rid, "name": name,
        "category": category, "rating": max(bat_rating, bowl_rating),
        "bat_rating": bat_rating, "bowl_rating": bowl_rating,
        "bowl_style": "Fast", "bowl_hand": "Right", "bat_hand": "Right",
        "traits": [{"effect_key": k, "level": 3} for k in traits],
    }


def _bowl_xi():
    return [
        _player(1, "Ace", "Bowler", 90, 20, traits=("bowl_death",)),
        _player(2, "Newball", "Bowler", 86, 18, traits=("bowl_powerplay",)),
        _player(3, "Spinner", "Bowler", 80, 22),
        _player(4, "Fourth", "Bowler", 74, 25),
        _player(5, "Allround", "All-rounder", 70, 65),
        _player(6, "Opener", "Batsman", 20, 88),
        _player(7, "Keeper", "Wicket Keeper", 15, 80),
        _player(8, "Three", "Batsman", 18, 78),
        _player(9, "Four", "Batsman", 16, 72),
        _player(10, "Five", "Batsman", 14, 68),
        _player(11, "Six", "Batsman", 12, 60),
    ]


def _state(**over):
    bat_xi = [_player(100 + i, f"Bat{i}", "Batsman", 20, 80 - i) for i in range(11)]
    bowl_xi = _bowl_xi()
    state = {
        "match_id": 1, "overs": 20, "ball_format": "T20",
        "innings": 1, "target": None,
        "current_over": 1, "current_ball": 0,
        "total_runs": 0, "total_wickets": 0,
        "wicket_limit": 10,
        "bat_xi": bat_xi, "bowl_xi": bowl_xi,
        "batting_order": list(bat_xi),
        "striker_idx": 0, "non_striker_idx": 1,
        "bat_stats": {str(p["roster_id"]): {"runs": 0, "balls": 0} for p in bat_xi},
        "bowl_stats": {str(p["roster_id"]): {"balls": 0, "runs": 0, "wickets": 0}
                       for p in bowl_xi},
        "prev_bowler_rid": None,
        "spell_rid": None, "spell_units": 0,
    }
    state.update(over)
    return state


class PhaseTests(SeededTestCase):
    def test_t20_phases(self):
        self.assertEqual(bc.phase(_state(current_over=1)), "powerplay")
        self.assertEqual(bc.phase(_state(current_over=6)), "powerplay")
        self.assertEqual(bc.phase(_state(current_over=10)), "middle")
        self.assertEqual(bc.phase(_state(current_over=17)), "death")
        self.assertEqual(bc.phase(_state(current_over=20)), "death")

    def test_short_format_still_has_all_three_phases(self):
        seen = {bc.phase(_state(overs=5, current_over=o)) for o in range(1, 6)}
        self.assertEqual(seen, {"powerplay", "middle", "death"})


class BowlerSelectionTests(SeededTestCase):
    def test_never_picks_the_previous_bowler(self):
        # Ace is the strongest bowler by a distance, so only the no-back-to-back
        # rule can keep them out of the next over.
        state = _state(current_over=2, prev_bowler_rid=1)
        for _ in range(30):
            self.assertNotEqual(bc.pick_bowler(state)["roster_id"], 1)

    def test_never_exceeds_the_per_bowler_quota(self):
        state = _state(current_over=10)
        quota = cm.max_bowler_overs(state)
        # Ace and Newball have already bowled out.
        for rid in (1, 2):
            state["bowl_stats"][str(rid)]["balls"] = quota * 6
        for _ in range(30):
            self.assertNotIn(bc.pick_bowler(state)["roster_id"], (1, 2))

    def test_prefers_a_death_specialist_at_the_death(self):
        state = _state(current_over=19)
        picks = [bc.pick_bowler(state)["roster_id"] for _ in range(200)]
        # Ace carries bowl_death and the best rating — they should dominate.
        self.assertGreater(picks.count(1), picks.count(3))

    def test_holds_a_frontline_bowler_back_for_the_death(self):
        # Over 14 with two overs of death still to cover: Ace has exactly two
        # overs left, so those should be earmarked rather than spent now.
        state = _state(current_over=14)
        quota = cm.max_bowler_overs(state)
        state["bowl_stats"]["1"]["balls"] = (quota - 2) * 6
        state["bowl_stats"]["2"]["balls"] = (quota - 2) * 6
        picks = [bc.pick_bowler(state)["roster_id"] for _ in range(200)]
        self.assertLess(picks.count(1), picks.count(3))

    def test_always_returns_an_eligible_bowler(self):
        state = _state(current_over=8, prev_bowler_rid=3)
        eligible = {p["roster_id"] for p in cm.eligible_bowlers(state)}
        for _ in range(50):
            self.assertIn(bc.pick_bowler(state)["roster_id"], eligible)

    def test_no_bowlers_returns_none(self):
        state = _state(bowl_xi=[], bat_xi=[], batting_order=[])
        self.assertIsNone(bc.pick_bowler(state))


class BowlingApproachTests(SeededTestCase):
    def test_always_a_valid_key(self):
        for over in range(1, 21):
            for _ in range(10):
                self.assertIn(bc.pick_bowling_approach(_state(current_over=over)),
                              BOWLING_KEYS)

    def test_attacks_a_brand_new_batter(self):
        state = _state(current_over=9)
        self.assertEqual(bc.pick_bowling_approach(state), "aggressive")

    def test_mixes_it_up_at_the_death(self):
        state = _state(current_over=18)
        state["bat_stats"]["100"] = {"runs": 30, "balls": 20}
        self.assertEqual(bc.pick_bowling_approach(state), "mixed")

    def test_varies_against_a_set_batter(self):
        state = _state(current_over=11)
        state["bat_stats"]["100"] = {"runs": 40, "balls": 20}   # SR 200
        self.assertEqual(bc.pick_bowling_approach(state), "variation")

    def test_squeezes_when_the_chase_has_run_away(self):
        state = _state(innings=2, target=250, total_runs=60, current_over=14)
        state["bat_stats"]["100"] = {"runs": 20, "balls": 15}
        self.assertEqual(bc.pick_bowling_approach(state), "defensive")


class BattingApproachTests(SeededTestCase):
    def test_always_a_valid_key(self):
        for over in range(1, 21):
            for _ in range(10):
                self.assertIn(bc.pick_batting_approach(_state(current_over=over)),
                              BATTING_KEYS)

    def test_intent_rises_with_the_required_rate(self):
        def approach(runs):
            state = _state(innings=2, target=runs + 1, total_runs=0,
                           current_over=8)
            return BAT_INDEX[bc.pick_batting_approach(state)]

        # 60 needed off 78 balls (RRR ~4.6) vs 200 off 78 (RRR ~15).
        self.assertLess(approach(60), approach(200))

    def test_wickets_lost_pulls_the_intent_back(self):
        calm = _state(innings=2, target=140, total_runs=60, current_over=10,
                      total_wickets=1)
        rocky = dict(calm, total_wickets=8)
        self.assertLessEqual(BAT_INDEX[bc.pick_batting_approach(rocky)],
                             BAT_INDEX[bc.pick_batting_approach(calm)])

    def test_a_desperate_chase_overrides_caution(self):
        # 120 needed off 30 balls with 8 down — blocking cannot win this.
        state = _state(innings=2, target=121, total_runs=0, current_over=16,
                       total_wickets=8)
        self.assertGreaterEqual(BAT_INDEX[bc.pick_batting_approach(state)],
                                BAT_INDEX["aggressive"])

    def test_a_tailender_plays_within_themselves(self):
        base = _state(current_over=10)
        base["batting_order"][0]["bat_rating"] = 25
        base["batting_order"][0]["rating"] = 30
        star = _state(current_over=10)
        star["batting_order"][0]["bat_rating"] = 92
        star["batting_order"][0]["rating"] = 92
        self.assertLess(BAT_INDEX[bc.pick_batting_approach(base)],
                        BAT_INDEX[bc.pick_batting_approach(star)])


class TossTests(SeededTestCase):
    def test_bot_elects_bat_or_bowl_and_uses_both(self):
        picks = {bc.elect_toss_decision() for _ in range(200)}
        self.assertEqual(picks, {"bat", "bowl"})


BAT_INDEX = {k: i for i, k in enumerate(bc.BAT_LADDER)}


if __name__ == "__main__":
    unittest.main()

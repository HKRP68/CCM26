"""Tests for the AI captain's game-theoretic brain (services/bot_tactics.py).

Two kinds of claim are worth pinning down here.

The first is that the *model* is honest: the ball model has to move the way the
real engine moves (a better bowler concedes less, the death is worth more than
the powerplay), and the value model has to price a wicket the way a captain
would (cheap at 20/0, ruinous at 150/8).

The second is that the *solver* does what it promises. A Nash mix for a
zero-sum game is what makes the bot unexploitable, and the properties that
matter — it is a probability distribution, it beats a fixed counter-strategy,
it never leaves a free lunch on the table — are all directly checkable.

Pure functions throughout, so no Telegram and no DB.
"""

import unittest

from engine.approach_modifiers import BATTING_KEYS, BOWLING_KEYS
from services import bot_tactics as bt


def _player(rid, bat, bowl, category="Batsman"):
    return {"roster_id": rid, "player_id": rid, "name": f"P{rid}",
            "category": category, "rating": max(bat, bowl),
            "bat_rating": bat, "bowl_rating": bowl,
            "bowl_style": "Fast", "bowl_hand": "Right", "bat_hand": "Right",
            "traits": []}


def _state(over=9, bat=70, bowl=70, faced=10, wickets=2, runs=60,
           pitch="Hard", **extra):
    order = [_player(100 + i, bat, 20) for i in range(11)]
    state = {
        "match_id": 1, "overs": 20, "ball_format": "T20", "innings": 1,
        "target": None, "pitch_type": pitch,
        "current_over": over, "current_ball": 0,
        "total_runs": runs, "total_wickets": wickets, "wicket_limit": 10,
        "batting_order": order, "striker_idx": 0, "non_striker_idx": 1,
        "bat_stats": {str(order[0]["roster_id"]):
                      {"runs": int(faced * 1.2), "balls": faced}},
        "bowl_stats": {},
        "current_bowler": _player(1, 20, bowl, "Bowler"),
    }
    state.update(extra)
    return state


class BallModelTests(unittest.TestCase):
    def test_probabilities_are_a_distribution(self):
        probs = bt.ball_probabilities(_state())
        self.assertAlmostEqual(sum(probs.values()), 1.0, places=9)
        self.assertTrue(all(p >= 0 for p in probs.values()))

    def test_a_better_bowler_concedes_less_and_takes_more(self):
        weak = bt.ball_probabilities(_state(bowl=45))
        strong = bt.ball_probabilities(_state(bowl=90))
        runs = lambda p: sum(p[k] * bt.RUN_VALUE[k] for k in p)
        self.assertGreater(runs(weak), runs(strong))
        self.assertGreater(strong["Wicket"], weak["Wicket"])

    def test_the_death_scores_faster_than_the_middle(self):
        runs = lambda p: sum(p[k] * bt.RUN_VALUE[k] for k in p)
        self.assertGreater(runs(bt.ball_probabilities(_state(over=18))),
                           runs(bt.ball_probabilities(_state(over=9))))

    def test_a_fresh_batter_scores_slower_than_a_settled_one(self):
        runs = lambda p: sum(p[k] * bt.RUN_VALUE[k] for k in p)
        self.assertLess(runs(bt.ball_probabilities(_state(faced=0))),
                        runs(bt.ball_probabilities(_state(faced=10))))

    def test_the_approaches_applied_are_the_engine_s_own(self):
        base = bt.ball_probabilities(_state())
        ultra = bt.approach_probabilities(base, "ultra", "balanced", 70)
        block = bt.approach_probabilities(base, "defensive", "balanced", 70)
        self.assertGreater(ultra["Six"], block["Six"])
        self.assertGreater(ultra["Wicket"], block["Wicket"])
        self.assertAlmostEqual(sum(ultra.values()), 1.0, places=9)


class OverDistributionTests(unittest.TestCase):
    def test_it_is_a_distribution_over_runs_and_wickets(self):
        probs = bt.ball_probabilities(_state())
        dist = bt.over_distribution(probs, 6, 8)
        self.assertAlmostEqual(sum(dist.values()), 1.0, places=6)
        self.assertTrue(all(0 <= w <= 6 and r >= 0 for r, w in dist))

    def test_an_innings_cannot_lose_more_wickets_than_it_has(self):
        probs = bt.ball_probabilities(_state())
        dist = bt.over_distribution(probs, 6, 2)
        self.assertLessEqual(max(w for _r, w in dist), 2)

    def test_the_mean_over_is_a_plausible_t20_over(self):
        probs = bt.ball_probabilities(_state())
        dist = bt.over_distribution(probs, 6, 8)
        mean_runs = sum(p * r for (r, _w), p in dist.items())
        self.assertTrue(3.0 < mean_runs < 16.0, mean_runs)


class WicketValueTests(unittest.TestCase):
    def test_a_wicket_costs_more_as_the_batting_runs_out(self):
        early = bt.wicket_cost(_state(over=3, wickets=0))
        late = bt.wicket_cost(_state(over=15, wickets=7))
        self.assertGreater(late, early)

    def test_survival_shrinks_with_wickets_in_hand(self):
        self.assertGreater(bt.survivable_balls(10), bt.survivable_balls(4))
        self.assertGreater(bt.survivable_balls(4), bt.survivable_balls(1))
        self.assertEqual(bt.survivable_balls(0), 0)


class ChaseModelTests(unittest.TestCase):
    def test_the_obvious_cases(self):
        self.assertEqual(bt.chase_win_prob(0, 12, 5), 1.0)      # already there
        self.assertEqual(bt.chase_win_prob(10, 0, 5), 0.0)      # no balls left
        self.assertEqual(bt.chase_win_prob(10, 12, 0), 0.0)     # all out

    def test_a_steeper_ask_is_harder(self):
        easy = bt.chase_win_prob(30, 60, 8)
        hard = bt.chase_win_prob(90, 60, 8)
        self.assertGreater(easy, hard)

    def test_wickets_in_hand_matter(self):
        self.assertGreater(bt.chase_win_prob(40, 30, 8),
                           bt.chase_win_prob(40, 30, 1))

    def test_the_same_rate_is_riskier_with_fewer_balls_left(self):
        """10 an over off 60 balls is a grind; off 6 it is a coin toss.

        The model has to know the difference, because it is the whole reason a
        chase is played differently at the death than in the middle.
        """
        long_haul = bt.chase_win_prob(100, 60, 8)
        last_over = bt.chase_win_prob(10, 6, 8)
        self.assertGreater(last_over, long_haul)


class SolverTests(unittest.TestCase):
    def test_the_payoff_matrix_covers_every_match_up(self):
        matrix = bt.payoff_matrix(_state())
        self.assertEqual(len(matrix), len(BATTING_KEYS))
        self.assertTrue(all(len(row) == len(BOWLING_KEYS) for row in matrix))

    def test_both_mixes_are_probability_distributions(self):
        matrix = bt.payoff_matrix(_state())
        bat, bowl, _value = bt.solve_zero_sum(matrix)
        for mixed in (bat, bowl):
            self.assertAlmostEqual(sum(mixed), 1.0, places=4)
            self.assertTrue(all(p >= -1e-9 for p in mixed))

    def test_the_solution_is_an_equilibrium(self):
        """Neither captain can gain by abandoning their half of the solution.

        This is the property that makes the bot unexploitable, so it is worth
        asserting directly rather than trusting the solver: against the bot's
        bowling mix, no single batting approach beats the bot's own batting
        mix by more than solver tolerance — and vice versa.
        """
        matrix = bt.payoff_matrix(_state())
        bat, bowl, value = bt.solve_zero_sum(matrix, iters=4000)
        scale = max(abs(v) for row in matrix for v in row) or 1.0
        tolerance = 0.03 * scale

        best_bat = max(sum(matrix[i][j] * bowl[j] for j in range(len(bowl)))
                       for i in range(len(bat)))
        self.assertLessEqual(best_bat - value, tolerance)

        best_bowl = min(sum(matrix[i][j] * bat[i] for i in range(len(bat)))
                        for j in range(len(bowl)))
        self.assertGreaterEqual(best_bowl - value, -tolerance)

    def test_a_flat_matrix_is_not_treated_as_a_decision(self):
        """A decided match must not be dressed up as a tactical dilemma."""
        self.assertFalse(bt.matrix_is_decisive([[0.0] * 5 for _ in range(5)]))
        self.assertFalse(bt.matrix_is_decisive([]))
        self.assertTrue(bt.matrix_is_decisive(bt.payoff_matrix(_state())))

    def test_the_best_response_punishes_a_predictable_opponent(self):
        """Against a batter who only ever slogs, the reply is the slog-stopper."""
        state = _state()
        matrix = bt.payoff_matrix(state)
        always_ultra = {k: (0.9 if k == "ultra" else 0.025) for k in bt.BAT_KEYS}
        reply = bt.exploit_mix(matrix, always_ultra, batting=False,
                               keys=bt.BOWL_KEYS)
        self.assertAlmostEqual(sum(reply.values()), 1.0, places=6)
        # The plan the bot leans on must be one that actually hurts Ultra —
        # i.e. one of the cheapest cells in the Ultra row.
        ultra_row = matrix[bt.BAT_KEYS.index("ultra")]
        cheapest = sorted(range(len(ultra_row)), key=lambda j: ultra_row[j])[:2]
        favourite = max(reply, key=reply.get)
        self.assertIn(bt.BOWL_KEYS.index(favourite), cheapest)


class BowlerChoiceTests(unittest.TestCase):
    def test_a_better_bowler_makes_the_over_cheaper(self):
        state = _state()
        weak = bt.bowler_edge(state, _player(2, 20, 45, "Bowler"))
        strong = bt.bowler_edge(state, _player(3, 20, 90, "Bowler"))
        self.assertGreater(weak, strong)   # value to the BATTING side


class CacheTests(unittest.TestCase):
    def test_the_same_situation_is_solved_once(self):
        state = _state()
        bt._SOLVE_CACHE.clear()
        bt.solved_over(state)
        self.assertEqual(len(bt._SOLVE_CACHE), 1)
        bt.solved_over(state)
        self.assertEqual(len(bt._SOLVE_CACHE), 1)
        # A different over is a different problem and must not reuse it.
        bt.solved_over(_state(over=17))
        self.assertEqual(len(bt._SOLVE_CACHE), 2)

    def test_the_cache_cannot_grow_without_bound(self):
        bt._SOLVE_CACHE.clear()
        for over in range(1, 21):
            for runs in range(0, 40, 5):
                bt.solved_over(_state(over=over, runs=runs))
        self.assertLessEqual(len(bt._SOLVE_CACHE), bt._SOLVE_CACHE_MAX)


if __name__ == "__main__":
    unittest.main()

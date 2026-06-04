import unittest
from services.match_engine import chase_requirements, compute_match_result, is_innings_over


class MatchCompletionLogicTests(unittest.TestCase):
    def chase_state(self, *, runs=0, wickets=0, over=1, ball=0, wicket_limit=10):
        return {
            "innings": 2,
            "target": 151,
            "total_runs": runs,
            "total_wickets": wickets,
            "wicket_limit": wicket_limit,
            "overs": 20,
            "current_over": over,
            "current_ball": ball,
            "bat_team_id": 2,
            "bowl_team_id": 1,
            "bat_team_name": "Team B",
            "bowl_team_name": "Team A",
        }

    def test_chase_ends_immediately_at_target_with_wickets_margin(self):
        state = self.chase_state(runs=151, wickets=4, over=18, ball=2)
        self.assertTrue(is_innings_over(state))
        self.assertEqual(compute_match_result(state)["text"], "Team B won by 6 wickets")
        self.assertEqual(chase_requirements(state), {
            "target": 151, "runs_required": 0, "balls_remaining": 16,
        })

    def test_challenge_chase_uses_two_wicket_limit(self):
        state = self.chase_state(runs=151, wickets=1, wicket_limit=2)
        result = compute_match_result(state)
        self.assertEqual(result["margin_value"], 1)
        self.assertEqual(result["text"], "Team B won by 1 wicket")

    def test_all_out_before_target_is_runs_win(self):
        state = self.chase_state(runs=140, wickets=10, over=18, ball=4)
        self.assertTrue(is_innings_over(state))
        self.assertEqual(compute_match_result(state)["text"], "Team A won by 10 runs")

    def test_completed_overs_is_runs_win(self):
        state = self.chase_state(runs=145, wickets=7, over=21, ball=0)
        self.assertTrue(is_innings_over(state))
        self.assertEqual(compute_match_result(state)["text"], "Team A won by 5 runs")

    def test_level_scores_at_innings_end_are_tied(self):
        state = self.chase_state(runs=150, wickets=10, over=19, ball=3)
        self.assertTrue(is_innings_over(state))
        self.assertEqual(compute_match_result(state)["text"], "Match Tied")



if __name__ == "__main__":
    unittest.main()

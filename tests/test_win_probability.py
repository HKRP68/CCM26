"""Tests for the live win-probability model used by the mini-app broadcast bar.

Exercises the engine.chase_chance path the Arena serializer feeds the bar with,
locking the key invariants: bat% + bowl% == 100, and a harder ask never reads
as more batting-favoured than an easier one.
"""

import unittest

from engine import chase_chance as cc


def _win_prob(runs_needed, wickets_lost, balls_left):
    info = cc.final_chase_chance(
        runs_needed, wickets_lost,
        batter_mod=cc.batter_modifier({"bat_rating": 80}, {"bat_rating": 75}, 20),
        bowler_mod=cc.bowler_modifier({"bowl_rating": 85}),
        pitch_mod=cc.pitch_modifier("Flat"),
        momentum_mod=cc.momentum_modifier(runs_needed * 6.0 / balls_left),
    )
    info = cc.apply_feasibility(info, runs_needed, balls_left)
    return info["chasing_chance"], info["defending_chance"]


class WinProbabilityTests(unittest.TestCase):
    def test_batting_and_bowling_sum_to_100(self):
        for args in [(20, 2, 30), (40, 4, 12), (30, 6, 6), (1, 0, 60)]:
            bat, bowl = _win_prob(*args)
            self.assertEqual(bat + bowl, 100, args)

    def test_easier_chase_is_more_batting_favoured(self):
        easy, _ = _win_prob(20, 2, 30)
        tight, _ = _win_prob(40, 4, 12)
        hard, _ = _win_prob(30, 6, 6)
        self.assertGreater(easy, tight)
        self.assertGreater(tight, hard)

    def test_a_live_chase_stays_inside_the_spec_caps(self):
        """While the chase is still possible the bar never reads as certainty in
        either direction — section 9 Step 4's 95/3 caps."""
        for args in [(20, 2, 30), (40, 4, 12), (60, 8, 40), (1, 0, 60)]:
            bat, bowl = _win_prob(*args)
            self.assertGreaterEqual(bat, cc.LOWER_CAP, args)
            self.assertLessEqual(bat, cc.UPPER_CAP, args)
            self.assertEqual(bat + bowl, 100, args)

    def test_an_eliminated_chase_reads_zero(self):
        """200 off 3 is not a slim chance, it is arithmetic. The lower cap is for
        chases that are merely hopeless; this one is over."""
        bat, bowl = _win_prob(200, 9, 3)
        self.assertEqual(bat, 0)
        self.assertEqual(bowl, 100)


if __name__ == "__main__":
    unittest.main()

"""Tests for the Death Over Chase Momentum System (services/death_over_chase.py).

Pure match-state logic — no DB or telegram deps — so these run in any env.
"""

import random
import unittest

from services import death_over_chase as d


def _snap(**over):
    base = {
        "innings": 2, "ballsLeft": 6, "runsNeeded": 15, "wicketsLeft": 5,
        "teamScore": 210, "partnershipRuns": 52,
        "striker": {"category": "Batsman", "balls": 18, "batRating": 87},
        "nonStriker": {"category": "All-rounder", "balls": 14, "batRating": 84},
    }
    base.update(over)
    return base


class ScenarioDetectionTests(unittest.TestCase):
    def test_last_over_smart_chase_with_set_pair_boost(self):
        # Spec section 31 worked example: 80% base + 15% set-pair boost = 95%.
        sc = d.detect_scenario(_snap())
        self.assertEqual(sc["scenario"], "Last Over Smart Chase")
        self.assertEqual(sc["chaseSuccessChance"], 95)

    def test_both_bowlers_falls_through_to_normal_engine(self):
        sc = d.detect_scenario(_snap(
            striker={"category": "Bowler", "balls": 2, "batRating": 30},
            nonStriker={"category": "Bowler", "balls": 2, "batRating": 30}))
        self.assertTrue(sc["useNormalEngine"])
        self.assertIsNone(sc["chaseSuccessChance"])

    def test_weak_batter_on_strike_is_capped_and_not_boosted(self):
        # A tail-ender on strike must stay at 50% even though the set non-striker
        # would otherwise trigger the +40 "200+ protection" boost.
        sc = d.detect_scenario(_snap(
            runsNeeded=12,
            striker={"category": "Bowler", "balls": 2, "batRating": 40}))
        self.assertEqual(sc["scenario"], "Weak Batter On Strike Rule")
        self.assertEqual(sc["chaseSuccessChance"], 50)

    def test_very_easy_takes_priority_over_smart(self):
        # Base scenario selection (pre-boost) must pick Very Easy at 90%.
        base = d.get_base_chase_scenario(_snap(runsNeeded=5))
        self.assertEqual(base["scenario"], "Very Easy Last Over Chase")
        self.assertEqual(base["chaseSuccessChance"], 90)
        # After boosts (high-score momentum +20, capped) it climbs to 95%.
        sc = d.detect_scenario(_snap(runsNeeded=5))
        self.assertEqual(sc["scenario"], "Very Easy Last Over Chase")
        self.assertEqual(sc["chaseSuccessChance"], 95)

    def test_normal_engine_outside_death_overs(self):
        sc = d.detect_scenario(_snap(ballsLeft=60, runsNeeded=80))
        self.assertTrue(sc["useNormalEngine"])

    def test_first_innings_never_active(self):
        sc = d.detect_scenario(_snap(innings=1))
        self.assertTrue(sc["useNormalEngine"])


class DecisionTests(unittest.TestCase):
    def test_normal_engine_decision_passthrough(self):
        dec = d.decide_chase_result({"useNormalEngine": True})
        self.assertTrue(dec["useNormalEngine"])
        self.assertIsNone(dec["chaseWillSucceed"])

    def test_decision_respects_chance_distribution(self):
        sc = {"active": True, "scenario": "X", "chaseSuccessChance": 80}
        rng = random.Random(0)
        wins = sum(d.decide_chase_result(sc, rng=rng)["chaseWillSucceed"]
                   for _ in range(4000))
        self.assertAlmostEqual(wins / 4000, 0.80, delta=0.03)


class BiasHookTests(unittest.TestCase):
    RAW = {"Dot": 0.25, "Single": 0.30, "Double": 0.12, "Three": 0.01,
           "Four": 0.15, "Six": 0.08, "Wicket": 0.07, "Extras": 0.02}

    def _norm(self, w):
        t = sum(w.values())
        return {k: v / t for k, v in w.items()}

    def test_success_bias_lifts_boundaries_cuts_wickets(self):
        hook = d.make_bias_hook(
            {"useNormalEngine": False, "chaseWillSucceed": True},
            lambda: _snap())
        out = self._norm(hook(self.RAW))
        base = self._norm(self.RAW)
        self.assertGreater(out["Four"] + out["Six"], base["Four"] + base["Six"])
        self.assertLess(out["Wicket"], base["Wicket"])
        self.assertLess(out["Dot"], base["Dot"])

    def test_failure_bias_raises_wickets_and_dots(self):
        hook = d.make_bias_hook(
            {"useNormalEngine": False, "chaseWillSucceed": False},
            lambda: _snap())
        out = self._norm(hook(self.RAW))
        base = self._norm(self.RAW)
        self.assertGreater(out["Wicket"], base["Wicket"])
        self.assertLess(out["Six"], base["Six"])

    def test_no_hook_for_normal_engine(self):
        self.assertIsNone(d.make_bias_hook({"useNormalEngine": True}, lambda: None))

    def test_weights_never_negative(self):
        hook = d.make_bias_hook(
            {"useNormalEngine": False, "chaseWillSucceed": False},
            lambda: _snap(runsNeeded=2, ballsLeft=3))
        out = hook(self.RAW)
        self.assertTrue(all(v >= 0 for v in out.values()))


if __name__ == "__main__":
    unittest.main()

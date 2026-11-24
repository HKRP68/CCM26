"""Rare volatility + highlight detection (Codex Prompt 05, §10 & §14)."""

import unittest

from engine.drama import events
from engine.sim.rng import SimRng


class RareVolatilityTests(unittest.TestCase):

    def test_activation_is_rare(self):
        rng = SimRng(5)
        fires = sum(events.rare_activation(rng, 0.015) for _ in range(10000))
        self.assertTrue(80 < fires < 220)               # ~1.5%, bounded

    def test_rare_type_symmetric(self):
        rng = SimRng(2)
        n = 4000
        ca = sum(events.choose_rare_type(rng) == events.RARE_COUNTERATTACK
                 for _ in range(n))
        self.assertTrue(0.45 < ca / n < 0.55)

    def test_rare_modifiers_are_small_tradeoffs(self):
        ca = events.rare_log_deltas(events.RARE_COUNTERATTACK)
        self.assertGreater(ca["Six"], 0.0)
        self.assertGreater(ca["Wicket"], 0.0)           # risk pays for reward
        surge = events.rare_log_deltas(events.RARE_BOWLING_SURGE)
        self.assertGreater(surge["Wicket"], 0.0)
        self.assertLess(surge["Six"], 0.0)


class HighlightDetectionTests(unittest.TestCase):

    def test_explosive_over_threshold(self):
        self.assertIsNone(events.explosive_over(1, 19, match_id="m", innings=1,
                                                legal_ball_index=6))
        self.assertIsNotNone(events.explosive_over(1, 20, match_id="m", innings=1,
                                                   legal_ball_index=6))

    def test_collapse_and_hat_trick(self):
        self.assertIsNone(events.major_collapse(2, match_id="m", innings=1,
                                                legal_ball_index=30))
        self.assertIsNotNone(events.major_collapse(3, match_id="m", innings=1,
                                                   legal_ball_index=30))
        self.assertIsNone(events.hat_trick(2, match_id="m", innings=1,
                                           legal_ball_index=30))
        self.assertIsNotNone(events.hat_trick(3, bowler_ref="B", match_id="m",
                                              innings=1, legal_ball_index=30))

    def test_milestone_under_pressure_flag(self):
        tense = events.milestone(49, 51, high_pressure=True, player="S",
                                 match_id="m", innings=1, legal_ball_index=40)
        calm = events.milestone(49, 51, high_pressure=False, player="S",
                                match_id="m", innings=1, legal_ball_index=40)
        self.assertEqual(tense["event_type"], "fifty_under_pressure")
        self.assertGreater(tense["severity"], calm["severity"])
        self.assertIsNone(events.milestone(40, 49, player="S", match_id="m",
                                           innings=1, legal_ball_index=40))

    def test_event_ids_are_stable_and_idempotent(self):
        a = events.build_event("explosive_over", "m1", 2, 66, dedup_key="over11")
        b = events.build_event("explosive_over", "m1", 2, 66, dedup_key="over11")
        self.assertEqual(a["event_id"], b["event_id"])  # same inputs => same id
        c = events.build_event("explosive_over", "m1", 2, 72, dedup_key="over12")
        self.assertNotEqual(a["event_id"], c["event_id"])

    def test_event_structure_fields(self):
        e = events.build_event("hat_trick", "m1", 1, 18, team="Bowlers",
                               player="B", tension_index=77)
        for field in ("event_id", "event_type", "match_id", "innings",
                      "legal_ball_index", "team_reference",
                      "relevant_player_reference", "severity", "tension_index",
                      "description_key"):
            self.assertIn(field, e)
        self.assertEqual(e["tension_index"], 77)


if __name__ == "__main__":
    unittest.main()

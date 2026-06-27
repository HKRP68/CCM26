"""Tests for sequence-aware narratives in engine.commentary_engine.

The new back-to-back / post-wicket / dot-streak narratives must fire only when
the caller supplies the relevant fields, and never for older callers that omit
them (backward compatibility).
"""

import unittest

from engine.commentary_engine import CommentaryEngine


class CommentarySequenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = CommentaryEngine()
        cls.base_state = {
            "current_over": 5, "current_ball": 2, "innings": 1,
            "batter_runs": 10, "partnership_runs": 20,
        }

    def _state(self, **extra):
        s = dict(self.base_state)
        s.update(extra)
        return s

    def _has_any(self, text, key):
        # A narrative is present if any of its templates' distinctive words show.
        markers = {
            "back_to_back_boundary": ["Back-to-back", "Two in two", "consecutive boundaries"],
            "responded_after_wicket": ["What a response", "Straight back at them", "replies in style"],
            "dot_pressure_streak": ["Dot after dot", "pressure cooker", "Three on the bounce"],
        }[key]
        return any(m in text for m in markers)

    def test_back_to_back_boundary_fires(self):
        ctx = {"type": "run", "runs": 4, "batter": "Kohli", "bowler": "Starc",
               "batting_team": "IND", "bowling_team": "AUS"}
        text = self.engine.get_commentary(ctx, self._state(last_ball_boundary=True))
        self.assertTrue(self._has_any(text, "back_to_back_boundary"))

    def test_responded_after_wicket_fires(self):
        ctx = {"type": "run", "runs": 6, "batter": "Kohli", "bowler": "Starc",
               "batting_team": "IND", "bowling_team": "AUS"}
        text = self.engine.get_commentary(ctx, self._state(last_ball_wicket=True))
        self.assertTrue(self._has_any(text, "responded_after_wicket"))

    def test_dot_streak_fires_on_three_in_a_row(self):
        ctx = {"type": "run", "runs": 0, "batter": "Kohli", "bowler": "Starc",
               "batting_team": "IND", "bowling_team": "AUS"}
        text = self.engine.get_commentary(ctx, self._state(consecutive_dots=2))
        self.assertTrue(self._has_any(text, "dot_pressure_streak"))

    def test_dot_streak_does_not_fire_too_early(self):
        ctx = {"type": "run", "runs": 0, "batter": "Kohli", "bowler": "Starc",
               "batting_team": "IND", "bowling_team": "AUS"}
        text = self.engine.get_commentary(ctx, self._state(consecutive_dots=1))
        self.assertFalse(self._has_any(text, "dot_pressure_streak"))

    def test_no_sequence_narrative_without_fields(self):
        # Older callers that omit the new fields get no sequence narrative.
        ctx = {"type": "run", "runs": 4, "batter": "Kohli", "bowler": "Starc",
               "batting_team": "IND", "bowling_team": "AUS"}
        text = self.engine.get_commentary(ctx, self._state())
        self.assertFalse(self._has_any(text, "back_to_back_boundary"))
        self.assertFalse(self._has_any(text, "responded_after_wicket"))

    def test_extra_does_not_trigger_boundary_narrative(self):
        ctx = {"type": "run", "runs": 4, "is_extra": True, "batter": "Kohli",
               "bowler": "Starc", "batting_team": "IND", "bowling_team": "AUS"}
        text = self.engine.get_commentary(ctx, self._state(last_ball_boundary=True))
        self.assertFalse(self._has_any(text, "back_to_back_boundary"))


if __name__ == "__main__":
    unittest.main()

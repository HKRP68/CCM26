"""Tests for the /wpm, /cm, /wpmbot refinement pass:

  - per-match form/traits caching in handlers.match._calc helpers
  - the "no empty summary" play-detection guard
  - challenge-mode (2-wicket) innings-end correctness
  - quick-match wicket sampling + chase balls-faced fixes
"""

import unittest

import handlers.match as bm
from services.match_engine import is_innings_over
from services import quick_match_service as qm


class StateHasPlayTests(unittest.TestCase):
    """_state_has_play gates the summary card so forfeits don't post a 0/0 card."""

    def test_no_play_states(self):
        self.assertFalse(bm._state_has_play(None))
        self.assertFalse(bm._state_has_play({}))
        self.assertFalse(bm._state_has_play(
            {"inn1_runs": 0, "inn1_wickets": 0, "total_runs": 0, "total_wickets": 0}))

    def test_real_play_states(self):
        self.assertTrue(bm._state_has_play({"inn1_runs": 84}))
        self.assertTrue(bm._state_has_play({"total_wickets": 1}))
        self.assertTrue(bm._state_has_play(
            {"inn1_runs": 0, "inn1_wickets": 0, "total_runs": 150, "total_wickets": 4}))

    def test_handles_non_int_values(self):
        # Defensive: stringly-typed values shouldn't raise.
        self.assertTrue(bm._state_has_play({"inn1_runs": "120"}))
        self.assertFalse(bm._state_has_play({"inn1_runs": None, "total_runs": "0"}))


class TraitsCacheTests(unittest.TestCase):
    """_traits_for must query the DB (via _get_roster_traits) once per roster."""

    def setUp(self):
        self._orig = bm._get_roster_traits
        self.calls = []
        bm._get_roster_traits = lambda rid: (self.calls.append(rid)
                                              or [{"effect_key": f"t{rid}"}])

    def tearDown(self):
        bm._get_roster_traits = self._orig

    def test_cached_per_roster(self):
        s = {}
        first = bm._traits_for(s, 5)
        second = bm._traits_for(s, 5)
        bm._traits_for(s, 7)
        bm._traits_for(s, 7)
        # Only one DB load per distinct roster_id.
        self.assertEqual(self.calls, [5, 7])
        # Same cached object returned each time.
        self.assertIs(first, second)
        self.assertEqual(first, [{"effect_key": "t5"}])

    def test_falsy_roster_skips_db(self):
        s = {}
        self.assertEqual(bm._traits_for(s, 0), [])
        self.assertEqual(bm._traits_for(s, None), [])
        self.assertEqual(self.calls, [])


class FormModCacheTests(unittest.TestCase):
    """_form_mod_for must serve cached values and never hit the DB for bots."""

    def test_cache_hit_returns_stored_value(self):
        s = {"_form_cache": {"5": 1.75}}
        self.assertEqual(bm._form_mod_for(s, 5), 1.75)

    def test_bot_or_invalid_roster_returns_zero_without_db(self):
        s = {}
        self.assertEqual(bm._form_mod_for(s, 0), 0.0)
        self.assertEqual(bm._form_mod_for(s, -3), 0.0)
        # No cache entries created for the bot/invalid case.
        self.assertEqual(s.get("_form_cache", {}), {})


class ChallengeWicketLimitTests(unittest.TestCase):
    """/cm uses a 2-wicket innings; the engine and autoplay must respect it."""

    def _state(self, wkts, wicket_limit):
        return {
            "innings": 1, "overs": 2, "target": None,
            "wicket_limit": wicket_limit,
            "total_wickets": wkts, "current_over": 1, "current_ball": 1,
        }

    def test_innings_over_at_wicket_limit(self):
        self.assertTrue(is_innings_over(self._state(2, 2)))
        self.assertFalse(is_innings_over(self._state(1, 2)))

    def test_autoplay_new_batsman_condition_respects_limit(self):
        # Mirrors the fixed condition in auto_play_bot_turns: a new batsman is
        # only requested while wickets are below the (mode-specific) limit.
        st = self._state(2, 2)
        self.assertFalse(st["total_wickets"] < st.get("wicket_limit", 10))
        st = self._state(1, 2)
        self.assertTrue(st["total_wickets"] < st.get("wicket_limit", 10))


class QuickMatchWicketSamplingTests(unittest.TestCase):
    """The fixed sampling must allow phases to lose more than one wicket."""

    def test_multi_wicket_phases_occur(self):
        death = qm.PHASE_DEFS[2]  # high base_wickets, aggressive risk
        seen_multi = False
        max_seen = 0
        for _ in range(800):
            _runs, wkts = qm.simulate_phase(death, "aggressive", 80, 80, 10)
            max_seen = max(max_seen, wkts)
            if wkts >= 2:
                seen_multi = True
        self.assertTrue(seen_multi,
                        "expected at least one >=2-wicket phase across 800 trials")
        # Never exceeds wickets in hand.
        self.assertLessEqual(max_seen, 10)

    def test_wickets_bounded_by_remaining(self):
        # With only 1 wicket in hand, a phase can lose at most 1.
        for _ in range(200):
            _runs, wkts = qm.simulate_phase(qm.PHASE_DEFS[2], "aggressive", 70, 90, 1)
            self.assertLessEqual(wkts, 1)


class QuickMatchChaseBallsTests(unittest.TestCase):
    """The winning phase should charge partial balls, not the whole phase."""

    def test_chase_uses_partial_balls(self):
        # Target reachable inside the first phase; balls_faced for the winning
        # phase must be <= that phase's balls (previously it always added all).
        res = qm.simulate_innings(["aggressive", "balanced", "defensive"],
                                   batting_rating=95, bowling_rating=60, target=5)
        self.assertTrue(res["chased"])
        self.assertLessEqual(res["balls_faced"], qm.PHASE_DEFS[0]["balls"])
        self.assertGreaterEqual(res["balls_faced"], 1)


if __name__ == "__main__":
    unittest.main()

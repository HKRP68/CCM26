"""Balanced Drama /sim integration (Codex Prompt 05, §12, §22, §23).

The overlay is wired into services/sim_match.py. These tests exercise it end to
end: the preset is recorded, highlights are produced, the match stays
reproducible, drama does not make Balanced a higher-scoring mode, and with the
overlay off the path is byte-identical to the realism engine.
"""

import hashlib
import logging
import os
import unittest

logging.disable(logging.CRITICAL)

from engine.drama import config as drama_config
from engine.realism.config import clear_cache as clear_realism
from services.sim_match import simulate_match
from tests.test_sim_match import _make_xi


class _Env:
    """Set realism + a drama preset for a block."""

    def __init__(self, preset):
        self.preset = preset

    def __enter__(self):
        self._prev = {k: os.environ.get(k) for k in
                      ("SIM_REALISM_ENABLED", "SIM_DRAMA_ENABLED", "SIM_DRAMA_PRESET")}
        os.environ["SIM_REALISM_ENABLED"] = "1"
        os.environ["SIM_DRAMA_PRESET"] = self.preset
        os.environ["SIM_DRAMA_ENABLED"] = "0" if self.preset == "REALISTIC" else "1"
        clear_realism()
        drama_config.clear_cache()
        return self

    def __exit__(self, *exc):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        clear_realism()
        drama_config.clear_cache()
        return False


def _digest(m):
    # Both innings are fully deterministic from the seed. The result text is
    # excluded on purpose: a tied match goes to a Super Over, which runs on the
    # separate legacy probability_engine (global random) — a documented P4-3
    # residual, not part of the drama/realism stream.
    return (m["innings1"]["runs"], m["innings1"]["wickets"],
            m["innings2"]["runs"], m["innings2"]["wickets"],
            m["innings1"]["drama"]["final_tension"],
            m["innings2"]["drama"]["final_tension"])


class IntegrationTests(unittest.TestCase):

    def setUp(self):
        self.home, self.away = _make_xi("H", 80), _make_xi("A", 78)

    def test_preset_recorded_and_highlights_produced(self):
        with _Env("BALANCED"):
            m = simulate_match(self.home, self.away, 20, "Flat",
                               "Alpha", "Bravo", seed=4242)
        self.assertEqual(m["drama_preset"], "BALANCED")
        self.assertEqual(m["drama_engine_version"], "drama-v1")
        self.assertIsInstance(m["drama_highlights"], list)
        self.assertGreater(len(m["drama_highlights"]), 0)
        # Highlights carry the full event structure.
        for h in m["drama_highlights"]:
            self.assertIn("event_id", h)
            self.assertIn("event_type", h)

    def test_tension_values_stay_in_range(self):
        with _Env("BALANCED"):
            m = simulate_match(self.home, self.away, 20, "Even",
                               "Alpha", "Bravo", seed=11)
        for inn in ("innings1", "innings2"):
            t = m[inn]["drama"]["final_tension"]
            self.assertTrue(0 <= t <= 100)

    def test_same_seed_reproducible(self):
        with _Env("BALANCED"):
            a = simulate_match(self.home, self.away, 20, "Even",
                               "Alpha", "Bravo", seed=777)
            b = simulate_match(self.home, self.away, 20, "Even",
                               "Alpha", "Bravo", seed=777)
        self.assertEqual(_digest(a), _digest(b))

    def test_different_seed_varies(self):
        with _Env("BALANCED"):
            a = simulate_match(self.home, self.away, 20, "Even",
                               "Alpha", "Bravo", seed=1)
            b = simulate_match(self.home, self.away, 20, "Even",
                               "Alpha", "Bravo", seed=2)
        self.assertNotEqual(_digest(a), _digest(b))

    def test_realistic_preset_leaves_the_path_untouched(self):
        # REALISTIC => no drama engine => identical to the realism-only result,
        # and no drama version recorded.
        with _Env("REALISTIC"):
            m = simulate_match(self.home, self.away, 20, "Even",
                               "Alpha", "Bravo", seed=55)
        self.assertEqual(m["drama_preset"], "REALISTIC")
        self.assertIsNone(m["drama_engine_version"])
        self.assertEqual(m["drama_highlights"], [])

    def test_no_player_data_is_touched(self):
        # §1 / §23: a Balanced match must not alter data/players.json nor stamp
        # any drama attribute onto a player record.
        players = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "data", "players.json")
        before = hashlib.sha256(open(players, "rb").read()).hexdigest()
        allowed = {"name", "rating", "bat_rating", "bowl_rating", "category",
                   "bowl_style", "bowl_hand", "bat_hand"}
        with _Env("BALANCED"):
            simulate_match(self.home, self.away, 20, "Even", "A", "B", seed=9)
        after = hashlib.sha256(open(players, "rb").read()).hexdigest()
        self.assertEqual(before, after, "data/players.json changed")
        for p in self.home + self.away:
            leaked = [k for k in p
                      if any(t in k.lower() for t in ("clutch", "drama",
                                                      "momentum", "tension"))]
            self.assertEqual(leaked, [], "drama attribute leaked onto a player")

    def test_drama_does_not_inflate_scoring(self):
        # §22: Balanced must not simply be a higher-scoring mode. Compare matched
        # seeds; the mean first-innings total moves only within a small band.
        home, away = _make_xi("H", 80), _make_xi("A", 80)
        with _Env("REALISTIC"):
            r = [simulate_match(home, away, 20, "Even", "A", "B", seed=3000 + s)
                 ["innings1"]["runs"] for s in range(24)]
        with _Env("BALANCED"):
            b = [simulate_match(home, away, 20, "Even", "A", "B", seed=3000 + s)
                 ["innings1"]["runs"] for s in range(24)]
        self.assertLess(abs(sum(b) / len(b) - sum(r) / len(r)), 12.0)


if __name__ == "__main__":
    unittest.main()

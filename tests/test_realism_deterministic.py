"""Deterministic simulation (Prompt 04, Cricket Mechanics & Reliability fix 3).

A match runs off one seeded random stream when realism is enabled: the same
seed replays it identically, two concurrent matches never perturb each other
through the global ``random`` state, and the seed + engine version are kept as
metadata. With the flag off the engine is byte-identical to before — the seed
is ignored and draws come from the global module exactly as they always did.
"""

import logging
import os
import random
import unittest

logging.disable(logging.CRITICAL)

from engine import ball_outcome as bo
from engine.format_config import get_format
from engine.realism.config import clear_cache
from engine.sim.rng import SimRng, ENGINE_VERSION
from services.sim_match import simulate_match
from services import cipl_match
from tests.test_sim_match import _make_xi

T20 = get_format("T20")


class _Realism:
    """Toggle the realism flag for a block and clear the config cache."""

    def __init__(self, on=True):
        self.on = on

    def __enter__(self):
        self._prev = os.environ.get("SIM_REALISM_ENABLED")
        os.environ["SIM_REALISM_ENABLED"] = "1" if self.on else "0"
        clear_cache()
        return self

    def __exit__(self, *exc):
        if self._prev is None:
            os.environ.pop("SIM_REALISM_ENABLED", None)
        else:
            os.environ["SIM_REALISM_ENABLED"] = self._prev
        clear_cache()
        return False


def _digest(m):
    """A reproducibility fingerprint for a simulated match."""
    return (m["innings1"]["runs"], m["innings1"]["wickets"],
            m["innings2"]["runs"], m["innings2"]["wickets"],
            m["result"]["text"], m["toss"]["winner"],
            len(m["commentary_feed"]))


# ───────────────────────── SimRng facade ─────────────────────────

class SimRngFacadeTests(unittest.TestCase):
    """SimRng is a drop-in for the random-module subset the engine uses, and
    is reproducible from its seed across fresh instances (i.e. processes)."""

    def test_choices_is_reproducible_from_seed(self):
        a = SimRng(123).choices([1, 2, 3, 4], weights=[1, 2, 3, 4], k=20)
        b = SimRng(123).choices([1, 2, 3, 4], weights=[1, 2, 3, 4], k=20)
        self.assertEqual(a, b)

    def test_streams_independent_of_global_random(self):
        random.seed(0)
        a = [SimRng(7).random() for _ in range(5)]
        [random.random() for _ in range(1000)]          # churn global state
        random.seed(999)
        b = [SimRng(7).random() for _ in range(5)]
        self.assertEqual(a, b)

    def test_choice_and_random_present(self):
        r = SimRng(1)
        self.assertIn(r.choice([10, 20, 30]), (10, 20, 30))
        self.assertTrue(0.0 <= r.random() < 1.0)


# ───────────────────────── ball_outcome rng param ─────────────────────────

class BallOutcomeRngTests(unittest.TestCase):

    def _ball(self, **kw):
        args = dict(
            batter={"name": "B", "batting_rating": 80, "batting_hand": "Right"},
            bowler={"name": "W", "bowling_rating": 75, "fielding_rating": 70,
                    "bowling_hand": "Right", "bowling_type": "Fast-medium"},
            pitch="Even", streak={"boundaries": 0}, over_number=8,
            batter_runs=10, balls_faced=8, format_config=T20)
        args.update(kw)
        return bo.calculate_outcome(**args)

    def test_rng_none_uses_global_module_byte_identical(self):
        # The default (rng=None) must behave exactly like the historical engine:
        # draws come from the global module, so a fixed global seed repeats.
        with _Realism(on=False):
            random.seed(4242)
            first = self._ball()
            random.seed(4242)
            second = self._ball()
        self.assertEqual(first["type"], second["type"])
        self.assertEqual(first["runs"], second["runs"])
        self.assertEqual(first.get("wicket_type"), second.get("wicket_type"))

    def test_explicit_rng_drives_outcome_under_realism(self):
        # Under realism the funnel is the sole source of per-delivery
        # randomness (the legacy pressure-cluster draw is bypassed), so the
        # same rng seed reproduces the delivery whatever the global state is.
        # (The legacy rng=None path keeps using the global module — that is
        # covered by the byte-identity test above and must stay that way.)
        with _Realism(on=True):
            random.seed(1)
            a = self._ball(rng=SimRng(555))
            random.seed(2)                               # different global state
            b = self._ball(rng=SimRng(555))
        self.assertEqual((a["type"], a["runs"], a.get("wicket_type")),
                         (b["type"], b["runs"], b.get("wicket_type")))


# ───────────────────────── sim_match determinism ─────────────────────────

class SimMatchDeterminismTests(unittest.TestCase):

    def setUp(self):
        self.home, self.away = _make_xi("H", 80), _make_xi("A", 78)

    def test_same_seed_replays_identically(self):
        with _Realism(on=True):
            m1 = simulate_match(self.home, self.away, 20, "Flat",
                                "Alpha", "Bravo", seed=12345)
            m2 = simulate_match(self.home, self.away, 20, "Flat",
                                "Alpha", "Bravo", seed=12345)
        self.assertEqual(_digest(m1), _digest(m2))

    def test_different_seed_diverges(self):
        with _Realism(on=True):
            m1 = simulate_match(self.home, self.away, 20, "Flat",
                                "Alpha", "Bravo", seed=1)
            m2 = simulate_match(self.home, self.away, 20, "Flat",
                                "Alpha", "Bravo", seed=2)
        self.assertNotEqual(_digest(m1), _digest(m2))

    def test_isolated_from_concurrent_global_random(self):
        # A second match churning the global RNG between two seeded runs must
        # not change the seeded match's result.
        with _Realism(on=True):
            random.seed(1)
            a = simulate_match(self.home, self.away, 20, "Flat",
                               "Alpha", "Bravo", seed=777)
            [random.random() for _ in range(5000)]
            b = simulate_match(self.home, self.away, 20, "Flat",
                               "Alpha", "Bravo", seed=777)
        self.assertEqual(_digest(a), _digest(b))

    def test_auto_seed_is_recorded_and_replayable(self):
        with _Realism(on=True):
            auto = simulate_match(self.home, self.away, 20, "Flat",
                                  "Alpha", "Bravo")
            self.assertIsInstance(auto["rng_seed"], int)
            self.assertEqual(auto["engine_version"], ENGINE_VERSION)
            replay = simulate_match(self.home, self.away, 20, "Flat",
                                    "Alpha", "Bravo", seed=auto["rng_seed"])
        self.assertEqual(_digest(auto), _digest(replay))


# ───────────────────────── legacy byte-identity ─────────────────────────

class SimMatchLegacyIdentityTests(unittest.TestCase):

    def setUp(self):
        self.home, self.away = _make_xi("H", 80), _make_xi("A", 78)

    def test_seed_is_ignored_when_flag_off(self):
        # With realism off, the seed must have no effect — the engine runs off
        # the global module exactly as it historically did.
        with _Realism(on=False):
            random.seed(31)
            no_seed = simulate_match(self.home, self.away, 20, "Flat",
                                     "Alpha", "Bravo")
            random.seed(31)
            with_seed = simulate_match(self.home, self.away, 20, "Flat",
                                       "Alpha", "Bravo", seed=99999)
        self.assertEqual(_digest(no_seed), _digest(with_seed))

    def test_metadata_is_null_when_flag_off(self):
        with _Realism(on=False):
            m = simulate_match(self.home, self.away, 20, "Flat",
                               "Alpha", "Bravo", seed=5)
        self.assertIsNone(m["rng_seed"])
        self.assertIsNone(m["engine_version"])


# ───────────────────────── cipl seed metadata ─────────────────────────

class CiplSeedMetadataTests(unittest.TestCase):

    def _state(self):
        bat = [dict(p, roster_id=f"b{i}") for i, p in enumerate(_make_xi("B", 80))]
        bowl = [dict(p, roster_id=f"w{i}") for i, p in enumerate(_make_xi("W", 78))]
        return cipl_match.build_cipl_state(
            match_id="m1", overs=20, bat_user_id=1, bowl_user_id=2,
            bat_user_tg="u1", bowl_user_tg="u2", bat_xi=bat, bowl_xi=bowl,
            bat_team_name="Bats", bowl_team_name="Bowls", chat_id=10)

    def test_build_state_carries_seed_and_version(self):
        st = self._state()
        self.assertIsInstance(st["rng_seed"], int)
        self.assertEqual(st["engine_version"], ENGINE_VERSION)

    def test_state_is_json_serialisable(self):
        import json
        st = self._state()
        # rng metadata must survive the serialise/deserialise the store does.
        round_trip = json.loads(json.dumps(
            {"rng_seed": st["rng_seed"], "engine_version": st["engine_version"]}))
        self.assertEqual(round_trip["rng_seed"], st["rng_seed"])


if __name__ == "__main__":
    unittest.main()

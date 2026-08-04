"""Tests for Lucky Card reward probabilities (the wheel behind /gspin).

Weights are entered on the website as percentages and can be fractional down
to 0.00001, so the picker must never floor or round them, and a reward parked
at 0 must never land.
"""

import sys
import types
import unittest


def _install_stubs():
    sa = types.ModuleType("sqlalchemy")
    sa.and_ = lambda *a, **k: None
    sa.or_ = lambda *a, **k: None
    sys.modules["sqlalchemy"] = sa
    sa_orm = types.ModuleType("sqlalchemy.orm")
    sa_orm.Session = object
    sys.modules["sqlalchemy.orm"] = sa_orm

    models = types.ModuleType("models")
    models.Player = type("Player", (), {})
    models.GSpinReward = type("GSpinReward", (), {"enabled": True, "sort_order": 0, "id": 0})
    sys.modules["models"] = models

    config = types.ModuleType("config")
    config.CLAIM_RARITY = [(1.0, 50, 100)]
    config.get_buy_value = config.get_sell_value = lambda r: 0
    config.GSPIN_OUTCOMES = [(0.58, "red", "coins", (1500, 3000))]
    sys.modules["config"] = config

    sys.modules.pop("services.gspin_reward_service", None)
    from services import gspin_reward_service
    return gspin_reward_service


class Reward:
    def __init__(self, label, weight, enabled=True):
        self.label = label
        self.weight = weight
        self.enabled = enabled
        self.sort_order = 0
        self.id = 0


class _Query:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def all(self):
        return [r for r in self._rows if r.enabled]


class FakeSession:
    def __init__(self, rewards):
        self._rewards = list(rewards)

    def query(self, *a, **k):
        return _Query(self._rewards)


_STUBBED = ("sqlalchemy", "sqlalchemy.orm", "models", "config",
            "services.gspin_reward_service")


class LuckyCardProbabilityTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: sys.modules.get(k) for k in _STUBBED}
        self.svc = _install_stubs()

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value

    def test_fractional_weights_are_not_floored(self):
        # The old picker floored every weight to at least 1, which turned a
        # 0.5% reward into a 33% one when the others were 1 apiece.
        rewards = [Reward("Common", 99.5), Reward("Rare", 0.5)]
        self.assertAlmostEqual(
            self.svc.reward_probability(rewards[1], rewards), 0.005, places=9)

    def test_tiny_weight_keeps_its_precision(self):
        rewards = [Reward("Common", 99.99999), Reward("Chase", 0.00001)]
        self.assertAlmostEqual(
            self.svc.reward_probability(rewards[1], rewards), 1e-7, places=12)

    def test_zero_weight_never_lands(self):
        rewards = [Reward("Common", 100.0), Reward("Parked", 0.0)]
        session = FakeSession(rewards)
        picks = {self.svc.pick_reward(session).label for _ in range(500)}
        self.assertEqual(picks, {"Common"})
        self.assertEqual(self.svc.reward_probability(rewards[1], rewards), 0.0)

    def test_all_parked_falls_back_rather_than_picking_one(self):
        # Every enabled reward at 0 means nothing can land — the caller has to
        # get None so it can use the legacy outcomes, not a reward at random.
        session = FakeSession([Reward("A", 0.0), Reward("B", 0.0)])
        self.assertIsNone(self.svc.pick_reward(session))

    def test_disabled_rewards_are_excluded_from_probability(self):
        rewards = [Reward("On", 50.0), Reward("Off", 50.0, enabled=False)]
        self.assertAlmostEqual(
            self.svc.reward_probability(rewards[0], rewards), 1.0, places=9)

    def test_weights_are_relative_not_absolute(self):
        # A column that sums to 1000 splits exactly like one that sums to 100.
        rewards = [Reward("A", 580.0), Reward("B", 420.0)]
        self.assertAlmostEqual(
            self.svc.reward_probability(rewards[0], rewards), 0.58, places=9)

    def test_draw_matches_configured_split(self):
        import random
        # Seeding is process-wide; put the generator back so a later test
        # doesn't silently inherit this sequence.
        self.addCleanup(random.setstate, random.getstate())
        random.seed(7)
        rewards = [Reward("Common", 90.0), Reward("Rare", 10.0)]
        session = FakeSession(rewards)
        picks = [self.svc.pick_reward(session).label for _ in range(4000)]
        rare = picks.count("Rare") / len(picks)
        self.assertAlmostEqual(rare, 0.10, delta=0.02)

    def test_bad_weight_value_is_treated_as_zero(self):
        rewards = [Reward("Good", 100.0), Reward("Broken", None)]
        self.assertEqual(self.svc.reward_probability(rewards[1], rewards), 0.0)
        self.assertAlmostEqual(
            self.svc.reward_probability(rewards[0], rewards), 1.0, places=9)

    def test_no_rewards_returns_none(self):
        self.assertIsNone(self.svc.pick_reward(FakeSession([])))

    def test_non_finite_weights_are_treated_as_zero(self):
        # Only reachable by writing straight to the database — the form clamps
        # to 0-100 — but an infinite total makes every `roll < cumulative`
        # comparison false and the picker falls out to the last row.
        for bad in (float("inf"), float("-inf"), float("nan")):
            rewards = [Reward("Good", 100.0), Reward("Broken", bad)]
            self.assertEqual(self.svc.reward_probability(rewards[1], rewards), 0.0,
                             f"{bad} was not neutralised")
            self.assertAlmostEqual(
                self.svc.reward_probability(rewards[0], rewards), 1.0, places=9)
            session = FakeSession(rewards)
            picks = {self.svc.pick_reward(session).label for _ in range(200)}
            self.assertEqual(picks, {"Good"})


if __name__ == "__main__":
    unittest.main()

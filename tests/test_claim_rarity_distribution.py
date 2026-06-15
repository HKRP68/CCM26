"""Tests for the website Claim Rarity distribution used by /claim and /daily.

These verify that get_random_player_by_rarity honours the admin-configured
rarity bands strictly: an empty band must never leak picks into arbitrary
ratings — it re-rolls among the other configured bands instead.
"""

import sys
import types
import unittest
from types import SimpleNamespace


def _load_player_service():
    # Stub sqlalchemy (not installed in the test env).
    sa = types.ModuleType("sqlalchemy")
    sa.and_ = lambda *a, **k: None
    sys.modules["sqlalchemy"] = sa
    sa_orm = types.ModuleType("sqlalchemy.orm")
    sa_orm.Session = object
    sys.modules["sqlalchemy.orm"] = sa_orm

    # Stub models with the two classes player_service references.
    models = types.ModuleType("models")
    models.Player = type("Player", (), {})
    # Class-level attrs so `ClaimRarityTier.is_active == True` (etc.) in the
    # query filters don't raise — the FakeSession ignores the filter args.
    models.ClaimRarityTier = type(
        "ClaimRarityTier", (), {"is_active": True, "sort_order": 0, "id": 0})
    sys.modules["models"] = models

    # Stub config — CLAIM_RARITY is only the fallback when no tiers exist.
    config = types.ModuleType("config")
    config.CLAIM_RARITY = [(1.0, 50, 100)]
    config.get_buy_value = lambda r: r * 100
    config.get_sell_value = lambda r: r * 50
    sys.modules["config"] = config

    sys.modules.pop("services.player_service", None)
    from services import player_service
    return player_service


class Tier:
    def __init__(self, probability, rating_min, rating_max, sort_order=0, tid=0):
        self.probability = probability
        self.rating_min = rating_min
        self.rating_max = rating_max
        self.is_active = True
        self.sort_order = sort_order
        self.id = tid


class _TierQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def all(self):
        return self._rows


class FakeSession:
    """Minimal session: serves ClaimRarityTier rows and Player.get-by-id."""

    def __init__(self, tiers):
        self._tiers = tiers

    def query(self, *a, **k):
        return _TierQuery(self._tiers)

    def get(self, _model, pk):
        return SimpleNamespace(id=pk, rating=None)


_STUBBED_MODULES = (
    "sqlalchemy", "sqlalchemy.orm", "models", "config", "services.player_service",
)


class ClaimRarityDistributionTests(unittest.TestCase):
    def setUp(self):
        # Snapshot any real modules we're about to stub so discovery runs of
        # other test files aren't affected by our stubs.
        self._saved = {k: sys.modules.get(k) for k in _STUBBED_MODULES}
        self.ps = _load_player_service()

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value

    def _patch_cache(self, pools):
        """pools: {rating: [player dicts]}."""
        from services import player_cache
        player_cache.get_by_rating = lambda r: list(pools.get(r, []))

    def test_empty_band_rerolls_within_configured_bands(self):
        # Two bands. The heavily weighted band (90-94) has NO players; the
        # other (50-59) is full. Every pull must still return a 50-59 player.
        tiers = [Tier(99.0, 90, 94, 1, 1), Tier(1.0, 50, 59, 2, 2)]
        session = FakeSession(tiers)
        self._patch_cache({55: [{"id": 555}], 56: [{"id": 556}]})

        results = [self.ps.get_random_player_by_rarity(session) for _ in range(200)]
        self.assertTrue(all(r is not None for r in results))
        self.assertTrue(all(r.id in (555, 556) for r in results),
                        "empty band leaked outside configured bands")

    def test_weighting_respects_configured_probabilities(self):
        # Both bands populated; the 50-59 band has 90% weight so it should
        # dominate the draw.
        tiers = [Tier(90.0, 50, 59, 1, 1), Tier(10.0, 90, 94, 2, 2)]
        session = FakeSession(tiers)
        self._patch_cache({55: [{"id": 1}], 92: [{"id": 2}]})

        import random
        random.seed(42)
        picks = [self.ps.get_random_player_by_rarity(session).id for _ in range(2000)]
        low_band = sum(1 for p in picks if p == 1)
        self.assertGreater(low_band, len(picks) * 0.8,
                           "configured 90% band did not dominate")

    def test_no_players_anywhere_returns_none_gracefully(self):
        tiers = [Tier(100.0, 50, 59, 1, 1)]
        session = FakeSession(tiers)
        self._patch_cache({})  # nothing cached
        # Last-resort widening path falls back to range range, which also finds
        # nothing here, so it should return None rather than raise.
        from services import player_cache
        player_cache.get_random_in_rating_range = lambda low, high: None
        self.assertIsNone(self.ps.get_random_player_by_rarity(session))


if __name__ == "__main__":
    unittest.main()

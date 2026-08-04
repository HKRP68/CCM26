"""Tests for blocked rating ranges — the website's "this rating never drops" rule.

Two things have to hold. The block service must read the rules correctly and
per source, and the /claim draw must respect them: a blocked band behaves like
an empty one (re-roll into another configured band) and no widening or fallback
path may sneak a blocked rating back in.
"""

import sys
import types
import unittest
from types import SimpleNamespace


def _install_stubs():
    """Stub sqlalchemy + models so the services import without a database."""
    sa = types.ModuleType("sqlalchemy")
    sa.and_ = lambda *a, **k: None
    sa.or_ = lambda *a, **k: None
    sys.modules["sqlalchemy"] = sa
    sa_orm = types.ModuleType("sqlalchemy.orm")
    sa_orm.Session = object
    sys.modules["sqlalchemy.orm"] = sa_orm

    models = types.ModuleType("models")
    models.Player = type("Player", (), {})
    models.ClaimRarityTier = type(
        "ClaimRarityTier", (), {"is_active": True, "sort_order": 0, "id": 0})
    models.RatingBlockRule = type("RatingBlockRule", (), {"is_active": True})
    sys.modules["models"] = models

    config = types.ModuleType("config")
    config.CLAIM_RARITY = [(1.0, 50, 100)]
    config.get_buy_value = lambda r: r * 100
    config.get_sell_value = lambda r: r * 50
    sys.modules["config"] = config

    sys.modules.pop("services.player_service", None)
    from services import player_service, rating_block_service
    return player_service, rating_block_service


class Tier:
    def __init__(self, probability, rating_min, rating_max, sort_order=0, tid=0):
        self.probability = probability
        self.rating_min = rating_min
        self.rating_max = rating_max
        self.is_active = True
        self.sort_order = sort_order
        self.id = tid


class Block:
    """A RatingBlockRule stand-in. Sources default to blocked everywhere."""

    def __init__(self, rating_min, rating_max, claim=True, drop=True, gspin=True):
        self.rating_min = rating_min
        self.rating_max = rating_max
        self.block_claim = claim
        self.block_drop = drop
        self.block_gspin = gspin
        self.is_active = True


class _Query:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def all(self):
        return self._rows


class FakeSession:
    def __init__(self, tiers=(), blocks=()):
        self._tiers = list(tiers)
        self._blocks = list(blocks)

    def query(self, model=None, *a, **k):
        import models as _models
        if model is _models.RatingBlockRule:
            # The real query filters on is_active; do the same here so a
            # disabled rule is invisible to the service, as in production.
            return _Query([b for b in self._blocks if b.is_active])
        return _Query([t for t in self._tiers if t.is_active])

    def get(self, _model, pk):
        return SimpleNamespace(id=pk, rating=None)


_STUBBED_MODULES = (
    "sqlalchemy", "sqlalchemy.orm", "models", "config", "services.player_service",
)


class RatingBlockTestCase(unittest.TestCase):
    def setUp(self):
        self._saved = {k: sys.modules.get(k) for k in _STUBBED_MODULES}
        self.ps, self.blocks = _install_stubs()
        self.blocks.invalidate()

    def tearDown(self):
        self.blocks.invalidate()
        for key, value in self._saved.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value

    def _patch_cache(self, pools):
        """pools: {rating: [player dicts]}.

        Stubs the all-active accessor too, so the widening fallback is served
        from these pools instead of reaching for a real database.
        """
        from services import player_cache
        player_cache.get_by_rating = lambda r: list(pools.get(r, []))
        # The real cache dicts carry their rating; fill it in from the pool key
        # so the fallback's block filter sees the same shape it does live.
        everything = [{"rating": rating, **player}
                      for rating, players in pools.items() for player in players]
        player_cache.get_all_active = lambda: list(everything)


class BlockServiceTests(RatingBlockTestCase):
    def test_rule_applies_only_to_its_selected_sources(self):
        session = FakeSession(blocks=[Block(95, 100, claim=True, drop=False, gspin=True)])
        self.assertEqual(self.blocks.blocked_ratings(session, "claim"),
                         frozenset(range(95, 101)))
        self.assertEqual(self.blocks.blocked_ratings(session, "gspin"),
                         frozenset(range(95, 101)))
        self.assertEqual(self.blocks.blocked_ratings(session, "drop"), frozenset())

    def test_inactive_rules_block_nothing(self):
        rule = Block(90, 100)
        rule.is_active = False
        session = FakeSession(blocks=[rule])
        self.assertEqual(self.blocks.blocked_ratings(session, "claim"), frozenset())

    def test_reversed_range_is_normalised(self):
        session = FakeSession(blocks=[Block(100, 95)])
        self.assertEqual(self.blocks.blocked_ratings(session, "claim"),
                         frozenset(range(95, 101)))

    def test_overlapping_rules_union(self):
        session = FakeSession(blocks=[Block(90, 94), Block(93, 96)])
        self.assertEqual(self.blocks.blocked_ratings(session, "claim"),
                         frozenset(range(90, 97)))

    def test_allowed_ratings_skips_blocked(self):
        session = FakeSession(blocks=[Block(85, 89)])
        self.assertEqual(self.blocks.allowed_ratings(session, 83, 91, "claim"),
                         [83, 84, 90, 91])

    def test_filter_players_handles_dicts_and_rows(self):
        session = FakeSession(blocks=[Block(95, 100)])
        pool = [{"id": 1, "rating": 96}, {"id": 2, "rating": 80},
                SimpleNamespace(id=3, rating=99), SimpleNamespace(id=4, rating=70)]
        kept = self.blocks.filter_players(session, pool, "claim")
        self.assertEqual([p["id"] if isinstance(p, dict) else p.id for p in kept],
                         [2, 4])

    def test_unknown_rating_is_kept(self):
        # A card with no readable rating can't be matched against a rule;
        # dropping it would turn a data problem into a missing reward.
        session = FakeSession(blocks=[Block(50, 100)])
        pool = [{"id": 1, "rating": None}]
        self.assertEqual(len(self.blocks.filter_players(session, pool, "claim")), 1)

    def test_broken_table_fails_open(self):
        class ExplodingSession:
            def query(self, *a, **k):
                raise RuntimeError("no such table")

        self.assertEqual(self.blocks.blocked_ratings(ExplodingSession(), "claim"),
                         frozenset())

    def test_a_failed_read_is_not_retried_on_every_draw(self):
        # During an outage the draw rate would otherwise become the failing-query
        # rate, with a logged traceback per draw.
        calls = []

        class ExplodingSession:
            def query(self, *a, **k):
                calls.append(1)
                raise RuntimeError("connection reset")

        session = ExplodingSession()
        for _ in range(50):
            self.blocks.blocked_ratings(session, "claim")
        self.assertEqual(len(calls), 1, "the failing query was retried")

    def test_an_admin_edit_clears_the_failure_backoff(self):
        class ExplodingSession:
            def query(self, *a, **k):
                raise RuntimeError("connection reset")

        self.blocks.blocked_ratings(ExplodingSession(), "claim")
        # invalidate() is what the website calls after saving a rule; the new
        # rule has to take effect now, not once the backoff expires.
        self.blocks.invalidate()
        session = FakeSession(blocks=[Block(95, 100)])
        self.assertEqual(self.blocks.blocked_ratings(session, "claim"),
                         frozenset(range(95, 101)))

    def test_describe_collapses_runs(self):
        session = FakeSession(blocks=[Block(90, 94), Block(99, 100), Block(60, 60)])
        self.assertEqual(self.blocks.describe(session, "claim"), "60, 90-94, 99-100")

    def test_describe_says_so_when_nothing_blocked(self):
        self.assertEqual(self.blocks.describe(FakeSession(), "drop"), "nothing blocked")

    def test_unknown_source_blocks_nothing(self):
        session = FakeSession(blocks=[Block(50, 100)])
        self.assertEqual(self.blocks.blocked_ratings(session, "market"), frozenset())


class ClaimDrawRespectsBlocksTests(RatingBlockTestCase):
    def test_blocked_band_never_pays_out(self):
        # 99% of pulls should land in 90-94, but that whole band is blocked, so
        # every pull must come from the 1% band instead.
        tiers = [Tier(99.0, 90, 94, 1, 1), Tier(1.0, 50, 59, 2, 2)]
        session = FakeSession(tiers, blocks=[Block(90, 94)])
        self._patch_cache({92: [{"id": 92}], 55: [{"id": 55}]})

        picks = [self.ps.get_random_player_by_rarity(session) for _ in range(200)]
        self.assertTrue(all(p is not None for p in picks))
        self.assertTrue(all(p.id == 55 for p in picks),
                        "a blocked band was awarded anyway")

    def test_partly_blocked_band_pays_from_what_is_left(self):
        # 90-94 blocked only at 93-94: pulls should still land, on 90-92 only.
        tiers = [Tier(100.0, 90, 94, 1, 1)]
        session = FakeSession(tiers, blocks=[Block(93, 94)])
        self._patch_cache({90: [{"id": 90}], 93: [{"id": 93}], 94: [{"id": 94}]})

        picks = [self.ps.get_random_player_by_rarity(session) for _ in range(200)]
        self.assertTrue(all(p is not None for p in picks))
        self.assertTrue(all(p.id == 90 for p in picks))

    def test_everything_blocked_returns_none(self):
        # No eligible card anywhere beats handing out a blocked one.
        tiers = [Tier(100.0, 90, 94, 1, 1)]
        session = FakeSession(tiers, blocks=[Block(50, 100)])
        self._patch_cache({92: [{"id": 92}]})
        from services import player_cache
        player_cache.get_random_in_rating_range = lambda low, high: {"id": 92}

        self.assertIsNone(self.ps.get_random_player_by_rarity(session))

    def test_rating_range_draw_respects_blocks(self):
        # The /daily milestone card and the Mystery Box come through here.
        session = FakeSession(blocks=[Block(81, 83)])
        self._patch_cache({81: [{"id": 81}], 84: [{"id": 84}], 85: [{"id": 85}]})

        picks = [self.ps.get_random_player_by_rating_range(session, 81, 85)
                 for _ in range(100)]
        self.assertTrue(all(p is not None and p.id in (84, 85) for p in picks))

    def test_rating_range_draw_can_opt_out(self):
        # source=None is for draws that aren't random rewards at all.
        session = FakeSession(blocks=[Block(81, 85)])
        self._patch_cache({81: [{"id": 81}]})
        from services import player_cache
        player_cache.get_random_in_rating_range = lambda low, high: {"id": 81}

        pick = self.ps.get_random_player_by_rating_range(session, 81, 85, source=None)
        self.assertIsNotNone(pick)

    def test_an_unrelated_block_keeps_the_last_resort_fallback(self):
        # Widening 60-65 finds nothing. Without any block rule the cache's last
        # resort returns some active player; a rule about 95-100 says nothing
        # about 60-65 and must not turn that into "no player at all".
        session = FakeSession(blocks=[Block(95, 100)])
        self._patch_cache({})
        from services import player_cache
        player_cache.get_all_active = lambda: [{"id": 70, "rating": 70},
                                               {"id": 97, "rating": 97}]

        picks = [self.ps.get_random_player_by_rating_range(session, 60, 65)
                 for _ in range(50)]
        self.assertTrue(all(p is not None for p in picks), "fallback was lost")
        self.assertTrue(all(p.id == 70 for p in picks),
                        "the last resort handed out a blocked rating")

    def test_the_last_resort_is_filtered_not_just_present(self):
        session = FakeSession(blocks=[Block(50, 100)])
        self._patch_cache({})
        from services import player_cache
        player_cache.get_all_active = lambda: [{"id": 97, "rating": 97}]

        self.assertIsNone(
            self.ps.get_random_player_by_rating_range(session, 60, 65))

    def test_widening_never_crosses_into_a_blocked_rating(self):
        # 86-88 is empty, so the draw widens outward — but 89+ is blocked and
        # must stay untouched no matter how far the search reaches.
        session = FakeSession(blocks=[Block(89, 100)])
        self._patch_cache({85: [{"id": 85}], 90: [{"id": 90}]})

        picks = [self.ps.get_random_player_by_rating_range(session, 86, 88)
                 for _ in range(50)]
        self.assertTrue(all(p is not None and p.id == 85 for p in picks))


class SimulationTests(RatingBlockTestCase):
    def test_simulation_matches_configured_weights(self):
        tiers = [Tier(90.0, 50, 59, 1, 1), Tier(10.0, 90, 94, 2, 2)]
        session = FakeSession(tiers)
        self._patch_cache({55: [{"id": 55}], 92: [{"id": 92}]})

        result = self.ps.simulate_rarity_draws(session, draws=20000)
        by_range = {(b["low"], b["high"]): b for b in result["bands"]}
        low_share = by_range[(50, 59)]["draws"] / 20000
        self.assertAlmostEqual(low_share, 0.9, delta=0.02)
        self.assertEqual(result["misses"], 0)

    def test_simulation_skips_blocked_and_empty_bands(self):
        tiers = [Tier(99.0, 90, 94, 1, 1), Tier(1.0, 50, 59, 2, 2)]
        session = FakeSession(tiers, blocks=[Block(90, 94)])
        self._patch_cache({92: [{"id": 92}], 55: [{"id": 55}]})

        result = self.ps.simulate_rarity_draws(session, draws=500)
        by_range = {(b["low"], b["high"]): b for b in result["bands"]}
        self.assertEqual(by_range[(90, 94)]["pool"], 0)
        self.assertEqual(by_range[(90, 94)]["draws"], 0)
        self.assertEqual(by_range[(50, 59)]["draws"], 500)

    def test_simulation_reports_misses_when_nothing_is_drawable(self):
        tiers = [Tier(100.0, 90, 94, 1, 1)]
        session = FakeSession(tiers, blocks=[Block(90, 94)])
        self._patch_cache({92: [{"id": 92}]})

        result = self.ps.simulate_rarity_draws(session, draws=100)
        self.assertEqual(result["misses"], 100)

    def test_tiny_probability_survives_the_draw(self):
        # 0.00001% is the website's minimum. Over a big enough sample it has to
        # show up at roughly its configured rate rather than being rounded away.
        tiers = [Tier(99.99999, 50, 59, 1, 1), Tier(0.00001, 95, 100, 2, 2)]
        session = FakeSession(tiers)
        self._patch_cache({55: [{"id": 55}], 97: [{"id": 97}]})

        result = self.ps.simulate_rarity_draws(session, draws=200000)
        by_range = {(b["low"], b["high"]): b for b in result["bands"]}
        # The weight is intact even if 200k draws rarely land on it: what must
        # not happen is the tier being dropped or promoted to a visible rate.
        self.assertAlmostEqual(by_range[(95, 100)]["weight"], 1e-7, places=12)
        self.assertLess(by_range[(95, 100)]["draws"], 5)


if __name__ == "__main__":
    unittest.main()

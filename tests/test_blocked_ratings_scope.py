"""A blocked rating range must reach /claim, /daily and /gspin — and nothing else.

The bug this covers: an admin blocked 92-99 so the free draws would stop handing
out top cards, and the block took the packs with it. A pack advertising a
guaranteed 92-99 player found every candidate filtered out and paid nothing —
a card the player had already bought, silently withheld.

So the scope is now part of the design rather than a per-caller habit:

  * :data:`services.rating_block_service.SOURCES` names claim and gspin only,
    and every other source (including the retired ``"drop"``) reads back as
    "nothing blocked" — covered in ``tests/test_rating_blocks.py``.
  * ``get_random_player_by_rating_range`` filters nothing unless a caller opts
    in, so a new reward path can't inherit the veto by forgetting an argument.
  * The earned rewards — packs, free packs, the Mystery Box, the subscriber
    Weekly Card — don't consult the block rules at all.

The last point is asserted two ways: a pack really does hand out a card while
every rating is blocked, and the modules behind those rewards are checked for
any reference to the block service that would quietly bring the filtering back.
"""

import ast
import inspect
import os
import sys
import types
import unittest
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ════════════════════════════════════════════════════════════════════
# Stubs — the lean test env has neither sqlalchemy nor a database
# ════════════════════════════════════════════════════════════════════

class _Col:
    """A model column that swallows every filter expression built on it."""

    def is_(self, *_a): return None
    def between(self, *_a): return None
    def in_(self, *_a): return None
    def __eq__(self, _other): return None
    def __hash__(self): return id(self)


def _install_stubs():
    sa = types.ModuleType("sqlalchemy")
    sa.and_ = lambda *a, **k: None
    sa.or_ = lambda *a, **k: None
    sa.func = SimpleNamespace(lower=lambda c: _Col(), count=lambda c: None)
    sys.modules["sqlalchemy"] = sa
    sa_orm = types.ModuleType("sqlalchemy.orm")
    sa_orm.Session = object
    sys.modules["sqlalchemy.orm"] = sa_orm

    models = types.ModuleType("models")
    for name in ("Pack", "PackPurchase", "UnopenedPack", "User", "UserRoster",
                 "ClaimRarityTier"):
        setattr(models, name, type(name, (), {}))
    models.Player = type("Player", (), {
        "id": _Col(), "rating": _Col(), "is_active": _Col(), "version": _Col(),
        "parent_player_id": _Col(), "is_career": _Col()})
    models.RatingBlockRule = type("RatingBlockRule", (), {"is_active": True})
    sys.modules["models"] = models

    config = types.ModuleType("config")
    config.CLAIM_RARITY = [(1.0, 50, 100)]
    config.get_buy_value = lambda r: r * 100
    config.get_sell_value = lambda r: r * 50
    sys.modules["config"] = config

    for name in ("services.player_service", "services.pack_service"):
        sys.modules.pop(name, None)
    from services import pack_service, player_service, rating_block_service
    return pack_service, player_service, rating_block_service


_STUBBED_MODULES = ("sqlalchemy", "sqlalchemy.orm", "models", "config",
                    "services.player_service", "services.pack_service")


class Block:
    """A RatingBlockRule stand-in with every source — old and new — switched on."""

    def __init__(self, rating_min, rating_max):
        self.rating_min = rating_min
        self.rating_max = rating_max
        self.block_claim = True
        self.block_drop = True      # the retired column, as legacy rows carry it
        self.block_gspin = True
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
    """Serves player rows to the pack picks and block rules to the service."""

    def __init__(self, players=(), blocks=()):
        self._players = list(players)
        self._blocks = list(blocks)

    def query(self, model=None, *a, **k):
        import models as _models
        if model is _models.RatingBlockRule:
            return _Query([b for b in self._blocks if b.is_active])
        return _Query(list(self._players))


def _player(pid, rating, version="Legend"):
    return SimpleNamespace(id=pid, rating=rating, version=version,
                           parent_player_id=None, is_active=True,
                           is_career=False)


# ════════════════════════════════════════════════════════════════════
# 1. Packs pay out while the free draws are blocked
# ════════════════════════════════════════════════════════════════════

class PackIgnoresBlockedRatingsTests(unittest.TestCase):
    """The regression itself: 92-99 blocked, the 92-99 pack still delivers."""

    def setUp(self):
        self._saved = {k: sys.modules.get(k) for k in _STUBBED_MODULES}
        self.packs, self.ps, self.blocks = _install_stubs()
        self.blocks.invalidate()
        self.session = FakeSession(
            players=[_player(1, 92), _player(2, 95), _player(3, 99)],
            blocks=[Block(50, 100)])

    def tearDown(self):
        self.blocks.invalidate()
        for key, value in self._saved.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value

    def _pack(self, mode="rating", versions=None):
        return SimpleNamespace(
            id=1, main_filter_mode=mode,
            main_min_rating=92, main_max_rating=99, main_count=1,
            main_weights_json=None, main_versions_json=versions,
            bonus_min_rating=92, bonus_max_rating=99, bonus_count=2,
            bonus_weights_json=None)

    def test_the_rules_really_are_live_in_this_session(self):
        # Guards the tests below: if the fake session stopped serving rules
        # they would pass for the wrong reason.
        self.assertEqual(self.blocks.blocked_ratings(self.session, "claim"),
                         frozenset(range(50, 101)))

    def test_rating_mode_pack_still_hands_over_its_guaranteed_card(self):
        for _ in range(20):
            player = self.packs._pick_main_player(self.session, self._pack())
            self.assertIsNotNone(player, "a bought pack paid out nothing")
            self.assertIn(player.rating, (92, 95, 99))

    def test_version_mode_pack_still_hands_over_its_guaranteed_card(self):
        pack = self._pack(mode="version", versions='["Legend"]')
        player = self.packs._pick_main_player(self.session, pack)
        self.assertIsNotNone(player)

    def test_both_mode_pack_still_hands_over_its_guaranteed_card(self):
        pack = self._pack(mode="both", versions='["Legend"]')
        player = self.packs._pick_main_player(self.session, pack)
        self.assertIsNotNone(player)

    def test_the_bonus_slots_are_not_filtered_either(self):
        player = self.packs._pick_bonus_player(self.session, self._pack())
        self.assertIsNotNone(player)

    def test_the_pity_pull_is_not_filtered_either(self):
        # Pity forces the pack's max rating — exactly the rating an admin is
        # most likely to have blocked from /claim.
        player = self.packs._pick_main_player(self.session, self._pack(),
                                              force_rating=99)
        self.assertIsNotNone(player)

    def test_a_random_draw_filters_nothing_unless_asked(self):
        # The default that stops a future reward path inheriting the veto.
        default = inspect.signature(
            self.ps.get_random_player_by_rating_range).parameters["source"].default
        self.assertIsNone(default, "blocks must be opt-in, not opt-out")


# ════════════════════════════════════════════════════════════════════
# 2. The earned-reward paths never reach for the block service
# ════════════════════════════════════════════════════════════════════

# Reward paths a player bought, earned or watched an ad for. None of them may
# consult the block rules, and none may pass a block source into a draw.
EARNED_REWARD_MODULES = (
    "services/pack_service.py",        # bought and granted packs
    "services/free_pack_service.py",   # ad-gated Free Pack
    "handlers/cmumysterybox.py",       # Mystery Box
    "handlers/premium_drops.py",       # subscriber Weekly Card
    "handlers/packs.py",               # the pack commands
)

# The draws a block is allowed to trim, and the module that owns each.
BLOCK_SOURCE_CALLERS = {
    "claim": ("handlers/claim.py", "handlers/daily.py",
              "services/daily_service.py", "services/player_service.py"),
    "gspin": ("handlers/gspin.py", "services/gspin_reward_service.py"),
}


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class BlockScopeTests(unittest.TestCase):
    def test_earned_rewards_do_not_use_the_block_service(self):
        for rel in EARNED_REWARD_MODULES:
            with self.subTest(module=rel):
                tree = ast.parse(_read(rel))
                names = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        names.update(a.name.rsplit(".", 1)[-1] for a in node.names)
                    elif isinstance(node, ast.ImportFrom):
                        names.update(a.name for a in node.names)
                self.assertNotIn(
                    "rating_block_service", names,
                    f"{rel} filters an earned reward through the block rules")

    def test_no_reward_path_passes_a_retired_block_source(self):
        for rel in EARNED_REWARD_MODULES:
            with self.subTest(module=rel):
                self.assertNotIn('source="drop"', _read(rel))
                self.assertNotIn("source='drop'", _read(rel))

    def test_the_blockable_draws_still_opt_in(self):
        # The other half of the guard: making blocks opt-in is only safe if the
        # two draws that want them actually ask.
        for source, modules in BLOCK_SOURCE_CALLERS.items():
            asked = [rel for rel in modules if f'source="{source}"' in _read(rel)]
            self.assertTrue(asked, f"nothing opts into the {source!r} block")

    def test_the_website_offers_only_the_two_blockable_sources(self):
        page = _read("templates/admin_rarity.html")
        self.assertIn("block_claim", page)
        self.assertIn("block_gspin", page)
        self.assertNotIn("block_drop", page,
                         "the website still offers to block card drops")


if __name__ == "__main__":
    unittest.main()

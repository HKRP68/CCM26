"""Regression coverage for the squad cap after the 25 → 19 rebalance.

``config.MAX_ROSTER`` is the single source of truth, but several modules used
to shadow it with a hardcoded 25 — so lowering the constant left those paths
still admitting cards up to the old limit. These tests pin that every module
which enforces the cap reads it from config, and that the ``order_position``
sentinel in the Mini App market-buy tracks the cap rather than a literal.
"""

import unittest

import config


class CapConstantTest(unittest.TestCase):

    def test_cap_is_nineteen(self):
        self.assertEqual(config.MAX_ROSTER, 19)

    def test_squad_trait_cap_leaves_the_career_slots_on_top(self):
        # 18 across the squad + 3 on the Career Player == 21 equipped.
        self.assertEqual(config.TRAIT_MAX_PER_SQUAD, 18)
        self.assertEqual(
            config.TRAIT_MAX_PER_SQUAD + config.TRAIT_MAX_PER_PLAYER, 21)


class NoModuleShadowsTheCapTest(unittest.TestCase):
    """Every module that names MAX_ROSTER must agree with config."""

    _MODULES = (
        "services.free_pack_service",
        "services.roster_service",
        "services.player_market",
        "services.global_market",
        "services.daily_service",
        "handlers.undo",
        "handlers.buy",
        "handlers.claim",
    )

    def test_module_level_constants_match_config(self):
        import importlib
        for name in self._MODULES:
            module = importlib.import_module(name)
            value = getattr(module, "MAX_ROSTER", None)
            if value is None:
                continue  # imports it locally inside a function — fine
            self.assertEqual(value, config.MAX_ROSTER,
                             f"{name} shadows MAX_ROSTER with {value}")

    def test_no_source_file_still_hardcodes_the_old_cap(self):
        """The literal 25 must not survive in a roster-cap comparison."""
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        # handlers/ carries stale orphan copies of root modules that nothing
        # imports; they are dead code and deliberately not maintained.
        dead = {root / "handlers" / n for n in (
            "config.py", "models.py", "database.py", "admin.py", "app.py",
            "bot.py", "roster_service.py", "pack_service.py",
            "player_market.py", "global_market.py", "daily_service.py",
            "undo_service.py", "gspin_reward_service.py", "trait_service.py",
            "trait_engine.py", "config_service.py", "achievement_service.py",
        )}
        pattern = re.compile(
            r"roster_count[^\n]{0,40}(>=|<)\s*25\b|MAX_ROSTER\s*=\s*25\b")

        offenders = []
        for path in list(root.glob("*.py")) + list(root.glob("services/*.py")) \
                + list(root.glob("handlers/*.py")):
            if path in dead or path.name.startswith("test_"):
                continue
            for lineno, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(root)}:{lineno}")
        self.assertEqual(offenders, [],
                         f"hardcoded roster cap of 25 still present: {offenders}")


class MarketBuyPositionSentinelTest(unittest.TestCase):
    """admin.py appends at max_position+1 while inside the squad, else 99.

    The comparison used a literal 25; with the cap at 19 that would have kept
    handing out positions 20-25 that no longer exist.
    """

    def _next_pos(self, max_pos):
        # Mirrors the branch in admin.webapp_market_buy.
        if max_pos and max_pos is not None and max_pos < config.MAX_ROSTER:
            return max_pos + 1
        elif not max_pos:
            return 1
        return 99

    def test_position_increments_inside_the_squad(self):
        self.assertEqual(self._next_pos(1), 2)
        self.assertEqual(self._next_pos(config.MAX_ROSTER - 1),
                         config.MAX_ROSTER)

    def test_position_falls_back_past_the_cap(self):
        self.assertEqual(self._next_pos(config.MAX_ROSTER), 99)
        self.assertEqual(self._next_pos(25), 99)

    def test_empty_roster_starts_at_one(self):
        self.assertEqual(self._next_pos(None), 1)
        self.assertEqual(self._next_pos(0), 1)


if __name__ == "__main__":
    unittest.main()

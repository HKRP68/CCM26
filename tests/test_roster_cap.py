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

    # handlers/ carries stale orphan copies of root/services modules that
    # nothing imports; they are dead code and deliberately not maintained.
    _DEAD_HANDLER_COPIES = (
        "config.py", "models.py", "database.py", "admin.py", "app.py",
        "bot.py", "roster_service.py", "pack_service.py", "player_market.py",
        "global_market.py", "daily_service.py", "undo_service.py",
        "gspin_reward_service.py", "trait_service.py", "trait_engine.py",
        "config_service.py", "achievement_service.py", "message_service.py",
    )

    def _live_sources(self, *globs):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        dead = {root / "handlers" / n for n in self._DEAD_HANDLER_COPIES}
        for pattern in globs:
            for path in root.glob(pattern):
                if path in dead or path.name.startswith("test_"):
                    continue
                yield root, path

    def test_no_source_file_still_hardcodes_the_old_cap(self):
        """The literal 25 must not survive in a roster-cap comparison."""
        import re
        pattern = re.compile(
            r"roster_count[^\n]{0,40}(>=|<)\s*25\b|MAX_ROSTER\s*=\s*25\b")

        offenders = []
        for root, path in self._live_sources("*.py", "services/*.py",
                                             "handlers/*.py"):
            for lineno, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(root)}:{lineno}")
        self.assertEqual(offenders, [],
                         f"hardcoded roster cap of 25 still present: {offenders}")

    def test_no_user_facing_message_still_shows_the_old_cap(self):
        """"12/25" style counters must interpolate the cap, not hardcode it.

        The enforcement sweep originally missed a dozen of these — the squad
        was capped at 19 while chat and the admin pages still rendered "/25".
        Any ``<something>/25`` in a display string is caught here.
        """
        import re
        # "/25" preceded by a closing brace/quote/word and not part of a longer
        # number, date or path — i.e. a rendered "N/25" counter.
        pattern = re.compile(r"[}\w'\"]\s*/\s*25(?![\d/:.-])")
        # Scoped to lines that are talking about a user's roster. Bowling
        # figures ("4/25 (4 OVERS)") and the separate 25-player BOT team limit
        # (services/bot_team_service.py) legitimately render "/25".
        roster_line = re.compile(r"roster|squad", re.IGNORECASE)

        offenders = []
        for root, path in self._live_sources("*.py", "services/*.py",
                                             "handlers/*.py",
                                             "templates/*.html"):
            for lineno, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line) and roster_line.search(line):
                    offenders.append(
                        f"{path.relative_to(root)}:{lineno}: {line.strip()[:90]}")
        self.assertEqual(offenders, [],
                         "user-facing '/25' roster counter still present:\n"
                         + "\n".join(offenders))


class MarketBuyPositionSentinelTest(unittest.TestCase):
    """admin.py appends at max_position+1 while inside the squad, else 99.

    The comparison used a literal 25; with the cap at 19 that would have kept
    handing out positions 20-25 that no longer exist. These call the production
    helper directly — a local re-implementation would keep passing even if the
    real branch went back to a literal.
    """

    def _next_pos(self, max_pos):
        from admin import _next_roster_position
        # The production helper takes the (order_position,) row the query
        # returns, so None means "user holds no cards".
        return _next_roster_position(None if max_pos is None else (max_pos,))

    def test_position_increments_inside_the_squad(self):
        self.assertEqual(self._next_pos(1), 2)
        self.assertEqual(self._next_pos(config.MAX_ROSTER - 1),
                         config.MAX_ROSTER)

    def test_position_falls_back_past_the_cap(self):
        self.assertEqual(self._next_pos(config.MAX_ROSTER), 99)
        self.assertEqual(self._next_pos(25), 99)

    def test_empty_roster_starts_at_one(self):
        self.assertEqual(self._next_pos(None), 1)

    def test_position_zero_is_inside_the_squad(self):
        # 0 is a real position, not "no cards" — it must not fall through to 1
        # via a truthiness check.
        self.assertEqual(self._next_pos(0), 1)

    def test_a_null_position_row_goes_to_the_unordered_bucket(self):
        # A card exists but has no position: there is nothing to append after,
        # so it joins the 99 bucket rather than claiming position 1.
        from admin import _next_roster_position
        self.assertEqual(_next_roster_position((None,)), 99)


if __name__ == "__main__":
    unittest.main()

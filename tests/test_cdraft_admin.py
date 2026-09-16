"""`/cdraftset` and the Match Gameplay page — the two ways an admin sets the
Challenge Draft pool.

Both write the same three ``GameConfig`` values, so the thing worth testing is
that each entry point turns what a person typed or ticked into the same stored
setting, and refuses what it cannot store rather than quietly emptying the pool.

The admin route and its template are checked at source level, the convention
this repo already follows for admin pages — importing ``admin`` pulls in the
whole Flask app and binds it to a live database (see
``tests/test_admin_pack_and_market_feeds.py``).
"""

import os
import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from handlers import cdraft_admin
from services import cdraft_service


def _read(path):
    with open(os.path.join(os.path.dirname(__file__), "..", path)) as fh:
        return fh.read()


class ResolveVersionsTests(unittest.TestCase):
    """Typed names are matched against the catalogue, not stored on trust."""

    AVAILABLE = ["Base", "Legend", "IPL 2026"]

    def test_names_come_back_in_the_catalogues_own_spelling(self):
        labels, unknown = cdraft_admin.resolve_versions(
            ["base", "ipl 2026"], self.AVAILABLE)
        self.assertEqual(labels, ["Base", "IPL 2026"])
        self.assertEqual(unknown, [])

    def test_an_unknown_name_is_reported_not_stored(self):
        labels, unknown = cdraft_admin.resolve_versions(
            ["Base", "Lgend"], self.AVAILABLE)
        self.assertEqual(labels, ["Base"])
        self.assertEqual(unknown, ["Lgend"])

    def test_duplicates_and_blanks_are_dropped(self):
        labels, unknown = cdraft_admin.resolve_versions(
            [" Base ", "base", "", "  "], self.AVAILABLE)
        self.assertEqual(labels, ["Base"])
        self.assertEqual(unknown, [])


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class CdraftSetCommandTests(unittest.IsolatedAsyncioTestCase):

    AVAILABLE = ["Base", "Legend", "TOTY"]

    def setUp(self):
        self.row = SimpleNamespace(cdraft_rating_min=78, cdraft_rating_max=88,
                                   cdraft_versions_json=None,
                                   cdraft_pair_spread=1)
        self.committed = []
        self.stored = {}

        session = SimpleNamespace(close=lambda: None, rollback=lambda: None)
        real_load_settings = cdraft_service.load_settings
        patches = [
            patch.object(cdraft_admin, "is_admin", lambda tg_id: tg_id == 1),
            patch.object(cdraft_admin, "get_session", lambda: session),
            patch.object(cdraft_admin, "_load_config_row", lambda s: self.row),
            patch.object(cdraft_admin, "known_versions",
                         lambda s: list(self.AVAILABLE)),
            patch.object(cdraft_admin, "_pool_report", lambda s: "POOL REPORT"),
            patch.object(cdraft_admin, "_commit",
                         lambda s: self.committed.append(True)),
            # load_settings normally reads the live config row; here it reads
            # back whatever this fake row currently holds.
            patch.object(cdraft_service, "load_settings",
                         lambda config=None: real_load_settings({
                             "cdraft_rating_min": self.row.cdraft_rating_min,
                             "cdraft_rating_max": self.row.cdraft_rating_max,
                             "cdraft_pair_spread": self.row.cdraft_pair_spread,
                             "cdraft_versions_json": self.row.cdraft_versions_json,
                         })),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    async def _run(self, *args, tg_id=1):
        message = FakeMessage()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=tg_id),
                                 effective_message=message)
        await cdraft_admin.cdraftset_handler(
            update, SimpleNamespace(args=list(args)))
        return message.replies[-1] if message.replies else ""

    async def test_a_non_admin_is_refused_and_nothing_is_written(self):
        reply = await self._run("min", "50", tg_id=999)
        self.assertEqual(reply, cdraft_admin.NOT_ADMIN)
        self.assertEqual(self.row.cdraft_rating_min, 78)
        self.assertFalse(self.committed)

    async def test_no_arguments_shows_the_pool(self):
        self.assertEqual(await self._run(), "POOL REPORT")
        self.assertFalse(self.committed)

    async def test_min_and_max_move_one_end_each(self):
        await self._run("min", "80")
        self.assertEqual((self.row.cdraft_rating_min, self.row.cdraft_rating_max),
                         (80, 88))
        await self._run("max", "92")
        self.assertEqual((self.row.cdraft_rating_min, self.row.cdraft_rating_max),
                         (80, 92))
        self.assertEqual(len(self.committed), 2)

    async def test_range_sets_both(self):
        await self._run("range", "75", "95")
        self.assertEqual((self.row.cdraft_rating_min, self.row.cdraft_rating_max),
                         (75, 95))

    async def test_a_backwards_range_is_a_typo_not_an_empty_band(self):
        await self._run("range", "95", "75")
        self.assertEqual((self.row.cdraft_rating_min, self.row.cdraft_rating_max),
                         (75, 95))

    async def test_a_rating_outside_the_limits_is_clamped(self):
        await self._run("min", "-40")
        self.assertEqual(self.row.cdraft_rating_min,
                         cdraft_service.RATING_FLOOR_LIMIT)

    async def test_a_non_numeric_rating_is_refused(self):
        reply = await self._run("min", "eighty")
        self.assertIn("whole number", reply)
        self.assertEqual(self.row.cdraft_rating_min, 78)
        self.assertFalse(self.committed)

    async def test_a_missing_rating_is_refused(self):
        reply = await self._run("min")
        self.assertIn("whole number", reply)
        self.assertFalse(self.committed)

    async def test_range_with_one_number_is_refused(self):
        reply = await self._run("range", "80")
        self.assertIn("whole number", reply)
        self.assertFalse(self.committed)

    async def test_spread_sets_the_gap_allowed_inside_a_slot(self):
        await self._run("spread", "2")
        self.assertEqual(self.row.cdraft_pair_spread, 2)
        self.assertTrue(self.committed)

    async def test_spread_is_clamped_to_keep_the_two_squads_fair(self):
        await self._run("spread", "99")
        self.assertEqual(self.row.cdraft_pair_spread,
                         cdraft_service.SPREAD_LIMIT_HIGH)

    async def test_a_non_numeric_spread_is_refused(self):
        reply = await self._run("spread", "wide")
        self.assertIn("whole number", reply)
        self.assertEqual(self.row.cdraft_pair_spread, 1)
        self.assertFalse(self.committed)

    async def test_versions_stores_the_catalogues_spelling(self):
        await self._run("versions", "base,", "legend")
        self.assertEqual(
            cdraft_service.parse_allowed_versions(self.row.cdraft_versions_json),
            ["Base", "Legend"])

    async def test_an_unknown_version_is_refused_with_the_valid_list(self):
        self.row.cdraft_versions_json = '["Base"]'
        reply = await self._run("versions", "Base, Lgend")
        self.assertIn("Lgend", reply)
        for label in self.AVAILABLE:
            self.assertIn(label, reply)
        self.assertEqual(self.row.cdraft_versions_json, '["Base"]',
                         "a typo must not change what is stored")
        self.assertFalse(self.committed)

    async def test_versions_all_clears_the_list(self):
        self.row.cdraft_versions_json = '["Base"]'
        await self._run("versions", "all")
        self.assertIsNone(self.row.cdraft_versions_json)

    async def test_versions_with_nothing_named_is_refused(self):
        reply = await self._run("versions")
        self.assertIn("/cdraftset", reply)
        self.assertFalse(self.committed)

    async def test_reset_restores_the_defaults(self):
        self.row.cdraft_rating_min, self.row.cdraft_rating_max = 60, 99
        self.row.cdraft_versions_json = '["TOTY"]'
        self.row.cdraft_pair_spread = 4
        await self._run("reset")
        self.assertEqual(self.row.cdraft_rating_min, cdraft_service.RATING_BOTTOM)
        self.assertEqual(self.row.cdraft_rating_max, cdraft_service.RATING_TOP)
        self.assertEqual(self.row.cdraft_pair_spread, cdraft_service.PAIR_SPREAD)
        self.assertIsNone(self.row.cdraft_versions_json)

    async def test_an_unrecognised_subcommand_prints_the_usage(self):
        reply = await self._run("wobble")
        self.assertEqual(reply, cdraft_admin.USAGE)
        self.assertFalse(self.committed)


class ConfigPlumbingTests(unittest.TestCase):
    """The two places a new config value is most often half-added."""

    KEYS = ("cdraft_rating_min", "cdraft_rating_max", "cdraft_versions_json",
            "cdraft_pair_spread")

    def test_every_key_has_a_default(self):
        """save_config silently ignores keys that aren't in DEFAULTS, so a
        missing one means the setting saves without error and never sticks."""
        from services.config_service import DEFAULTS
        for key in self.KEYS:
            with self.subTest(key=key):
                self.assertIn(key, DEFAULTS)

    def test_every_key_has_a_column_and_a_migration(self):
        models_src = _read("models.py")
        database_src = _read("database.py")
        for key in self.KEYS:
            with self.subTest(key=key):
                self.assertRegex(models_src, rf"\n\s*{key}\s*=\s*Column\(")
                self.assertIn(f'_try_add("game_config", "{key}"', database_src)

    def test_the_json_column_can_actually_be_cleared(self):
        """save_config treats None as 'leave this alone' unless the key is in
        allow_null — without it, unticking every box could never take effect."""
        src = _read("admin.py")
        route = src[src.index("def admin_match_settings"):]
        route = route[:route.index("\n@app.route")]
        self.assertIn('allow_null=("cdraft_versions_json",)', route)


class MatchSettingsPageTests(unittest.TestCase):
    """The website half, checked at source level."""

    def setUp(self):
        src = _read("admin.py")
        start = src.index("def admin_match_settings")
        self.route = src[start:start + src[start:].index("\n@app.route")]
        self.admin_src = src
        self.template = _read("templates/admin_match_settings.html")

    def test_the_route_saves_all_three_settings(self):
        for key in ConfigPlumbingTests.KEYS:
            with self.subTest(key=key):
                self.assertIn(key, self.route)

    def test_the_route_reads_the_form_fields_the_template_renders(self):
        for field in ("cdraft_rating_min", "cdraft_rating_max"):
            self.assertIn(f'name="{field}"', self.template)
            self.assertIn(f'"{field}"', self.route)
        self.assertIn('name="cdraft_versions"', self.template)
        self.assertIn('request.form.getlist("cdraft_versions")', self.route)

    def test_the_version_grid_renders_checkboxes_with_counts(self):
        self.assertIn("cdraft_versions", self.template)
        self.assertIn('type="checkbox"', self.template)
        self.assertRegex(self.template,
                         r"for label, count in cdraft_versions")
        self.assertIn("cdraft_allowed", self.template)

    def test_the_page_says_that_ticking_nothing_allows_everything(self):
        """The one rule a person cannot guess from the boxes themselves."""
        self.assertRegex(self.template,
                         r"(?is)tick nothing and every (edition|version) is[\s\S]{0,40}allowed")

    def test_the_route_warns_when_the_settings_cannot_deal_a_draft(self):
        self.assertIn("feasibility_shortfalls", self.route)

    def test_a_bad_rating_flashes_rather_than_five_hundreds(self):
        self.assertRegex(self.route, r"except ValueError:\s*\n\s*flash\(")

    def test_the_helpers_exclude_career_cards_from_the_pool(self):
        """A career card belongs to one user and must never be dealt."""
        helpers = self.admin_src[self.admin_src.index("def _cdraft_pool_rows"):
                                 self.admin_src.index("def admin_match_settings")]
        self.assertIn("not_career", helpers)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

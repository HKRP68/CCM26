"""The Packs and Markets tabs say who got what, not just that something moved.

Both feeds are audit tables keyed by id: ``user #84 bought pack #3, got
[{"player_id": 91, ...}]``. Nobody can act on that. ``services/audit_feed.py``
resolves the rows to names before they reach the page, and a pack that is bought
but still sealed has to read as sealed rather than as an empty haul — the pulls
are only recorded when the manager actually opens it.

The template and route halves are checked at source level: importing ``admin``
pulls in the whole Flask app and binds it to a live database, which poisons the
module identity later test files rely on and is far more than these assertions
need.
"""

import json
import os
import re
import unittest

from services.audit_feed import manager_label, pack_pulls


def _read(path):
    with open(os.path.join(os.path.dirname(__file__), "..", path)) as fh:
        return fh.read()


class PackPullsTests(unittest.TestCase):
    def test_a_sealed_pack_is_none_not_an_empty_haul(self):
        """players_json is written on open, so its absence means "not yet"."""
        self.assertIsNone(pack_pulls(None))
        self.assertIsNone(pack_pulls(""))

    def test_an_opened_pack_lists_what_came_out(self):
        pulls = pack_pulls(json.dumps([
            {"player_id": 1, "name": "Virat Kohli", "rating": 93, "slot": "main"},
            {"player_id": 2, "name": "Ravi Bishnoi", "rating": 78, "slot": "bonus"},
        ]))
        self.assertEqual([p["name"] for p in pulls],
                         ["Virat Kohli", "Ravi Bishnoi"])
        self.assertEqual([p["slot"] for p in pulls], ["main", "bonus"])
        self.assertEqual(pulls[0]["rating"], 93)

    def test_a_pull_with_no_name_still_identifies_the_card(self):
        self.assertEqual(pack_pulls(json.dumps([{"player_id": 7}]))[0]["name"],
                         "#7")

    def test_corrupt_json_shows_an_empty_haul_rather_than_a_500(self):
        for bad in ("not json", "{}", "[1,2]", "null"):
            self.assertIsInstance(pack_pulls(bad), list, bad)

    def test_a_bare_name_from_an_older_audit_row_still_reads(self):
        pull = pack_pulls(json.dumps(["Virat Kohli"]))[0]
        self.assertEqual(pull["name"], "Virat Kohli")
        self.assertIsNone(pull["rating"])


class ManagerLabelTests(unittest.TestCase):
    class _User:
        def __init__(self, username=None, first_name=None, team_name=None):
            self.username = username
            self.first_name = first_name
            self.team_name = team_name

    def test_the_handle_wins(self):
        self.assertEqual(manager_label(self._User("sam", "Samir", "XI")), "sam")

    def test_it_falls_back_through_name_then_team(self):
        self.assertEqual(manager_label(self._User(None, "Samir")), "Samir")
        self.assertEqual(manager_label(self._User(None, "  ", "Delhi XI")),
                         "Delhi XI")

    def test_a_manager_with_nothing_to_show_returns_none(self):
        """The page pairs the label with the id, so None is a real answer."""
        self.assertIsNone(manager_label(self._User()))
        self.assertIsNone(manager_label(None))


class PackRouteTests(unittest.TestCase):
    """The route has to hand the template names, not the ids it reads."""

    def setUp(self):
        admin = _read("admin.py")
        self.route = (admin.split("def admin_packs_list():")[1]
                      .split("\n@app.route")[0])

    def test_it_resolves_the_manager_the_pack_and_the_pulls(self):
        for key in ("user_label", "pack_label", "pulls"):
            self.assertIn(f'"{key}"', self.route)
        self.assertIn("manager_label(", self.route)
        self.assertIn("pack_pulls(", self.route)

    def test_the_managers_are_looked_up_in_one_batch(self):
        """One query for the page, not one per row."""
        self.assertIn("users_by_id(", self.route)
        # The batch helper is the *only* way this route reaches users — a
        # per-row lookup would have to query User itself to get there.
        self.assertNotIn("query(User)", self.route)

    def test_the_feed_is_newest_first(self):
        self.assertIn("purchased_at.desc()", self.route)


class MarketRouteTests(unittest.TestCase):
    def setUp(self):
        admin = _read("admin.py")
        self.route = (admin.split("def admin_markets_overview():")[1]
                      .split("\n@app.route")[0])

    def test_it_resolves_the_buyer_and_the_card(self):
        self.assertIn("manager_label(", self.route)
        self.assertIn('"item_label"', self.route)
        self.assertIn('"player"', self.route)

    def test_only_player_rows_are_looked_up_as_players(self):
        """A trait purchase stores a trait id in the same column."""
        self.assertIn('== "player"', self.route)

    def test_the_cards_are_looked_up_in_one_batch(self):
        self.assertIn("Player.id.in_(bought_player_ids)", self.route)


class FeedTemplateTests(unittest.TestCase):
    def test_the_pack_table_shows_who_what_and_got(self):
        html = _read("templates/admin_packs.html")
        self.assertIn("Who Opened What", html)
        for column in (">Who<", ">Pack<", ">Got<"):
            self.assertIn(column, html)
        self.assertIn("pp.user_label", html)
        self.assertIn("pp.pack_label", html)
        self.assertIn("pull.name", html)
        self.assertIn("pull.rating", html)

    def test_a_sealed_pack_reads_as_sealed(self):
        html = _read("templates/admin_packs.html")
        self.assertIn("pp.pulls is none", html)
        self.assertIn("Sealed", html)

    def test_main_and_bonus_pulls_are_told_apart(self):
        html = _read("templates/admin_packs.html")
        self.assertIn("pull.slot == 'main'", html)

    def test_the_market_table_shows_who_and_got(self):
        html = _read("templates/admin_markets.html")
        self.assertIn("Who Bought What", html)
        self.assertIn("r.user_label", html)
        self.assertIn("r.item_label", html)
        self.assertIn("r.player.rating", html)

    def test_neither_table_still_prints_a_bare_id_as_the_who(self):
        for path in ("templates/admin_packs.html", "templates/admin_markets.html"):
            html = _read(path)
            self.assertNotRegex(html, r"user #\{\{\s*r\.user_id")
            self.assertNotIn("pp.players_json", html)

    def test_the_row_fields_the_templates_read_all_exist(self):
        """Both feeds moved the audit row under a "row" key — catch a stale field."""
        packs = _read("templates/admin_packs.html")
        markets = _read("templates/admin_markets.html")
        for html, prefix, columns in (
                (packs, "pp", {"purchased_at", "user_id", "cost_paid", "currency"}),
                (markets, "r", {"purchased_at", "user_id", "market_type",
                                "price_paid"})):
            used = set(re.findall(rf"{prefix}\.row\.([a-z_]+)", html))
            self.assertTrue(columns <= used,
                            f"{sorted(columns - used)} not read via .row")
            stale = set(re.findall(rf"{prefix}\.({'|'.join(columns)})\b", html))
            self.assertFalse(stale, f"{sorted(stale)} still read off the wrapper")


if __name__ == "__main__":
    unittest.main()

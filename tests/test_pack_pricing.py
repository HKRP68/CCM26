"""Pricing a pack from what it can actually pull.

Admins used to price packs by eye: open the rating table, guess what the filters
produce, pick a number. A pack priced by guess is either free money or dead
stock, so ``services/pack_pricing.py`` does the arithmetic from the same filters
the pack draws from. These tests pin the parts that decide whether the number is
worth trusting:

  • **the averaging follows the pull.** A slot that rolls a RATING and then a
    card at it — rating mode, and the bonus slot — is worth the mean over its
    ratings, so a band with forty 74s and one 80 is worth about halfway between
    them. A version slot with no odds set draws uniformly over the CARDS, so
    the same lopsided spread there is worth about a 74. Getting this backwards
    prices a pack at a fraction of what it hands out.
  • **weights are the odds, so they are the weighting.** 60/30/10 over 85-87 is
    not the same pack as even odds over 85-87, and pricing it as though it were
    overcharges for the cheap end.
  • **a rating with no card cannot be pulled.** Its weight is dropped and the
    rest renormalised, and the gap is reported — an unfillable band is nearly
    always a mistake, not a price.
  • **the round-off only ever goes up.** A suggestion that undercuts the cards
    inside by a rounding error is the one direction that costs the game coins.
"""

import itertools
import os
import sys
import tempfile
import unittest

_SEQ = itertools.count(1)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
# ``services.player_service`` is on the list because ``pack_pricing`` filters
# every query through its ``not_career``. Leave it cached from an earlier test
# file and that filter carries a *different* ``models.Player`` class than the
# one this file just re-imported — SQLAlchemy then builds "FROM players,
# players" and every query here comes back a cartesian product.
# ``services.pack_odds`` is on it for the same reason ``pack_pricing`` is: it
# holds the pools and the version parsing both of them query ``Player`` with.
_MODULE_NAMES = ("database", "models", "config",
                 "services.player_service", "services.pack_odds",
                 "services.pack_pricing")


def _sync_package_attr(name, module):
    """Keep the parent package's attribute in step with ``sys.modules``.

    ``sys.modules.pop("services.pack_service")`` is not enough on its own: the
    ``services`` package object still carries ``pack_service`` as an attribute,
    and ``from services import pack_service`` reads that attribute in
    preference to importing anything. Several test files in this suite stub
    ``models`` and import service modules against the stub; without this, one
    of those stubbed modules is handed straight to this file, bringing a
    ``Player`` class that is not the one imported here — and every query comes
    back "ambiguous column name: players.id", a very long way from the cause.
    """
    if "." not in name:
        return
    parent, _, child = name.rpartition(".")
    package = sys.modules.get(parent)
    if package is None:
        return
    if module is None:
        if hasattr(package, child):
            delattr(package, child)
    else:
        setattr(package, child, module)


def setUpModule():
    global _PREV_DATABASE_URL, _SAVED_MODULES, _TMP, _ENGINE

    _PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
    _SAVED_MODULES = {name: sys.modules.get(name) for name in _MODULE_NAMES}
    for name in _MODULE_NAMES:
        sys.modules.pop(name, None)
        _sync_package_attr(name, None)

    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"

    from database import Base, engine
    import models  # noqa: F401  (registers the tables on Base)

    _ENGINE = engine
    Base.metadata.create_all(bind=engine)


def tearDownModule():
    try:
        _ENGINE.dispose()
    except Exception:
        pass
    if _PREV_DATABASE_URL is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _PREV_DATABASE_URL
    for name, module in _SAVED_MODULES.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module
        _sync_package_attr(name, module)
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


# ══════════════════════════════════════════════════════════════════════
# The round-off — no database in sight
# ══════════════════════════════════════════════════════════════════════

class RoundUpTests(unittest.TestCase):
    def setUp(self):
        from services import pack_pricing
        self.round = pack_pricing.round_up_nice

    def test_it_rounds_to_the_next_round_number(self):
        self.assertEqual(self.round(925_300), 930_000)
        self.assertEqual(self.round(2_211_700), 2_300_000)
        self.assertEqual(self.round(678), 680)

    def test_it_never_rounds_down(self):
        """Under-pricing the cards inside is the one direction that costs coins."""
        for value in (1, 99, 101, 1_001, 12_345, 999_999, 4_690_100):
            self.assertGreaterEqual(self.round(value), value, value)

    def test_a_number_that_is_already_round_is_left_alone(self):
        for value in (10, 500, 930_000, 2_300_000):
            self.assertEqual(self.round(value), value, value)

    def test_nothing_is_worth_nothing(self):
        self.assertEqual(self.round(0), 0)
        self.assertEqual(self.round(None), 0)
        self.assertEqual(self.round(-5), 0)


class ParseTests(unittest.TestCase):
    def setUp(self):
        from services import pack_pricing
        self.p = pack_pricing

    def test_versions_come_from_a_list_json_or_a_comma_string(self):
        for raw in (["Star", "Legend"], '["Star", "Legend"]', "Star, Legend"):
            self.assertEqual(self.p.parse_versions(raw), ["Star", "Legend"], raw)

    def test_a_version_named_twice_is_listed_once(self):
        self.assertEqual(self.p.parse_versions("Star, star ,Star"), ["Star"])

    def test_weights_of_the_wrong_length_mean_uniform(self):
        """Exactly what the pack itself does with them."""
        self.assertIsNone(self.p.parse_weights("60, 30", 3))
        self.assertEqual(self.p.parse_weights("60, 30, 10", 3), [60, 30, 10])

    def test_weights_that_cannot_pick_anything_mean_uniform(self):
        self.assertIsNone(self.p.parse_weights("0, 0, 0", 3))
        self.assertIsNone(self.p.parse_weights("60, -30, 10", 3))
        self.assertIsNone(self.p.parse_weights("sixty, 30, 10", 3))


# ══════════════════════════════════════════════════════════════════════
# The pricing, against a real catalogue
# ══════════════════════════════════════════════════════════════════════

class PricingCase(unittest.TestCase):
    def setUp(self):
        from database import get_session
        from services import pack_pricing

        self.session = get_session()
        self.p = pack_pricing

    def tearDown(self):
        from models import Player
        # Roll back first: a test that left the session in a failed flush would
        # otherwise take the whole file down with a pool of leaked connections
        # rather than just failing itself.
        self.session.rollback()
        self.session.query(Player).delete()
        self.session.commit()
        self.session.close()

    def card(self, rating, version="Base", *, base=True, active=True,
             career=False, parent_rating=50):
        """One card. ``base=False`` makes it a variant of a card rated
        ``parent_rating`` — kept out of the band under test by default, so the
        parent a variant needs for its foreign key never joins the pool the
        assertion is about."""
        from models import Player
        n = next(_SEQ)
        row = Player(name=f"Card {n}", rating=rating, version=version,
                     category="Batsman", country="India",
                     bat_hand="Right", bowl_hand="Right", bowl_style="Fast",
                     is_active=active, is_career=career)
        self.session.add(row)
        self.session.flush()
        if not base:
            parent = self.card(parent_rating, version="Base")
            row.parent_player_id = parent.id
            self.session.flush()
        return row

    def buy(self, rating):
        from config import get_buy_value
        return get_buy_value(rating)


class MainSlotTests(PricingCase):
    def test_an_even_band_is_the_mean_of_its_ratings(self):
        for rating in (85, 86, 87):
            self.card(rating)
        self.session.commit()
        out = self.p.main_slot_value(self.session, mode="rating", versions=None,
                                     min_rating=85, max_rating=87, weights=None)
        expected = sum(self.buy(r) for r in (85, 86, 87)) / 3
        self.assertEqual(out["value"], round(expected))
        self.assertEqual(out["pool"], 3)

    def test_the_weights_are_the_odds_so_they_price_the_pack(self):
        for rating in (85, 86, 87):
            self.card(rating)
        self.session.commit()
        out = self.p.main_slot_value(self.session, mode="rating", versions=None,
                                     min_rating=85, max_rating=87,
                                     weights="60, 30, 10")
        expected = (self.buy(85) * 0.6 + self.buy(86) * 0.3
                    + self.buy(87) * 0.1)
        self.assertEqual(out["value"], round(expected))

    def test_a_rating_with_no_card_is_dropped_and_reported(self):
        """The pack cannot pull it, so pricing as though it could is a lie."""
        self.card(85)
        self.card(87)
        self.session.commit()
        out = self.p.main_slot_value(self.session, mode="rating", versions=None,
                                     min_rating=85, max_rating=87, weights=None)
        self.assertEqual(out["empty_ratings"], [86])
        self.assertEqual(out["value"],
                         round((self.buy(85) + self.buy(87)) / 2))

    def test_a_version_is_averaged_over_its_cards_not_its_ratings(self):
        """Thirty 85s and one 99 is worth about an 85."""
        for _ in range(30):
            self.card(85, version="Star")
        self.card(99, version="Star")
        self.session.commit()
        out = self.p.main_slot_value(self.session, mode="version",
                                     versions=["Star"],
                                     min_rating=50, max_rating=100, weights=None)
        expected = (self.buy(85) * 30 + self.buy(99)) / 31
        self.assertEqual(out["value"], round(expected))
        self.assertEqual(out["pool"], 31)
        self.assertLess(out["value"], (self.buy(85) + self.buy(99)) / 2)

    def test_version_mode_ignores_the_rating_band(self):
        """It has no rating step — the pack picks uniformly from the version."""
        self.card(99, version="Legend")
        self.session.commit()
        out = self.p.main_slot_value(self.session, mode="version",
                                     versions=["Legend"],
                                     min_rating=50, max_rating=60, weights=None)
        self.assertEqual(out["value"], self.buy(99))

    def test_both_mode_prices_the_intersection(self):
        self.card(91, version="Legend")
        self.card(95, version="Legend")
        self.card(93, version="Base")      # right band, wrong version
        self.session.commit()
        out = self.p.main_slot_value(self.session, mode="both",
                                     versions=["Legend"],
                                     min_rating=91, max_rating=95, weights=None)
        self.assertEqual(out["value"], round((self.buy(91) + self.buy(95)) / 2))
        self.assertEqual(out["pool"], 2)
        self.assertEqual(out["empty_ratings"], [92, 93, 94])

    def test_both_mode_with_no_version_falls_back_to_the_band(self):
        """Because that is exactly what the pack does when the list is empty."""
        self.card(91)
        self.card(92)
        self.session.commit()
        both = self.p.main_slot_value(self.session, mode="both", versions=[],
                                      min_rating=91, max_rating=92, weights=None)
        rating = self.p.main_slot_value(self.session, mode="rating",
                                        versions=None, min_rating=91,
                                        max_rating=92, weights=None)
        self.assertEqual(both["value"], rating["value"])

    def test_a_version_nothing_carries_is_worth_nothing(self):
        self.card(90, version="Base")
        self.session.commit()
        out = self.p.main_slot_value(self.session, mode="version",
                                     versions=["Ghost"],
                                     min_rating=50, max_rating=100, weights=None)
        self.assertEqual(out["value"], 0)
        self.assertEqual(out["pool"], 0)

    def test_inactive_and_career_cards_are_not_in_the_pool(self):
        """A pack can't pull either, so neither may move the price."""
        self.card(90, version="Star")
        self.card(99, version="Star", active=False)
        self.card(99, version="Star", career=True)
        self.session.commit()
        out = self.p.main_slot_value(self.session, mode="version",
                                     versions=["Star"],
                                     min_rating=50, max_rating=100, weights=None)
        self.assertEqual(out["pool"], 1)
        self.assertEqual(out["value"], self.buy(90))

    def test_a_backwards_band_is_read_the_way_round_it_was_meant(self):
        self.card(85)
        self.card(87)
        self.session.commit()
        out = self.p.main_slot_value(self.session, mode="rating", versions=None,
                                     min_rating=87, max_rating=85, weights=None)
        self.assertGreater(out["value"], 0)


class BonusSlotTests(PricingCase):
    def test_the_bonus_band_is_priced_on_the_rating_it_rolls_not_the_card_count(self):
        """``_pick_bonus_player`` rolls a RATING, so that is the weighting.

        Forty 74s and one 80: the pull picks a rating evenly across the band and
        only then a card at it, so the 80 comes up as often as the 74 — not one
        time in forty-one. Averaging over the cards priced this band like a 74
        and handed out 80s.
        """
        for _ in range(40):
            self.card(74)
        self.card(80)
        self.session.commit()
        out = self.p.bonus_slot_value(self.session, 74, 80)
        expected = (self.buy(74) + self.buy(80)) / 2
        self.assertEqual(out["value"], round(expected))
        self.assertEqual(out["pool"], 41)
        # 75-79 hold nothing, so their share of the roll is dropped rather than
        # priced — and reported, because an unfillable band is usually a typo.
        self.assertEqual(out["empty_ratings"], [75, 76, 77, 78, 79])

    def test_bonus_weights_are_the_bonus_odds(self):
        """The bonus slot has carried weights all along; nothing used to set them."""
        for rating in (74, 80):
            self.card(rating)
        self.session.commit()
        even = self.p.bonus_slot_value(self.session, 74, 80)
        # One weight per rating in 74-80, all of it on the 80.
        top = self.p.bonus_slot_value(self.session, 74, 80,
                                      weights="0, 0, 0, 0, 0, 0, 100")
        self.assertEqual(top["value"], self.buy(80))
        self.assertGreater(top["value"], even["value"])

    def test_variants_are_not_bonus_cards(self):
        """Bonus slots draw base cards only."""
        self.card(80)
        self.card(80, base=False)
        self.session.commit()
        self.assertEqual(self.p.bonus_slot_value(self.session, 80, 80)["pool"], 1)

    def test_an_empty_band_is_zero_rather_than_a_crash(self):
        out = self.p.bonus_slot_value(self.session, 74, 80)
        self.assertEqual(out["value"], 0)
        self.assertEqual(out["pool"], 0)


class SuggestionTests(PricingCase):
    def build(self):
        for rating in (85, 86, 87):
            self.card(rating)
        for rating in range(74, 81):
            self.card(rating)
        self.session.commit()

    def test_the_price_is_every_slot_added_up_then_rounded_up(self):
        self.build()
        s = self.p.suggest(self.session, main_filter_mode="rating",
                           main_min_rating=85, main_max_rating=87, main_count=1,
                           main_weights="60, 30, 10",
                           bonus_min_rating=74, bonus_max_rating=80,
                           bonus_count=2)
        self.assertEqual(s["raw_value"],
                         s["main_total"] + s["bonus_total"])
        self.assertEqual(s["main_total"], s["main"]["value"] * 1)
        self.assertEqual(s["bonus_total"], s["bonus"]["value"] * 2)
        self.assertEqual(s["coins"], self.p.round_up_nice(s["raw_value"]))
        self.assertGreaterEqual(s["coins"], s["raw_value"])

    def test_the_other_currencies_come_off_the_coin_figure(self):
        self.build()
        s = self.p.suggest(self.session, main_filter_mode="rating",
                           main_min_rating=85, main_max_rating=87, main_count=1,
                           bonus_min_rating=74, bonus_max_rating=80,
                           bonus_count=2)
        self.assertEqual(
            s["quest_points"],
            self.p.round_up_nice(s["raw_value"] / s["coins_per_quest_point"]))
        self.assertEqual(
            s["gems"], self.p.round_up_nice(s["raw_value"] / s["coins_per_gem"]))

    def test_more_cards_in_the_pack_costs_more(self):
        self.build()
        one = self.p.suggest(self.session, main_filter_mode="rating",
                             main_min_rating=85, main_max_rating=87,
                             main_count=1, bonus_count=0)
        two = self.p.suggest(self.session, main_filter_mode="rating",
                             main_min_rating=85, main_max_rating=87,
                             main_count=2, bonus_count=0)
        self.assertEqual(two["raw_value"], one["raw_value"] * 2)

    def test_a_pack_that_can_pull_nothing_says_so_instead_of_pricing_it(self):
        s = self.p.suggest(self.session, main_filter_mode="rating",
                           main_min_rating=85, main_max_rating=87, main_count=1,
                           bonus_min_rating=74, bonus_max_rating=80,
                           bonus_count=2)
        self.assertEqual(s["raw_value"], 0)
        self.assertEqual(s["coins"], 0)
        self.assertEqual(s["quest_points"], 0)
        self.assertEqual(s["gems"], 0)
        self.assertTrue(any("main slot" in w for w in s["warnings"]))
        self.assertTrue(any("bonus" in w for w in s["warnings"]))

    def test_a_gap_in_the_band_is_flagged_not_swallowed(self):
        self.card(85)
        self.card(87)
        self.session.commit()
        s = self.p.suggest(self.session, main_filter_mode="rating",
                           main_min_rating=85, main_max_rating=87,
                           main_count=1, bonus_count=0)
        self.assertTrue(any("rating 86" in w for w in s["warnings"]),
                        s["warnings"])


class VersionCatalogueTests(PricingCase):
    def test_every_version_is_offered_with_what_picking_it_means(self):
        for _ in range(3):
            self.card(90, version="Star")
        self.card(75, version="Base")
        self.session.commit()
        rows = {r["name"]: r for r in self.p.version_catalogue(self.session)}
        self.assertEqual(rows["Star"]["cards"], 3)
        self.assertEqual(rows["Star"]["avg_value"], self.buy(90))
        self.assertEqual((rows["Star"]["min_rating"], rows["Star"]["max_rating"]),
                         (90, 90))
        self.assertEqual(rows["Base"]["cards"], 1)

    def test_the_canonical_rarities_are_offered_before_any_card_carries_them(self):
        """A pack for a version being prepared shouldn't wait on an upload."""
        names = [r["name"] for r in self.p.version_catalogue(self.session)]
        for rarity in ("Base", "Star", "Legend"):
            self.assertIn(rarity, names)

    def test_the_richest_version_is_listed_first(self):
        self.card(99, version="Legend")
        self.card(60, version="Base")
        self.session.commit()
        rows = self.p.version_catalogue(self.session)
        self.assertEqual(rows[0]["name"], "Legend")

    def test_a_card_a_pack_cannot_pull_is_not_in_the_catalogue(self):
        self.card(99, version="Retired", active=False)
        self.card(99, version="MyCareer", career=True)
        self.session.commit()
        names = [r["name"] for r in self.p.version_catalogue(self.session)]
        self.assertNotIn("Retired", names)
        self.assertNotIn("MyCareer", names)


class VersionFieldTests(PricingCase):
    """The picker posts one value per choice; the old field posted one string."""

    def test_the_picker_posts_a_list(self):
        self.assertEqual(
            self.p.parse_versions_field(self.session, ["Star", "Legend"]),
            ["Star", "Legend"])

    def test_a_legacy_comma_joined_post_still_works(self):
        self.assertEqual(
            self.p.parse_versions_field(self.session, ["Star, Legend"]),
            ["Star", "Legend"])

    def test_a_version_with_a_comma_in_its_name_is_not_torn_in_half(self):
        self.card(90, version="World Cup, Final")
        self.session.commit()
        self.assertEqual(
            self.p.parse_versions_field(self.session, ["World Cup, Final"]),
            ["World Cup, Final"])

    def test_selecting_nothing_clears_the_list(self):
        self.assertEqual(self.p.parse_versions_field(self.session, []), [])


class AdminWiringTests(unittest.TestCase):
    """The form and the routes, checked at source level.

    Importing ``admin`` builds the whole Flask app against a live database —
    much more than assertions about which calls exist need.
    """

    @staticmethod
    def _read(*parts):
        path = os.path.join(os.path.dirname(__file__), "..", *parts)
        with open(path) as fh:
            return fh.read()

    def test_the_version_field_is_a_dropdown_of_real_versions(self):
        html = self._read("templates", "admin_pack_form.html")
        self.assertIn('<select name="main_versions"', html)
        self.assertIn("multiple", html)
        self.assertIn("version_catalogue", html)
        # The old free-text input is gone.
        self.assertNotIn('<input type="text" name="main_versions"', html)

    def test_a_version_the_catalogue_lost_stays_selected(self):
        """Saving the form for an unrelated reason must not drop it."""
        html = self._read("templates", "admin_pack_form.html")
        self.assertIn("known_lower", html)
        self.assertIn("no active cards", html)

    def test_both_form_routes_hand_over_the_catalogue(self):
        admin = self._read("admin.py")
        for route in ("def admin_pack_new():", "def admin_pack_edit(pack_id):"):
            body = admin.split(route)[1].split("\n@app.route")[0]
            self.assertIn("version_catalogue", body, route)

    def test_the_suggestion_endpoint_prices_the_unsaved_form(self):
        admin = self._read("admin.py")
        body = (admin.split("def admin_pack_price_suggestion():")[1]
                .split("\n@app.route")[0])
        for field in ("main_filter_mode", "main_versions", "main_weights",
                      "bonus_count"):
            self.assertIn(field, body)
        self.assertIn("jsonify", body)

    def test_saving_reads_the_multi_select(self):
        admin = self._read("admin.py")
        body = admin.split("def _save_pack_from_form(")[1]
        self.assertIn('parse_versions_field(db, f.getlist("main_versions"))',
                      body)

    def test_applying_a_price_clears_the_other_two_currencies(self):
        """A pack requires every non-zero cost but charges only the first."""
        html = self._read("templates", "admin_pack_form.html")
        self.assertIn("(key === currency) ? amount : 0", html)
        self.assertIn("must hold", html)

    def test_the_odds_are_a_table_of_ratings_not_a_comma_box(self):
        html = self._read("templates", "admin_pack_form.html")
        self.assertIn("_weight_", html)
        self.assertIn("data-prob", html)
        # Keyed by rating, so a band that moved can't slide every weight onto
        # the wrong row.
        self.assertIn('name="{{ slot }}_weight_{{ row.rating }}"', html)
        # The old free-text box is gone.
        self.assertNotIn('name="main_weights"', html)

    def test_the_odds_tables_are_rendered_inside_the_form(self):
        """Outside it they would post nothing, and the live price would be blind
        to the very numbers it is pricing.

        The macro is *defined* above the form, as Jinja requires; what has to
        sit inside it is every call to the macro.
        """
        html = self._read("templates", "admin_pack_form.html")
        opened, closed = html.index('<form method="POST"'), html.index("</form>")
        calls = [i for i in range(len(html))
                 if html.startswith("{{ odds_table(", i)]
        self.assertEqual(len(calls), 2, "one table per slot")
        for at in calls:
            self.assertTrue(opened < at < closed,
                            "an odds table was rendered outside the form")

    def test_saving_writes_both_slots_odds(self):
        admin = self._read("admin.py")
        body = admin.split("def _save_pack_from_form(")[1]
        self.assertIn("pack.main_weights_json = _odds_json_from_form(", body)
        # bonus_weights_json has been on the model all along with nothing
        # writing it — the bonus odds table is the first thing that does.
        self.assertIn("pack.bonus_weights_json = _odds_json_from_form(", body)

    def test_a_version_packs_band_comes_from_its_versions(self):
        """The weight list is positional, so the band has to be the real one."""
        admin = self._read("admin.py")
        body = admin.split("def _save_pack_from_form(")[1]
        self.assertIn("version_span(db, selected)", body)
        self.assertIn("main_span_locked", body)

    def test_the_suggestion_hands_back_the_rows_as_well_as_the_price(self):
        """One round trip per edit — two could disagree with each other."""
        admin = self._read("admin.py")
        body = (admin.split("def admin_pack_price_suggestion():")[1]
                .split("\n@app.route")[0])
        self.assertIn("odds_rows(", body)
        self.assertIn('"odds"', body)

    def test_the_odds_are_not_published_to_players(self):
        """They are an admin tuning tool. A pull nobody can audit is better
        than a pull everybody argues about."""
        packs = self._read("handlers", "packs.py")
        self.assertNotIn("Main odds", packs)
        self.assertNotIn("main_weights_json", packs)


if __name__ == "__main__":
    unittest.main()

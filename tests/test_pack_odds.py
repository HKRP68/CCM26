"""The odds a pack pays out on, and the one place that reads them.

A pack's weights used to be typed into a free-text box as a comma-separated
list, counted by hand against the rating band, and thrown away with a flash
message if the count came out wrong. The form is a table now — one row per
rating, keyed by the rating — and ``services/pack_odds.py`` is what turns that
back into the positional list the pull and the price have always read.

Everything here pins a rule that something downstream depends on:

  • **``None`` means uniform, and every failure means ``None``.** Empty, the
    wrong length, negative, all zeros — the pull falls back to an even roll for
    each of those, so the codec has to agree, or a pack is priced on odds it
    never had.
  • **weights are carried by rating, not by position.** The stored shape is
    positional, but only because the band that goes with it is stored too. A
    map that doesn't fit its band is not "close enough", it is uniform.
  • **a rating with no card can't be pulled**, so the rows say so and the
    simulation never lands on one, whatever weight it carries.
  • **the whole 50-100 band has to fit in the column.** Five decimals across
    fifty-one ratings is what broke ``String(500)``, and a truncated weight
    list reads back as uniform with no error anywhere.
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
# Same reason ``tests/test_pack_pricing.py`` carries this list: leave any of
# these cached from an earlier test file and the queries here run against a
# different ``models.Player`` than the one just re-imported, which SQLAlchemy
# turns into "FROM players, players" and a cartesian product.
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
# The codec — no database in sight
# ══════════════════════════════════════════════════════════════════════

class BandTests(unittest.TestCase):
    def setUp(self):
        from services import pack_odds
        self.o = pack_odds

    def test_a_band_is_every_rating_it_covers(self):
        self.assertEqual(self.o.band(85, 87), [85, 86, 87])

    def test_a_backwards_band_is_read_the_way_round_it_was_meant(self):
        self.assertEqual(self.o.band(87, 85), [85, 86, 87])

    def test_it_never_runs_outside_50_to_100(self):
        self.assertEqual(self.o.band(0, 500)[0], 50)
        self.assertEqual(self.o.band(0, 500)[-1], 100)
        self.assertEqual(len(self.o.band(0, 500)), 51)

    def test_nonsense_is_an_empty_band_rather_than_a_crash(self):
        self.assertEqual(self.o.band(None, "eighty"), [])


class WeightsMapTests(unittest.TestCase):
    def setUp(self):
        from services import pack_odds
        self.o = pack_odds

    def test_the_stored_positional_list_round_trips(self):
        self.assertEqual(self.o.weights_map(85, 87, "[60, 30, 10]"),
                         {85: 60.0, 86: 30.0, 87: 10.0})

    def test_a_rating_keyed_object_is_read_too(self):
        """Nothing writes this yet; reading it keeps the shape swappable."""
        self.assertEqual(self.o.weights_map(85, 87, '{"85": 60, "87": 10}'),
                         {85: 60.0, 86: 0.0, 87: 10.0})

    def test_a_rating_outside_the_band_is_dropped_not_an_error(self):
        self.assertEqual(self.o.weights_map(85, 86, '{"85": 60, "99": 10}'),
                         {85: 60.0, 86: 0.0})

    def test_the_legacy_comma_box_still_parses(self):
        self.assertEqual(self.o.weights_map(85, 87, "60, 30, 10"),
                         {85: 60.0, 86: 30.0, 87: 10.0})

    def test_a_list_of_the_wrong_length_means_uniform(self):
        """Length is the only thing saying which rating each weight is for."""
        self.assertIsNone(self.o.weights_map(85, 87, "[60, 30]"))
        self.assertIsNone(self.o.weights_map(85, 87, "[60, 30, 10, 5]"))

    def test_everything_that_cannot_pick_means_uniform(self):
        for raw in (None, "", "   ", "[0, 0, 0]", "[60, -30, 10]",
                    "[sixty, 30, 10]", "not json at all", "{"):
            self.assertIsNone(self.o.weights_map(85, 87, raw), raw)

    def test_decimals_survive(self):
        self.assertEqual(self.o.weights_map(85, 86, "[99.99999, 0.00001]"),
                         {85: 99.99999, 86: 0.00001})


class WeightsJsonTests(unittest.TestCase):
    def setUp(self):
        from services import pack_odds
        self.o = pack_odds

    def test_a_map_comes_back_as_the_positional_list(self):
        self.assertEqual(self.o.weights_json(85, 87, {85: 60, 86: 30, 87: 10}),
                         "[60, 30, 10]")

    def test_a_rating_left_out_of_the_map_is_a_zero_not_a_gap(self):
        self.assertEqual(self.o.weights_json(85, 87, {85: 60, 87: 10}),
                         "[60, 0, 10]")

    def test_an_all_zero_table_is_stored_as_no_odds_at_all(self):
        """Clearing the table and never having set it have to look identical."""
        self.assertIsNone(self.o.weights_json(85, 87, {85: 0, 86: 0, 87: 0}))
        self.assertIsNone(self.o.weights_json(85, 87, {}))

    def test_a_single_rating_band_stores_nothing(self):
        """One weight over one rating says nothing the band doesn't."""
        self.assertIsNone(self.o.weights_json(90, 90, {90: 100}))

    def test_whole_numbers_stay_whole(self):
        """The seeded packs carry "[60, 30, 10]"; nothing should read 60.0."""
        self.assertNotIn(".", self.o.weights_json(85, 87, {85: 60, 86: 30, 87: 10}))

    def test_what_it_writes_is_what_the_map_reads_back(self):
        stored = self.o.weights_json(85, 88, {85: 12.5, 86: 0, 87: 1, 88: 0.00001})
        self.assertEqual(self.o.weights_map(85, 88, stored),
                         {85: 12.5, 86: 0.0, 87: 1.0, 88: 0.00001})

    def test_the_whole_rating_range_fits_in_the_column(self):
        """A truncated weight list parses as nothing, which reads as uniform —
        a tuned pack would pay flat odds with no error anywhere. This is why
        both weight columns are Text rather than String(500)."""
        from models import Pack
        from sqlalchemy import Text

        worst = {r: 12.34567 for r in range(50, 101)}
        stored = self.o.weights_json(50, 100, worst)
        self.assertGreater(len(stored), 500,
                           "the worst case has stopped being a worst case")
        self.assertEqual(self.o.weights_map(50, 100, stored), worst)
        for column in ("main_weights_json", "bonus_weights_json"):
            self.assertIsInstance(Pack.__table__.c[column].type, Text, column)


# ══════════════════════════════════════════════════════════════════════
# The rows, against a real catalogue
# ══════════════════════════════════════════════════════════════════════

class OddsCase(unittest.TestCase):
    def setUp(self):
        from database import get_session
        from services import pack_odds

        self.session = get_session()
        self.o = pack_odds

    def tearDown(self):
        from models import Player
        self.session.rollback()
        self.session.query(Player).delete()
        self.session.commit()
        self.session.close()

    def card(self, rating, version="Base", *, active=True, career=False):
        from models import Player
        n = next(_SEQ)
        row = Player(name=f"Card {n}", rating=rating, version=version,
                     category="Batsman", country="India", bat_hand="Right",
                     bowl_hand="Right", bowl_style="Fast",
                     is_active=active, is_career=career)
        self.session.add(row)
        self.session.flush()
        return row

    def buy(self, rating):
        from config import get_buy_value
        return get_buy_value(rating)


class VersionSpanTests(OddsCase):
    def test_the_span_is_what_the_version_actually_holds(self):
        for rating in (91, 93, 95):
            self.card(rating, version="Legend")
        self.card(70, version="Base")
        self.session.commit()
        self.assertEqual(self.o.version_span(self.session, ["Legend"]), (91, 95))

    def test_a_retired_card_is_not_part_of_the_span(self):
        self.card(91, version="Legend")
        self.card(99, version="Legend", active=False)
        self.session.commit()
        self.assertEqual(self.o.version_span(self.session, ["Legend"]), (91, 91))

    def test_a_career_card_is_not_part_of_the_span(self):
        self.card(91, version="Legend")
        self.card(99, version="Legend", career=True)
        self.session.commit()
        self.assertEqual(self.o.version_span(self.session, ["Legend"]), (91, 91))

    def test_a_version_nobody_has_a_card_in_has_no_span(self):
        self.session.commit()
        self.assertIsNone(self.o.version_span(self.session, ["Legend"]))
        self.assertIsNone(self.o.version_span(self.session, []))


class OddsRowsTests(OddsCase):
    def test_version_mode_builds_its_rows_from_the_version(self):
        for rating in (91, 92, 94):
            self.card(rating, version="Legend")
        self.session.commit()
        out = self.o.odds_rows(self.session, mode="version", versions=["Legend"],
                               min_rating=70, max_rating=99)
        self.assertEqual((out["min_rating"], out["max_rating"]), (91, 94))
        self.assertEqual(out["span_source"], "versions")
        self.assertEqual([r["rating"] for r in out["rows"]], [94, 93, 92, 91],
                         "the best card belongs at the top")

    def test_a_rating_inside_the_span_with_no_card_is_reported(self):
        self.card(91, version="Legend")
        self.card(94, version="Legend")
        self.session.commit()
        out = self.o.odds_rows(self.session, mode="version", versions=["Legend"],
                               min_rating=70, max_rating=99)
        self.assertEqual(out["empty_ratings"], [93, 92])
        self.assertEqual(out["pool_total"], 2)

    def test_every_row_carries_the_card_value(self):
        self.card(86)
        self.session.commit()
        out = self.o.odds_rows(self.session, mode="rating", versions=None,
                               min_rating=85, max_rating=87)
        by_rating = {r["rating"]: r for r in out["rows"]}
        self.assertEqual(by_rating[86]["buy_value"], self.buy(86))
        self.assertEqual(by_rating[86]["pool"], 1)
        self.assertEqual(by_rating[85]["pool"], 0)

    def test_saved_odds_come_back_against_their_rows(self):
        for rating in (85, 86, 87):
            self.card(rating)
        self.session.commit()
        out = self.o.odds_rows(self.session, mode="rating", versions=None,
                               min_rating=85, max_rating=87,
                               weights_raw="[60, 30, 10]")
        self.assertFalse(out["uniform"])
        self.assertEqual({r["rating"]: r["weight"] for r in out["rows"]},
                         {85: 60.0, 86: 30.0, 87: 10.0})

    def test_a_hand_narrowed_version_band_is_not_widened_back(self):
        """Odds that fit the band they came with describe that band.

        Widening a narrowed version pack back to the version's full span would
        slide every weight onto the wrong rating — a positional list only means
        anything next to the band it was written for.
        """
        for rating in (91, 92, 93, 94, 95):
            self.card(rating, version="Legend")
        self.session.commit()
        out = self.o.odds_rows(self.session, mode="version", versions=["Legend"],
                               min_rating=93, max_rating=95,
                               weights_raw="[70, 20, 10]")
        self.assertEqual((out["min_rating"], out["max_rating"]), (93, 95))
        self.assertEqual(out["span_source"], "inputs")
        self.assertEqual({r["rating"]: r["weight"] for r in out["rows"]},
                         {93: 70.0, 94: 20.0, 95: 10.0})

    def test_the_bonus_slot_only_ever_sees_base_cards(self):
        from models import Player
        base = self.card(80)
        variant = self.card(80, version="Legend")
        variant.parent_player_id = base.id
        self.session.commit()
        out = self.o.odds_rows(self.session, mode="rating", versions=None,
                               min_rating=80, max_rating=80, slot="bonus")
        self.assertEqual(out["rows"][0]["pool"], 1)
        self.assertEqual(self.session.query(Player).count(), 2)


class SimulateTests(unittest.TestCase):
    def setUp(self):
        from services import pack_odds
        self.o = pack_odds

    def test_it_follows_the_weights(self):
        counts = {85: 5, 86: 5, 87: 5}
        tally = self.o.simulate(counts, 85, 87, "[98, 1, 1]", draws=4000)
        self.assertGreater(tally[85], 3000)
        self.assertEqual(sum(tally.values()), 4000)

    def test_a_rating_with_no_card_never_comes_up(self):
        """However it is weighted — which is the whole point of simulating."""
        counts = {85: 5, 86: 0, 87: 5}
        tally = self.o.simulate(counts, 85, 87, "[10, 80, 10]", draws=2000)
        self.assertEqual(tally[86], 0)
        self.assertEqual(tally[85] + tally[87], 2000)

    def test_no_weights_is_an_even_roll(self):
        counts = {85: 1, 86: 1, 87: 1}
        tally = self.o.simulate(counts, 85, 87, None, draws=6000)
        for rating in (85, 86, 87):
            self.assertGreater(tally[rating], 1500, rating)

    def test_a_slot_that_can_pull_nothing_simulates_nothing(self):
        self.assertEqual(self.o.simulate({}, 85, 87, None, draws=100), {})
        self.assertEqual(self.o.simulate({85: 1}, 85, 87, None, draws=0), {})


if __name__ == "__main__":
    unittest.main()

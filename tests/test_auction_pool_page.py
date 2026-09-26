"""The Franchise Auction's pool page, driven through the Flask admin app.

What is pinned here:

  • **Preview always says what it found.** The results block used to hang off the
    row list alone, so a filter matching nobody rendered nothing at all — no
    table, no count, no message — which is indistinguishable from a dead button.
    Every filter box narrows, so filling them all in at once is the ordinary way
    to match nobody.
  • **The page comes back to the pool card.** The filter form carries a fragment,
    because the card sits fifteen cards down and a GET reload otherwise lands at
    the top with the results below the fold.
  • **A card whose ``is_active`` is NULL is still a card.** ``is_active == True``
    does not match NULL in SQL, so those rows were dropped from the pool builder
    in silence while the Players page still listed them.
  • **Sets are numbered, orderable, and a number cannot be given twice** — the
    website half of the Set No rules, over HTTP rather than in the service.
"""

import itertools
import os
import re
import sys
import tempfile
import unittest

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None

# Everything that caches a reference to ``database`` / ``models``, for the reason
# tests/test_auction_bidding.py gives at length. ``admin`` is in the list because
# it binds ``get_session`` at import time.
_MODULE_NAMES = ("database", "models", "config", "admin",
                 "services.player_service", "services.player_query",
                 "services.auction_service", "services.retention_negotiation",
                 "services.auction_sets_io", "services.auction_scheduler",
                 "services.auction_rich", "handlers.auction")

_PID = itertools.count(1)


def _unload(names):
    for name in names:
        sys.modules.pop(name, None)
        parent, _, child = name.rpartition(".")
        package = sys.modules.get(parent) if parent else None
        if package is not None:
            try:
                delattr(package, child)
            except AttributeError:
                pass


def _restore(saved):
    for name, module in saved.items():
        parent, _, child = name.rpartition(".")
        if module is None:
            _unload([name])
            continue
        sys.modules[name] = module
        package = sys.modules.get(parent) if parent else None
        if package is not None:
            setattr(package, child, module)


def setUpModule():
    global _PREV_DATABASE_URL, _SAVED_MODULES, _TMP, _ENGINE

    _PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
    _SAVED_MODULES = {name: sys.modules.get(name) for name in _MODULE_NAMES}
    _unload(_MODULE_NAMES)

    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"
    os.environ.setdefault("BOT_TOKEN", "test-token")
    os.environ.setdefault("ADMIN_PASSWORD", "test")
    os.environ.setdefault("ADMIN_USERNAME", "admin")

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
    _restore(_SAVED_MODULES)
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


PLAYER_DEFAULTS = dict(category="Batsman", country="India", version="Base",
                       bat_hand="Right", bowl_hand="Right",
                       bowl_style="Medium Pacer", bat_rating=80,
                       bowl_rating=40)

EMPTY_STATE = "No catalogue card matches that filter"


class PoolPageCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            import admin
        except Exception as exc:            # pragma: no cover - env-dependent
            raise unittest.SkipTest(f"admin app unavailable: {exc}")
        admin.app.config["TESTING"] = True
        # The forms under test are POSTed directly rather than scraped for their
        # token; CSRF itself is covered where it is implemented.
        admin.app.config["WTF_CSRF_ENABLED"] = False
        cls.admin = admin

    def setUp(self):
        from database import get_session
        from models import Player
        from services import auction_service as A

        self.session = get_session()
        self.A = A
        self.tag = next(_PID)
        self.players = []
        for offset, rating in enumerate((97, 93, 88, 84, 75)):
            player = Player(name=f"Pool {self.tag} {offset}", rating=rating,
                            is_active=True, **PLAYER_DEFAULTS)
            self.session.add(player)
            self.players.append(player)
        self.session.flush()
        self.season = A.create_season(self.session, f"Pool page {self.tag}")
        self.session.commit()
        self.season_id = self.season.id

        self.client = self.admin.app.test_client()
        with self.client.session_transaction() as flask_session:
            flask_session["admin"] = True

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    # ── helpers ──

    def get(self, query=""):
        response = self.client.get(f"/auctions/{self.season_id}{query}")
        self.assertEqual(200, response.status_code)
        return response.get_data(as_text=True)

    def post(self, data, follow=True):
        response = self.client.post(f"/auctions/{self.season_id}", data=data,
                                    follow_redirects=follow)
        return response

    def preview_count(self, query):
        """How many cards the preview says it found — 0 for the empty state.

        Read off the page rather than asserted on a name, because the filter box
        echoes ``q`` back into itself: a name typed into the search is on the
        page whether or not it matched anything.
        """
        body = self.get(query)
        match = re.search(r"(\d+) match; showing the first \d+", body)
        if match is None:
            self.assertIn(EMPTY_STATE, body)
            self.assertNotIn("Add ticked", body)
            return 0
        self.assertIn("Add ticked", body)
        self.assertNotIn(EMPTY_STATE, body)
        return int(match.group(1))

    def sets(self):
        self.session.expire_all()
        return [(e["set_no"], e["name"])
                for e in self.A.list_sets(self.session, self.season)]

    def lot_nos(self):
        self.session.expire_all()
        return sorted(lot.lot_no for lot in
                      self.A.lots(self.session, self.season.id))


class PreviewTests(PoolPageCase):

    def test_a_filter_that_matches_nobody_says_so(self):
        # The reported bug, in one request: every box filled in, nothing matched,
        # and the page said nothing whatsoever.
        body = self.get("?preview=1&q=NoSuchCricketerAnywhere"
                        "&rating_min=80&rating_max=100&country=India"
                        "&role=Batsman&version_mode=base")
        self.assertIn(EMPTY_STATE, body)
        self.assertNotIn("Add ticked", body)

    def test_a_filter_that_matches_shows_the_rows_and_the_count(self):
        # Scoped by name to this test's own cards: the catalogue is shared by
        # every test in the module, so a bare rating band counts the others too.
        self.assertEqual(3, self.preview_count(          # 97, 93 and 88
            f"?preview=1&q=Pool+{self.tag}+&rating_min=85"))

    def test_the_page_says_nothing_about_a_preview_nobody_asked_for(self):
        body = self.get()
        self.assertNotIn(EMPTY_STATE, body)
        self.assertNotIn("Add ticked", body)

    def test_the_filter_form_comes_back_to_the_pool_card(self):
        # The builder now sits inside the one Auction pool card rather than in
        # a card of its own, so the filter form comes back to the builder's own
        # anchor — which is a finer landing than the card's top, and still
        # inside it. Both ids have to be on the page or the fragment lands on
        # nothing and Preview reads as a dead button.
        body = self.get()
        self.assertIn('id="pool"', body)
        self.assertIn('id="builder"', body)
        self.assertIn(f'/auctions/{self.season_id}#builder', body)
        self.assertLess(body.index('id="pool"'), body.index('id="builder"'))

    def test_a_card_with_a_null_is_active_is_still_offered(self):
        from sqlalchemy import text
        from models import Player
        legacy = Player(name=f"Legacy {self.tag}", rating=90, is_active=True,
                        **PLAYER_DEFAULTS)
        self.session.add(legacy)
        self.session.commit()
        # As a row written around the ORM's default leaves it.
        self.session.execute(text("UPDATE players SET is_active = NULL "
                                  "WHERE id = :id"), {"id": legacy.id})
        self.session.commit()
        # Asserted on the count, not on the name: the filter box echoes ``q``
        # back into itself, so the name is on the page either way.
        self.assertEqual(1, self.preview_count(f"?preview=1&q=Legacy+{self.tag}"))

    def test_a_deactivated_card_is_still_kept_out(self):
        self.players[0].is_active = False
        self.session.commit()
        name = self.players[0].name.replace(" ", "+")
        self.assertEqual(0, self.preview_count(f"?preview=1&q={name}"))


class SetsCardPageTests(PoolPageCase):

    def add_set(self, players, name, set_no=None):
        data = {"action": "add_pool", "set_name": name,
                "player_ids": [str(p.id) for p in players]}
        if set_no is not None:
            data["set_no"] = str(set_no)
        return self.post(data)

    def test_the_card_renders_with_its_numbers(self):
        self.add_set(self.players[:2], "Marquee")
        body = self.get()
        self.assertIn('id="sets"', body)
        self.assertIn("Save order", body)
        self.assertIn("Marquee", body)
        self.assertEqual([(1, "Marquee")], self.sets())

    def test_a_set_can_be_added_at_a_chosen_number(self):
        self.add_set(self.players[:2], "Marquee")
        self.add_set(self.players[2:4], "Uncapped")
        self.assertEqual([(1, "Marquee"), (2, "Uncapped")], self.sets())
        # The newcomer takes number 1 and pushes the rest down, keeping their
        # order rather than reshuffling them.
        self.add_set(self.players[4:], "Openers", set_no=1)
        self.assertEqual([(1, "Openers"), (2, "Marquee"), (3, "Uncapped")],
                         self.sets())

    def test_a_row_button_moves_the_set_it_sits_in(self):
        self.add_set(self.players[:2], "Marquee")
        self.add_set(self.players[2:4], "Uncapped")
        self.add_set(self.players[4:], "Spares")
        # Row index 2 is Spares — the button carries its index, which is the only
        # way one click can say both "up" and "which".
        self.post({"sets_card": "1", "move_up": "2",
                   "set_name": ["Marquee", "Uncapped", "Spares"],
                   "set_no": ["1", "2", "3"]})
        self.assertEqual([(1, "Marquee"), (2, "Spares"), (3, "Uncapped")],
                         self.sets())

    def test_retyped_numbers_reorder_the_queue(self):
        self.add_set(self.players[:2], "Marquee")
        self.add_set(self.players[2:4], "Uncapped")
        self.add_set(self.players[4:], "Spares")
        self.post({"sets_card": "1", "action": "set_positions",
                   "set_name": ["Marquee", "Uncapped", "Spares"],
                   "set_no": ["3", "1", "2"]})
        self.assertEqual([(1, "Uncapped"), (2, "Spares"), (3, "Marquee")],
                         self.sets())
        # Nothing has been bid on, so the lot numbers stay readable.
        self.assertEqual([1, 2, 3, 4, 5], self.lot_nos())

    def test_a_number_already_given_is_refused_and_nothing_moves(self):
        self.add_set(self.players[:2], "Marquee")
        self.add_set(self.players[2:4], "Uncapped")
        before = self.sets()
        response = self.post({"sets_card": "1", "action": "set_positions",
                              "set_name": ["Marquee", "Uncapped"],
                              "set_no": ["1", "1"]})
        body = response.get_data(as_text=True)
        self.assertIn("Marquee", body)
        self.assertIn("different number", body)
        self.assertEqual(before, self.sets())

    def test_a_rating_band_can_be_added_from_the_page(self):
        response = self.post({"sets_card": "1", "action": "add_set_range",
                              "range": "85-100", "set_name": "Top order"})
        self.assertIn("Top order", response.get_data(as_text=True))
        self.assertEqual([(1, "Top order")], self.sets())

    def test_re_adding_players_already_pooled_is_a_no_op_not_an_error(self):
        # Adding is idempotent, so a filter re-run over the same players adds
        # nobody and the new set never reaches the queue. Asking to place it at a
        # number must not then refuse a request that did no harm.
        self.add_set(self.players[:2], "Marquee")
        response = self.add_set(self.players[:2], "Marquee again", set_no=1)
        body = response.get_data(as_text=True)
        self.assertNotIn("nobody waiting", body)
        self.assertEqual([(1, "Marquee")], self.sets())

    def test_a_band_nobody_is_rated_in_is_refused_in_words(self):
        response = self.post({"sets_card": "1", "action": "add_set_range",
                              "range": "not a band"})
        self.assertIn("85-90", response.get_data(as_text=True))
        self.assertEqual([], self.sets())

    def test_reordering_says_nothing_to_the_room_from_the_setup_page(self):
        from models import AuctionEvent
        self.add_set(self.players[:2], "Marquee")
        self.add_set(self.players[2:], "Uncapped")
        self.post({"sets_card": "1", "action": "set_positions",
                   "set_name": ["Marquee", "Uncapped"],
                   "set_no": ["2", "1"]})
        self.session.expire_all()
        # Every event is announced to the auction's group, and the setup page is
        # where an admin nudges the order half a dozen times before starting.
        self.assertEqual(0, self.session.query(AuctionEvent)
                         .filter(AuctionEvent.season_id == self.season_id,
                                 AuctionEvent.kind == "set_order").count())

    def test_the_pool_table_shows_which_set_each_lot_is_in(self):
        self.add_set(self.players[:2], "Marquee")
        body = self.get()
        # The set name had no home on this page at all before — an admin who
        # typed one could not see it anywhere.
        self.assertIn("<th>Set</th>", body)
        self.assertIn("Marquee", body)


# ══════════════════════════════════════════════════════════════════════
# Base prices, typed as ranges
#
# The ladder used to be a column of "rating at least" rows whose upper bound
# was whatever the row above started at. That is not how a room talks about
# it — "96 to 92 start at ₹2 Cr" is — and it meant inserting a band in the
# middle silently re-cut the two around it. Rows are now explicit ranges.
#
# What is pinned here:
#
#   • a ladder saved the old way prices every rating exactly as it did;
#   • a range typed top-first is stored and read back as one;
#   • a rating no band covers is reported, not silently built at the floor.
# ══════════════════════════════════════════════════════════════════════

class BasePriceRangeTests(PoolPageCase):

    def rules(self):
        self.session.expire_all()
        return self.A.base_price_rules(self.season)

    def test_a_ladder_saved_before_ranges_prices_exactly_as_it_did(self):
        """The old shape carried its top implicitly: each band ran up to just
        under the band above. Nothing about that may move."""
        import json
        self.season.base_price_rules_json = json.dumps(
            [{"min_rating": 90, "base_lakh": 200},
             {"min_rating": 80, "base_lakh": 50},
             {"min_rating": 0, "base_lakh": 20}])
        self.session.commit()
        for rating, expected in ((99, 200), (90, 200), (89, 50), (80, 50),
                                 (79, 20), (0, 20)):
            self.assertEqual(expected,
                             self.A.base_price_for(self.season, rating),
                             f"{rating} OVR")
        self.assertEqual([], self.A.base_price_gaps(self.season))
        # And the derived tops are what the page will show.
        self.assertEqual(["90+", "89-80", "79-0"],
                         [self.A.render_rating_band(r) for r in self.rules()])

    def test_a_range_typed_on_the_page_is_stored_and_read_back(self):
        self.post({"action": "price_rules",
                   "rule_max_rating": ["", "96", "91"],
                   "rule_min_rating": ["97", "92", "0"],
                   "rule_price": ["4", "2", "0.2"]})
        self.assertEqual(["97+", "96-92", "91-0"],
                         [self.A.render_rating_band(r) for r in self.rules()])
        for rating, expected in ((99, 400), (97, 400), (96, 200), (92, 200),
                                 (91, 20), (0, 20)):
            self.assertEqual(expected,
                             self.A.base_price_for(self.season, rating),
                             f"{rating} OVR")

    def test_a_band_added_in_the_middle_leaves_its_neighbours_alone(self):
        """The whole point of an explicit top. Under the old shape, adding
        88-85 re-cut the band above it without being asked to."""
        self.post({"action": "price_rules",
                   "rule_max_rating": ["", "96"],
                   "rule_min_rating": ["97", "92"],
                   "rule_price": ["4", "2"]})
        self.post({"action": "price_rules",
                   "rule_max_rating": ["", "96", "91"],
                   "rule_min_rating": ["97", "92", "0"],
                   "rule_price": ["4", "2", "0.5"]})
        self.session.expire_all()
        self.assertEqual(200, self.A.base_price_for(self.season, 92))
        self.assertEqual(400, self.A.base_price_for(self.season, 97))
        self.assertEqual(50, self.A.base_price_for(self.season, 91))

    def test_a_range_typed_backwards_is_read_as_the_slip_it_is(self):
        self.post({"action": "price_rules",
                   "rule_max_rating": ["92"],
                   "rule_min_rating": ["96"],
                   "rule_price": ["2"]})
        self.session.expire_all()
        self.assertEqual(["96-92"],
                         [self.A.render_rating_band(r) for r in self.rules()])
        self.assertEqual(200, self.A.base_price_for(self.season, 94))

    def test_a_rating_no_band_covers_is_named_rather_than_left_silent(self):
        response = self.post({"action": "price_rules",
                              "rule_max_rating": ["", "96"],
                              "rule_min_rating": ["97", "92"],
                              "rule_price": ["4", "2"]})
        body = response.get_data(as_text=True)
        self.assertIn("No band covers", body)
        self.assertIn("91-1", body)
        self.session.expire_all()
        self.assertEqual([(1, 91)], self.A.base_price_gaps(self.season))
        # It is a warning, not a refusal — the ladder still saved.
        self.assertEqual(200, self.A.base_price_for(self.season, 92))
        # …and the uncovered cards fall to the floor, which is what it said.
        self.assertEqual(self.A.DEFAULT_MIN_BASE_PRICE_LAKH,
                         self.A.base_price_for(self.season, 50))

    def test_the_card_shows_every_range_and_an_add_button(self):
        body = self.get()
        self.assertIn("Add range", body)
        self.assertIn("rule_max_rating", body)
        self.assertIn("Rating from", body)

    def test_an_empty_ladder_is_refused_rather_than_saved(self):
        before = self.season.base_price_rules_json
        self.post({"action": "price_rules", "rule_max_rating": [""],
                   "rule_min_rating": [""], "rule_price": [""]})
        self.session.expire_all()
        self.assertEqual(before, self.season.base_price_rules_json)


class SetDeleteTests(PoolPageCase):
    """Deleting a set, and emptying the pool — over HTTP.

    The destructive half of the Sets card. Two things are pinned: what a delete
    actually removes (only what is still waiting, never the record of what
    already happened), and that a click alone cannot do it.
    """

    def add_set(self, players, name):
        return self.post({"action": "add_pool", "set_name": name,
                          "player_ids": [str(p.id) for p in players]})

    def names(self):
        self.session.expire_all()
        return sorted(lot.name for lot in
                      self.A.lots(self.session, self.season.id))

    def test_a_set_deletes_with_its_name_typed_back(self):
        self.add_set(self.players[:2], "Marquee")
        self.add_set(self.players[2:], "Uncapped")
        response = self.post({"sets_card": "1", "drop_set": "0",
                              "set_name": ["Marquee", "Uncapped"],
                              "set_no": ["1", "2"],
                              "confirm_set": ["Marquee", ""]})
        self.assertIn("Marquee deleted", response.get_data(as_text=True))
        self.assertEqual([(1, "Uncapped")], self.sets())
        self.assertEqual(3, len(self.names()))

    def test_the_typed_name_has_to_match_and_nothing_goes_without_it(self):
        self.add_set(self.players[:2], "Marquee")
        before = self.names()
        response = self.post({"sets_card": "1", "drop_set": "0",
                              "set_name": ["Marquee"], "set_no": ["1"],
                              "confirm_set": [""]})
        self.assertIn("Type", response.get_data(as_text=True))
        self.assertEqual(before, self.names())
        self.assertEqual([(1, "Marquee")], self.sets())

    def test_the_confirm_box_is_read_at_the_row_the_button_names(self):
        # Three rows, three confirm boxes, and the second one typed. A server
        # reading ``confirm_set`` as a single value would read the FIRST box —
        # which is blank — and refuse a delete the admin correctly confirmed.
        self.add_set(self.players[:2], "Marquee")
        self.add_set(self.players[2:4], "Uncapped")
        self.add_set(self.players[4:], "Spares")
        self.post({"sets_card": "1", "drop_set": "1",
                   "set_name": ["Marquee", "Uncapped", "Spares"],
                   "set_no": ["1", "2", "3"],
                   "confirm_set": ["", "Uncapped", ""]})
        self.assertEqual([(1, "Marquee"), (2, "Spares")], self.sets())

    def test_a_deleted_set_leaves_what_already_happened_alone(self):
        self.add_set(self.players[:3], "Marquee")
        self.session.expire_all()
        # One of them has already been sold: the set is half run.
        lots = self.A.queued_lots(self.session, self.season, set_name="Marquee")
        sold = lots[0]
        sold.status = self.A.LOT_SOLD
        sold.sold_price_lakh = 100
        self.session.commit()

        self.post({"sets_card": "1", "drop_set": "0",
                   "set_name": ["Marquee"], "set_no": ["1"],
                   "confirm_set": ["Marquee"]})
        self.session.expire_all()
        remaining = self.A.lots(self.session, self.season.id)
        self.assertEqual([sold.name], [lot.name for lot in remaining])
        # The set is still there, finished, rather than gone with its record.
        self.assertEqual([(1, "Marquee")], self.sets())

    def test_emptying_the_pool_needs_the_word_and_then_takes_everything(self):
        self.add_set(self.players[:2], "Marquee")
        self.add_set(self.players[2:], "Uncapped")
        refused = self.post({"action": "pool_clear", "confirm_clear": "yes"})
        self.assertIn("Type EMPTY", refused.get_data(as_text=True))
        self.assertEqual(5, len(self.names()))

        self.post({"action": "pool_clear", "confirm_clear": "EMPTY"})
        self.assertEqual([], self.names())
        self.assertEqual([], self.sets())

    def test_the_card_offers_a_delete_per_set(self):
        self.add_set(self.players[:2], "Marquee")
        body = self.get()
        self.assertIn('name="drop_set"', body)
        self.assertIn('name="confirm_set"', body)
        self.assertIn("Empty the whole pool", body)


class SquadRulesPageTests(PoolPageCase):
    """The role minimum/maximum form, and what it refuses."""

    def save(self, mins, maxes, **extra):
        data = {"action": "squad_rules",
                "min_squad_size": str(extra.pop("min_squad", 3)),
                "max_squad_size": str(extra.pop("max_squad", 11)),
                "home_country": "India", "max_overseas": "8"}
        for index, role in enumerate(self.A.SQUAD_ROLES):
            data[f"role_min_{index}"] = str(mins.get(role, "") or "")
            high = maxes.get(role, "")
            data[f"role_max_{index}"] = "" if high is None else str(high)
        data.update(extra)
        return self.post(data)

    def test_both_ends_save_and_a_typed_zero_survives(self):
        self.save({"Bowler": 3}, {"Bowler": 5, "Wicket Keeper": 0})
        self.session.expire_all()
        self.assertEqual({"Bowler": 3},
                         self.A.role_minimums(self.season))
        # A blank max is "no ceiling"; a typed 0 is a real rule and must not be
        # mistaken for one. That distinction is the whole reason the map is
        # sparse rather than one entry per role.
        self.assertEqual({"Bowler": 5, "Wicket Keeper": 0},
                         self.A.role_maximums(self.season))
        self.assertNotIn("Batsman", self.A.role_maximums(self.season))

    def test_a_minimum_above_its_own_maximum_is_refused(self):
        response = self.save({"Bowler": 6}, {"Bowler": 4})
        self.assertIn("above the maximum", response.get_data(as_text=True))
        self.session.expire_all()
        self.assertEqual({}, self.A.role_minimums(self.season))
        self.assertEqual({}, self.A.role_maximums(self.season))

    def test_minimums_that_cannot_fit_the_squad_are_refused(self):
        response = self.save({"Batsman": 5, "Bowler": 5, "All-rounder": 5},
                             {}, max_squad=11)
        self.assertIn("more than the squad limit",
                      response.get_data(as_text=True))
        self.session.expire_all()
        self.assertEqual({}, self.A.role_minimums(self.season))

    def test_maximums_that_cannot_fill_the_squad_are_refused(self):
        response = self.save({}, {"Batsman": 2, "Bowler": 2,
                                  "All-rounder": 1, "Wicket Keeper": 1},
                             min_squad=11, max_squad=11)
        self.assertIn("fewer than the minimum squad size",
                      response.get_data(as_text=True))
        self.session.expire_all()
        self.assertEqual({}, self.A.role_maximums(self.season))

    def test_the_card_renders_a_row_per_role(self):
        body = self.get()
        for index, role in enumerate(self.A.SQUAD_ROLES):
            self.assertIn(f'name="role_min_{index}"', body)
            self.assertIn(f'name="role_max_{index}"', body)
            self.assertIn(role, body)


class FranchiseFileTests(PoolPageCase):
    """The field, downloaded and uploaded."""

    def field(self):
        self.session.expire_all()
        return self.A.franchises(self.session, self.season.id)

    def add(self, name, **fields):
        franchise = self.A.create_franchise(self.session, self.season, name,
                                            quiet=True, **fields)
        self.session.commit()
        return franchise

    def download(self):
        import json as _json
        response = self.client.get(
            f"/auctions/{self.season_id}/franchises.json")
        self.assertEqual(200, response.status_code)
        self.assertIn("attachment",
                      response.headers.get("Content-Disposition", ""))
        return _json.loads(response.get_data(as_text=True))

    def upload(self, payload, **extra):
        import json as _json
        data = {"action": "franchise_import",
                "franchise_json": _json.dumps(payload)}
        data.update(extra)
        return self.post(data)

    def test_the_download_carries_the_rules_and_not_the_score(self):
        self.add("Mumbai", short_name="MI", city="Mumbai", owner_tg_id=111,
                 owner_name="Aarav")
        payload = self.download()
        self.assertEqual("franchise-auction/franchises", payload["format"])
        row = payload["franchises"][0]
        self.assertEqual("Mumbai", row["name"])
        self.assertEqual(111, row["owner_tg_id"])
        self.assertIn("purse_total_lakh", row)
        # The caches and the results are what an import must never write, so
        # they are not in the file to be written from.
        self.assertNotIn("purse_remaining_lakh", row)
        self.assertNotIn("squad_size", row)
        self.assertEqual([], row["squad"])

    def test_a_download_reimports_into_the_same_field(self):
        self.add("Mumbai", short_name="MI", owner_tg_id=111)
        self.add("Chennai", short_name="CSK", owner_tg_id=222)
        payload = self.download()
        # Into a second auction, which is the case the file exists for.
        other = self.A.create_season(self.session, f"Next {self.tag}")
        self.session.commit()
        added, updated, removed = self.A.import_franchises(
            self.session, other, payload)
        self.session.commit()
        self.assertEqual((2, 0, []), (added, updated, removed))
        # The file's own order is what the new field comes out in — a franchise
        # with no sort order of its own takes the row it was listed in, so the
        # expansion pick order does not become alphabetical by accident.
        exported = [row["name"] for row in payload["franchises"]]
        names = [f.name for f in self.A.franchises(self.session, other.id)]
        self.assertEqual(exported, names)
        self.assertEqual({"Mumbai", "Chennai"}, set(names))
        rebuilt = {f.name: f for f in self.A.franchises(self.session, other.id)}
        self.assertEqual("MI", rebuilt["Mumbai"].short_name)
        self.assertEqual(222, rebuilt["Chennai"].owner_tg_id)

    def test_uploading_the_same_file_twice_edits_rather_than_doubles(self):
        self.upload({"franchises": [{"name": "Mumbai", "owner_tg_id": 111}]})
        self.assertEqual(["Mumbai"], [f.name for f in self.field()])
        response = self.upload({"franchises": [
            {"name": "mumbai", "owner_tg_id": 999, "city": "Wankhede"}]})
        self.assertIn("1 updated", response.get_data(as_text=True))
        field = self.field()
        self.assertEqual(1, len(field))
        self.assertEqual(999, field[0].owner_tg_id)
        self.assertEqual("Wankhede", field[0].city)
        # Matched by name, so the name it already had is what it keeps.
        self.assertEqual("Mumbai", field[0].name)

    def test_a_changed_purse_goes_through_the_ledger(self):
        self.upload({"franchises": [{"name": "Mumbai",
                                     "purse_total_lakh": 10000}]})
        self.upload({"franchises": [{"name": "Mumbai",
                                     "purse_total_lakh": 12000}]})
        franchise = self.field()[0]
        self.assertEqual(12000, franchise.purse_total_lakh)
        self.assertEqual(12000, franchise.purse_remaining_lakh)
        # The purse and the sum of its ledger rows must agree, which is exactly
        # what a column written over without a row would break.
        self.assertEqual(12000,
                         self.A.ledger_total(self.session, franchise.id))
        self.assertEqual([], self.A.reconcile_purses(self.session, self.season))

    def test_keys_the_file_leaves_out_are_left_alone(self):
        self.upload({"franchises": [{"name": "Mumbai", "city": "Wankhede",
                                     "owner_name": "Aarav"}]})
        self.upload({"franchises": [{"name": "Mumbai", "owner_tg_id": 111}]})
        franchise = self.field()[0]
        self.assertEqual("Wankhede", franchise.city)
        self.assertEqual("Aarav", franchise.owner_name)
        self.assertEqual(111, franchise.owner_tg_id)

    def test_nothing_is_removed_unless_the_box_is_ticked(self):
        self.add("Mumbai")
        self.add("Chennai")
        self.upload({"franchises": [{"name": "Mumbai"}]})
        self.assertEqual({"Mumbai", "Chennai"},
                         {f.name for f in self.field()})
        self.upload({"franchises": [{"name": "Mumbai"}]},
                    replace_missing="1")
        self.assertEqual(["Mumbai"], [f.name for f in self.field()])

    def test_a_broken_file_is_refused_by_name(self):
        for payload, expected in (
                ("not json at all", "not valid JSON"),
                ('{"franchises": []}', "lists no franchises"),
                ('{"teams": []}', "no “franchises” list"),
                ('{"franchises": [{"city": "Mumbai"}]}', "has no name"),
                ('{"franchises": [{"name": "A"}, {"name": "a"}]}',
                 "listed twice")):
            with self.subTest(payload=payload):
                response = self.post({"action": "franchise_import",
                                      "franchise_json": payload})
                self.assertIn(expected, response.get_data(as_text=True))
                self.assertEqual([], self.field())

    def test_the_card_offers_both_halves(self):
        body = self.get()
        self.assertIn(f"/auctions/{self.season_id}/franchises.json", body)
        self.assertIn('name="franchise_file"', body)
        self.assertIn('name="replace_missing"', body)


class FranchiseDeleteTests(PoolPageCase):
    """A franchise is removed only when its name is typed back."""

    def field(self):
        self.session.expire_all()
        return self.A.franchises(self.session, self.season.id)

    def setUp(self):
        super().setUp()
        self.A.create_franchise(self.session, self.season, "Mumbai", quiet=True)
        self.session.commit()

    def test_the_wrong_name_changes_nothing(self):
        franchise = self.field()[0]
        response = self.post({"action": "franchise_delete",
                              "franchise_id": str(franchise.id),
                              "confirm_name": "Chennai"})
        self.assertIn("exactly to remove it", response.get_data(as_text=True))
        self.assertEqual(["Mumbai"], [f.name for f in self.field()])

    def test_the_right_name_removes_it(self):
        franchise = self.field()[0]
        self.post({"action": "franchise_delete",
                   "franchise_id": str(franchise.id),
                   "confirm_name": "mumbai"})
        self.assertEqual([], self.field())

    def test_the_page_asks_for_the_name(self):
        body = self.get()
        self.assertIn('name="confirm_name"', body)
        self.assertIn("Remove this franchise", body)


class PlayerControlsAndIncrementsTests(PoolPageCase):
    """The website's side of /aincrement, /aunsold <list>, /areinstate and
    /aforce — the same service calls, from the setup page and the console."""

    def setUp(self):
        super().setUp()
        self.A.add_players_to_pool(self.session, self.season, self.players)
        self.session.commit()

    def lots(self):
        self.session.expire_all()
        return self.A.lots(self.session, self.season_id)

    def test_the_increment_ladder_is_saved_from_the_page(self):
        body = self.get()
        self.assertIn('name="inc_step"', body)
        self.post({"action": "increment_rules",
                   "inc_upto": ["2", "5", ""],
                   "inc_step": ["10L", "20L", "50L"]})
        self.session.expire_all()
        season = self.session.get(type(self.season), self.season_id)
        self.assertEqual(
            [{"upto_lakh": 200, "step_lakh": 10},
             {"upto_lakh": 500, "step_lakh": 20},
             {"upto_lakh": 0, "step_lakh": 50}],
            self.A.increment_rules(season))

    def test_a_row_can_be_marked_unsold_and_reinstated(self):
        lot = self.lots()[1]
        self.post({"action": "lot_unsold", "lot_id": str(lot.id)})
        self.assertEqual(self.A.LOT_UNSOLD, self.lots()[1].status)
        self.post({"action": "lot_reinstate", "lot_id": str(lot.id)})
        back = [l for l in self.lots() if l.id == lot.id][0]
        self.assertEqual(self.A.LOT_QUEUED, back.status)

    def test_the_console_forces_a_player_by_name(self):
        wanted = self.lots()[-1]
        response = self.client.post(
            f"/auctions/{self.season_id}/console",
            data={"action": "lot_force", "players": wanted.name},
            follow_redirects=True)
        self.assertEqual(200, response.status_code)
        self.assertIn("Player controls", response.get_data(as_text=True))
        self.session.expire_all()
        self.assertEqual(wanted.id,
                         self.A.next_queued(self.session, self.season_id).id)



class RetentionAndSetsFileTests(PoolPageCase):
    """Dynamic retention's controls and the sets file, over HTTP."""

    def setUp(self):
        super().setUp()
        self.A.add_players_to_pool(self.session, self.season,
                                   self.players[:2], set_name="Marquee")
        self.A.add_players_to_pool(self.session, self.season,
                                   self.players[2:], set_name="Rest")
        self.session.commit()

    def rn(self):
        from services import retention_negotiation as RN
        self.session.expire_all()
        return RN

    def test_the_mode_toggle_and_the_dynamic_panel(self):
        self.post({"action": "retention_mode", "mode": "dynamic"})
        self.assertTrue(self.rn().is_dynamic(self.season))
        body = self.get()
        self.assertIn("Save slots", body)
        self.assertIn("Save negotiation rules", body)
        self.post({"action": "retention_mode", "mode": "classic"})
        self.assertFalse(self.rn().is_dynamic(self.season))
        self.assertNotIn("Save slots", self.get())

    def test_slots_save_with_an_added_row_and_a_removed_one(self):
        self.post({"action": "retention_mode", "mode": "dynamic"})
        self.post({"action": "retention_negotiation", "ret_budget": "70"})
        self.post({
            "action": "retention_slots",
            "slot_key": ["elite", "premium", "core", ""],
            "slot_emoji": ["🥇", "🥈", "🥉", "🧢"],
            "slot_label": ["Icon", "Premium", "Core", "Uncapped"],
            "slot_min": ["92", "87", "83", "70"],
            "slot_max": ["99", "91", "86", "82"],
            "slot_floor": ["25", "18", "12", "4"],
            "slot_remove": ["2"],
        })
        slots = self.rn().slots(self.season)
        self.assertEqual(["Icon", "Premium", "Uncapped"],
                         [s["label"] for s in slots])
        self.assertEqual(2500, slots[0]["floor_lakh"])
        self.assertEqual(3, self.season.max_retentions)

    def test_negotiation_rules_save(self):
        self.post({"action": "retention_mode", "mode": "dynamic"})
        self.post({"action": "retention_negotiation", "chances": "4",
                   "counter_pct": "85", "demand_curve": "80=10-12 | 95=30-34",
                   "p_money_enabled": "", "p_balanced_enabled": "1"})
        rules = self.rn().rules(self.season)
        self.assertEqual(4, rules["chances"])
        self.assertEqual([[80, 1000, 1200], [95, 3000, 3400]],
                         rules["demand_curve"])
        self.assertFalse(rules["personalities"]["money"]["enabled"])

    def test_sets_download_and_upload(self):
        for fmt in ("json", "csv"):
            response = self.client.get(
                f"/auctions/{self.season_id}/sets.{fmt}")
            self.assertEqual(200, response.status_code)
            self.assertIn("attachment", response.headers["Content-Disposition"])
        blob = self.client.get(
            f"/auctions/{self.season_id}/sets.json").get_data()
        import io
        other = self.A.create_season(self.session, f"Copy {self.tag}")
        self.session.commit()
        response = self.client.post(
            f"/auctions/{other.id}",
            data={"action": "sets_import",
                  "sets_file": (io.BytesIO(blob), "sets.json")},
            content_type="multipart/form-data", follow_redirects=True)
        self.assertEqual(200, response.status_code)
        self.session.expire_all()
        self.assertEqual(
            ["Marquee", "Rest"],
            [e["name"] for e in self.A.list_sets(self.session, other)])


class RetentionFromLastSeasonPageTests(PoolPageCase):
    """The setup page retains from last season's squad only."""

    def setUp(self):
        super().setUp()
        from tests._previous_league import link_squads
        self.mumbai = self.A.create_franchise(self.session, self.season,
                                              "Mumbai")
        self.kochi = self.A.create_franchise(self.session, self.season,
                                             "Kochi")
        self.season.max_retentions = 2
        self.session.commit()
        link_squads(self.session, self.A, self.season,
                    {"Mumbai": [self.players[0]],
                     "Kochi Tuskers": [self.players[1]]})

    def test_the_page_lists_each_squad_and_refuses_a_stranger(self):
        body = self.get()
        self.assertIn(self.players[0].name, body)
        response = self.post({"action": "retain",
                              "franchise_id": self.mumbai.id,
                              "player_id": self.players[2].id})
        self.assertIn("squad last season", response.get_data(as_text=True))
        self.session.expire_all()
        self.assertEqual([], self.A.retained(self.session, self.mumbai.id))

    def test_a_renamed_side_is_tied_on_the_page(self):
        team = self.A.league_teams(self.session,
                                   self.season.previous_league_id)
        tuskers = next(t for t in team if t.name == "Kochi Tuskers")
        self.post({"action": "previous_team", "franchise_id": self.kochi.id,
                   "team_id": tuskers.id})
        self.post({"action": "retain", "franchise_id": self.kochi.id,
                   "player_id": self.players[1].id, "price": "5"})
        self.session.expire_all()
        self.assertEqual([self.players[1].id],
                         [l.player_id for l in
                          self.A.retained(self.session, self.kochi.id)])


if __name__ == "__main__":       # pragma: no cover
    unittest.main()

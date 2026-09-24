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
                 "services.auction_service", "services.auction_scheduler",
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
        body = self.get()
        self.assertIn('id="pool"', body)
        self.assertIn(f'/auctions/{self.season_id}#pool', body)

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


if __name__ == "__main__":       # pragma: no cover
    unittest.main()

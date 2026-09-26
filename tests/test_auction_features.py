"""The Franchise Auction's room features: sets, the room's views, and the teams.

What is pinned here:

  • **Sets decide what comes next.** A set — or every queued player in a
    rating range — can be brought to the front, and the whole queue ordered
    set by set, without touching the lot on the block.
  • **Removing a franchise loses nothing.** Its players go back into the pool,
    its opening purse is shared equally (odd lakh included), and every purse
    still equals its ledger afterwards.
  • **Unsold players get one more go, automatically**, as the ⚡ Accelerated
    set — and whoever is still unsold after that tops up a short squad, free,
    inside the squad and overseas caps.
  • **A retention is the franchise's to accept.** Only its owner or a
    co-owner can press Accept — not another owner, not a bot admin.
  • **Auction admins run auctions and nothing else**, and only a bot admin
    appoints one.
  • **The room hears about every player and every burst of bids**, with a
    fresh pinned board per lot, and every rich message degrades to HTML.
"""

import asyncio
import itertools
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None

# Everything that caches a reference to ``database`` / ``models`` — the reason
# is the one tests/test_auction_bidding.py gives, at length.
_MODULE_NAMES = ("database", "models", "config",
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


# name, rating, category, country
CATALOGUE = [
    ("Virat Kohli", 97, "Batsman", "India"),
    ("Jasprit Bumrah", 95, "Bowler", "India"),
    ("Rashid Khan", 93, "All Rounder", "Afghanistan"),
    ("Jos Buttler", 92, "Wicket Keeper", "England"),
    ("Sanju Samson", 88, "Wicket Keeper", "India"),
    ("Tim David", 84, "Batsman", "Australia"),
    ("Rinku Singh", 83, "Batsman", "India"),
    ("Mukesh Kumar", 74, "Bowler", "India"),
]

ALICE, BOB, CAROL, DAVE, ERIN = 111, 222, 333, 444, 555

PLAYER_DEFAULTS = dict(category="Batsman", country="India", version="Base",
                       bat_hand="Right", bowl_hand="Right",
                       bowl_style="Medium Pacer", bat_rating=80,
                       bowl_rating=40, is_active=True)
NOW = datetime(2026, 3, 1, 12, 0, 0)


class AdminEnv:
    """``BOT_ADMIN_IDS`` set for the duration of a with-block."""

    def __init__(self, *ids):
        self.value = ",".join(str(i) for i in ids)

    def __enter__(self):
        self.previous = os.environ.get("BOT_ADMIN_IDS")
        os.environ["BOT_ADMIN_IDS"] = self.value

    def __exit__(self, *exc):
        if self.previous is None:
            os.environ.pop("BOT_ADMIN_IDS", None)
        else:
            os.environ["BOT_ADMIN_IDS"] = self.previous


class FeatureCase(unittest.TestCase):
    """Three franchises and the catalogue above, split into two sets."""

    min_squad = 2
    max_squad = 6
    max_overseas = 8
    purse_lakh = 10_000
    # The quiet gap after a bid is its own feature (see
    # tests/test_auction_restart_countdown.py); these tests bid on one tick.
    bid_gap = 0

    def setUp(self):
        from database import get_session
        from models import Player
        from services import auction_service as A

        self.session = get_session()
        self.A = A
        self.tag = next(_PID)
        self.players = []
        for name, rating, category, country in CATALOGUE:
            player = Player(name=name, rating=rating, category=category,
                            country=country, version="Base",
                            bat_hand="Right", bowl_hand="Right",
                            bowl_style="Medium Pacer", bat_rating=rating,
                            bowl_rating=max(0, rating - 10), is_active=True)
            self.session.add(player)
            self.players.append(player)
        self.session.flush()

        self.season = A.create_season(
            self.session, f"Features {self.tag}", min_squad_size=self.min_squad,
            max_squad_size=self.max_squad, max_overseas=self.max_overseas,
            opening_purse_lakh=self.purse_lakh,
            bid_gap_seconds=self.bid_gap)
        A.bind_chat(self.session, self.season, -5000 - self.tag)
        self.mumbai = A.create_franchise(self.session, self.season, "Mumbai",
                                         owner_tg_id=ALICE, owner_name="Alice")
        self.chennai = A.create_franchise(self.session, self.season, "Chennai",
                                          owner_tg_id=BOB, owner_name="Bob")
        self.delhi = A.create_franchise(self.session, self.season, "Delhi",
                                        owner_tg_id=DAVE, owner_name="Dave")
        self.session.commit()

    def tearDown(self):
        from models import AuctionAdmin
        self.session.rollback()
        # Auction admins are global, not per season — clear them so one test's
        # appointment cannot leak into the next.
        self.session.query(AuctionAdmin).delete()
        self.session.commit()
        self.session.close()

    # ── helpers ──

    def build_sets(self):
        """Batsmen and keepers as "Bats", everyone else as "Others"."""
        bats = [p for p in self.players
                if p.category in ("Batsman", "Wicket Keeper")]
        others = [p for p in self.players if p not in bats]
        self.A.add_players_to_pool(self.session, self.season, bats,
                                   set_name="Bats")
        self.A.add_players_to_pool(self.session, self.season, others,
                                   set_name="Others")
        self.session.commit()
        return bats, others

    def start(self, now=NOW):
        lot = self.A.start(self.session, self.season, now=now)
        self.session.commit()
        return lot

    def buy(self, franchise, lot=None, price=None, now=NOW):
        lot = lot or self.A.current_lot(self.session, self.season)
        self.A.place_bid(self.session, self.season, lot, franchise,
                         price or lot.base_price_lakh, now=now)
        sold = self.A.sell_lot(self.session, self.season, lot, now=now)
        self.session.commit()
        return sold

    def pass_until(self, predicate, guard=80):
        """Pass lot after lot until ``predicate()`` — returns names passed."""
        names = []
        while not predicate() and guard:
            guard -= 1
            lot = self.A.current_lot(self.session, self.season)
            if lot is None:
                if self.A.next_queued(self.session, self.season.id) is None:
                    break
                self.A.open_next_lot(self.session, self.season, now=NOW)
                self.session.commit()
                continue
            names.append(lot.name)
            self.A.pass_lot(self.session, self.season, lot)
            self.session.commit()
        return names

    def assert_ledger_agrees(self, note=""):
        for franchise in self.A.franchises(self.session, self.season.id):
            self.assertEqual(
                self.A.ledger_total(self.session, franchise.id),
                int(franchise.purse_remaining_lakh or 0),
                f"{franchise.name}'s ledger and purse disagree {note}")

    # ── running a handler ──

    def run_handler(self, handler, user_id, args=(), *, chat_type="supergroup",
                    bot=None, reply_to=None, chat_id=None):
        replies, markups = [], []

        async def reply_text(text, **kwargs):
            replies.append(text)
            markups.append(kwargs.get("reply_markup"))
            return SimpleNamespace(message_id=900 + len(replies))

        sent = []

        async def send_message(chat_id=None, text="", **kwargs):
            sent.append(text)
            return SimpleNamespace(message_id=700 + len(sent))

        update = SimpleNamespace(
            effective_chat=SimpleNamespace(
                id=self.season.chat_id if chat_id is None else chat_id,
                type=chat_type),
            effective_user=SimpleNamespace(id=user_id, username="u",
                                           first_name="U"),
            effective_message=SimpleNamespace(reply_text=reply_text,
                                              message_id=7,
                                              reply_to_message=reply_to))
        context = SimpleNamespace(
            args=list(args),
            bot=bot or SimpleNamespace(send_message=send_message))
        self.session.commit()
        asyncio.run(handler(update, context))
        self.session.expire_all()
        return SimpleNamespace(replies=replies, markups=markups, sent=sent)


# ══════════════════════════════════════════════════════════════════════
# Sets
# ══════════════════════════════════════════════════════════════════════

class SetTests(FeatureCase):

    def test_a_rating_range_reads_the_way_people_type_it(self):
        parse = self.A.parse_rating_range
        self.assertEqual((85, 90), parse("85-90"))
        self.assertEqual((85, 90), parse("90 - 85"))
        self.assertEqual((85, 90), parse("85 to 90"))
        self.assertEqual((85, 90), parse("85-90 OVR"))
        self.assertEqual((85, 999), parse("85+"))
        self.assertEqual((88, 88), parse("88"))
        self.assertIsNone(parse("Marquee"))
        self.assertEqual("85-90 OVR", self.A.range_set_name(85, 90))
        self.assertEqual("85+ OVR", self.A.range_set_name(85, 999))

    def test_a_rating_range_goes_in_as_one_set_best_first(self):
        from models import Player
        base = 300 + 5 * self.tag       # a band no other test's cards sit in
        for offset, name in enumerate(("Low", "Mid", "Top")):
            self.session.add(Player(name=f"Band {self.tag} {name}",
                                    rating=base + offset, **PLAYER_DEFAULTS))
        self.session.commit()
        added, skipped, label = self.A.add_rating_range_to_pool(
            self.session, self.season, base, base + 2)
        self.session.commit()
        self.assertEqual((3, 0), (added, skipped))
        self.assertEqual(f"{base}-{base + 2} OVR", label)
        queued = self.A.queued_lots(self.session, self.season, set_name=label)
        self.assertEqual([base + 2, base + 1, base],
                         [lot.rating for lot in queued])

    def test_an_empty_rating_range_is_refused_by_name(self):
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.add_rating_range_to_pool(self.session, self.season,
                                            900, 901)
        self.assertIn("900-901", str(caught.exception))

    def test_a_set_can_be_brought_to_the_front_keeping_its_order(self):
        bats, others = self.build_sets()
        first = self.start()
        self.assertEqual("Bats", self.A.set_label(first))

        label, moved = self.A.bring_forward(self.session, self.season, "oth")
        self.session.commit()
        self.assertEqual("Others", label)
        self.assertEqual([p.name for p in others], [lot.name for lot in moved])
        upcoming = self.A.next_queued(self.session, self.season.id)
        self.assertEqual(others[0].name, upcoming.name)
        # The lot on the block is untouched.
        self.assertEqual(first.id, self.A.current_lot(self.session, self.season).id)

    def test_a_rating_range_can_be_brought_forward_across_sets(self):
        self.build_sets()
        self.start()
        label, moved = self.A.bring_forward(self.session, self.season, "70-85")
        self.session.commit()
        self.assertEqual("70-85 OVR", label)
        self.assertTrue(all(70 <= lot.rating <= 85 for lot in moved))
        self.assertEqual(moved[0].id,
                         self.A.next_queued(self.session, self.season.id).id)

    def test_the_whole_queue_can_be_ordered_by_set(self):
        bats, others = self.build_sets()
        labels = self.A.set_order(self.session, self.season,
                                  ["Others", "Bats"])
        self.session.commit()
        self.assertEqual(["Others", "Bats"], labels)
        queue = self.A.queued_lots(self.session, self.season)
        self.assertEqual([p.name for p in others + bats],
                         [lot.name for lot in queue])

    def test_a_range_named_set_moves_only_itself(self):
        """"85-90 OVR" is the name /apool gives a set, not a request for
        every queued player rated 85-90 wherever they sit."""
        self.build_sets()
        others = self.A.queued_lots(self.session, self.season,
                                    set_name="Others")
        for lot in others:
            lot.set_name = "70-99 OVR"
        self.session.commit()
        label, moved = self.A.bring_forward(self.session, self.season,
                                           "70-99 OVR")
        self.assertEqual("70-99 OVR", label)
        self.assertEqual(sorted(l.id for l in others),
                         sorted(l.id for l in moved))

    def test_an_unknown_set_names_the_ones_that_are_queued(self):
        self.build_sets()
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.bring_forward(self.session, self.season, "Marquee")
        self.assertIn("Bats", str(caught.exception))
        self.assertIn("Others", str(caught.exception))

    def test_sets_report_what_is_live_what_is_next_and_what_is_done(self):
        self.build_sets()
        self.start()
        states = {e["name"]: e["state"] for e in
                  self.A.list_sets(self.session, self.season)}
        self.assertEqual({"Bats": "live", "Others": "next"}, states)
        self.assertEqual("Others",
                         self.A.next_set(self.session, self.season)["name"])

        # Run the Bats set out; Others goes live and Bats is done.
        self.pass_until(lambda: self.A.set_label(
            self.A.current_lot(self.session, self.season)
            or SimpleNamespace(set_name="x")) == "Others")
        states = {e["name"]: e["state"] for e in
                  self.A.list_sets(self.session, self.season)}
        self.assertEqual("done", states["Bats"])
        self.assertEqual("live", states["Others"])
        self.assertIsNone(self.A.next_set(self.session, self.season))


# ══════════════════════════════════════════════════════════════════════
# Set No — the running order, read back as a number
# ══════════════════════════════════════════════════════════════════════

class SetNumberTests(FeatureCase):
    """Set No is a *position*, not a column, and that is what makes it unique.

    What is pinned here: the numbers run 1…N with no repeats however the pool was
    built; typing them reorders the queue; a number already given to another set
    is refused and nothing moves; and reordering a pool nobody has bid in yet
    leaves the lot numbers compact instead of climbing every time.
    """

    def names(self):
        return [e["name"] for e in self.A.list_sets(self.session, self.season)]

    def numbers(self):
        return [e["set_no"] for e in self.A.list_sets(self.session, self.season)]

    def lot_nos(self):
        return sorted(lot.lot_no for lot in
                      self.A.lots(self.session, self.season.id))

    def three_sets(self):
        """Bats / Others, plus a third so an order has a middle to it."""
        bats, others = self.build_sets()
        from models import Player
        extra = Player(name=f"Spare {self.tag}", rating=61, **PLAYER_DEFAULTS)
        self.session.add(extra)
        self.session.flush()
        self.A.add_players_to_pool(self.session, self.season, [extra],
                                   set_name="Spares")
        self.session.commit()
        return bats, others, extra

    def test_numbers_are_one_to_n_with_no_repeats(self):
        self.three_sets()
        self.assertEqual([1, 2, 3], self.numbers())
        self.assertEqual(["Bats", "Others", "Spares"], self.names())

    def test_numbers_stay_unique_through_a_relist(self):
        # The ⚡ Accelerated set is created by the service, never by a page, so
        # nothing stamps a number on it — it has to be numbered on the way out.
        self.build_sets()
        self.start()
        self.pass_until(lambda: self.A.next_queued(self.session,
                                                   self.season.id) is None)
        self.A.relist_all(self.session, self.season,
                          set_name=self.A.ACCELERATED_SET)
        self.session.commit()
        numbers = self.numbers()
        self.assertEqual(sorted(numbers), list(range(1, len(numbers) + 1)))
        self.assertIn(self.A.ACCELERATED_SET, self.names())
        # It came back into the queue, so it runs after everything already done.
        entry = next(e for e in self.A.list_sets(self.session, self.season)
                     if e["name"] == self.A.ACCELERATED_SET)
        self.assertEqual(max(numbers), entry["set_no"])

    def test_typing_the_numbers_reorders_the_queue(self):
        self.three_sets()
        order = self.A.set_positions(
            self.session, self.season,
            [("Bats", 3), ("Others", 1), ("Spares", 2)])
        self.session.commit()
        self.assertEqual(["Others", "Spares", "Bats"], order)
        self.assertEqual(["Others", "Spares", "Bats"], self.names())
        self.assertEqual([1, 2, 3], self.numbers())
        # And the queue really runs that way, which is the only thing Set No
        # means.
        self.assertEqual(
            "Others",
            self.A.set_label(self.A.next_queued(self.session, self.season.id)))

    def test_the_numbers_are_a_rank_not_a_value(self):
        self.three_sets()
        order = self.A.set_positions(
            self.session, self.season,
            [("Bats", 40), ("Others", 2), ("Spares", 9)])
        self.session.commit()
        self.assertEqual(["Others", "Spares", "Bats"], order)
        self.assertEqual([1, 2, 3], self.numbers())

    def test_a_number_already_given_is_refused_and_nothing_moves(self):
        self.three_sets()
        before, before_lots = self.names(), self.lot_nos()
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.set_positions(self.session, self.season,
                                 [("Bats", 1), ("Others", 1), ("Spares", 3)])
        message = str(caught.exception)
        # It has to name both, or an admin cannot tell which one to change.
        self.assertIn("Bats", message)
        self.assertIn("Others", message)
        self.assertEqual(before, self.names())
        self.assertEqual(before_lots, self.lot_nos())

    def test_a_number_belonging_to_a_finished_set_is_refused(self):
        self.build_sets()
        self.start()
        self.pass_until(lambda: self.A.set_label(
            self.A.current_lot(self.session, self.season)
            or SimpleNamespace(set_name="x")) == "Others")
        done = next(e for e in self.A.list_sets(self.session, self.season)
                    if e["state"] == "done")
        queued = [e for e in self.A.list_sets(self.session, self.season)
                  if e["queued"]]
        if not queued:                      # nothing left to reorder
            return
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.set_positions(self.session, self.season,
                                 [(queued[0]["name"], done["set_no"])])
        self.assertIn(done["name"], str(caught.exception))

    def test_a_set_that_has_finished_cannot_be_reordered(self):
        self.build_sets()
        self.start()
        self.pass_until(lambda: self.A.set_label(
            self.A.current_lot(self.session, self.season)
            or SimpleNamespace(set_name="x")) == "Others")
        with self.assertRaises(self.A.AuctionError):
            self.A.set_positions(self.session, self.season, [("Bats", 1)])

    def test_move_set_swaps_neighbours_and_stops_at_the_ends(self):
        self.three_sets()
        self.assertEqual(["Bats", "Others", "Spares"], self.names())
        self.A.move_set(self.session, self.season, "Spares", -1)
        self.session.commit()
        self.assertEqual(["Bats", "Spares", "Others"], self.names())
        self.A.move_set(self.session, self.season, "Spares", -1)
        self.session.commit()
        self.assertEqual(["Spares", "Bats", "Others"], self.names())
        # Already first: a no-op, not an error — ↑ on the top row is a misclick.
        self.assertIsNone(self.A.move_set(self.session, self.season,
                                          "Spares", -1))
        self.assertEqual(["Spares", "Bats", "Others"], self.names())

    def test_move_set_matches_the_name_exactly(self):
        self.three_sets()
        # "Spare" is a prefix of "Spares" and a substring of nothing else, so
        # ``_match_set`` would happily take it. The buttons must not: they submit
        # names they rendered, and a near-miss means the page is stale.
        with self.assertRaises(self.A.AuctionError):
            self.A.move_set(self.session, self.season, "Spare", -1)
        self.assertEqual(["Bats", "Others", "Spares"], self.names())

    def test_reordering_an_untouched_pool_keeps_lot_numbers_compact(self):
        bats, others, _ = self.three_sets()
        total = len(bats) + len(others) + 1
        self.assertEqual(list(range(1, total + 1)), self.lot_nos())
        for _ in range(3):
            self.A.move_set(self.session, self.season, "Spares", -1)
            self.A.move_set(self.session, self.season, "Spares", 1)
            self.session.commit()
        # Without the compaction pass these would now be in the sixties: every
        # reorder numbers above the season's high-water mark, and the number is
        # on screen as the lot's "#".
        self.assertEqual(list(range(1, total + 1)), self.lot_nos())

    def test_a_reorder_after_a_sale_never_reuses_a_number(self):
        self.build_sets()
        self.start()
        self.buy(self.mumbai)
        before = {lot.id: lot.lot_no for lot in
                  self.A.lots(self.session, self.season.id)
                  if lot.status != self.A.LOT_QUEUED}
        self.A.set_positions(self.session, self.season, [("Others", 1)])
        self.session.commit()
        after = self.A.lots(self.session, self.season.id)
        self.assertEqual(len(after), len({lot.lot_no for lot in after}))
        # A lot that has left the queue keeps the number it ran under.
        for lot in after:
            if lot.id in before:
                self.assertEqual(before[lot.id], lot.lot_no)

    def test_quiet_reorders_say_nothing_to_the_room(self):
        from models import AuctionEvent

        def events():
            return (self.session.query(AuctionEvent)
                    .filter(AuctionEvent.season_id == self.season.id,
                            AuctionEvent.kind == "set_order").count())

        self.three_sets()
        self.assertEqual(0, events())
        # The setup page nudges the order repeatedly; every event is announced to
        # the room, so those nudges must not queue up messages.
        self.A.move_set(self.session, self.season, "Spares", -1, quiet=True)
        self.session.commit()
        self.assertEqual(0, events())
        # The live console is the opposite: the room is watching, so it is told.
        self.A.move_set(self.session, self.season, "Spares", 1, quiet=False)
        self.session.commit()
        self.assertEqual(1, events())

    def test_a_long_set_name_is_stored_clipped_to_the_column(self):
        from models import Player
        extra = Player(name=f"Longname {self.tag}", rating=62,
                       **PLAYER_DEFAULTS)
        self.session.add(extra)
        self.session.flush()
        long_name = "M" * 60
        self.A.add_players_to_pool(self.session, self.season, [extra],
                                   set_name=long_name)
        self.session.commit()
        stored = [lot.set_name for lot in
                  self.A.lots(self.session, self.season.id)
                  if lot.player_id == extra.id]
        self.assertEqual(["M" * 40], stored)
        # And the clipped name is what the reorder path has to match, so the
        # round trip has to work on it.
        self.assertIn("M" * 40, self.names())
        self.A.move_set(self.session, self.season, "M" * 40, -1)
        self.session.commit()

    def test_a_rating_band_added_from_a_page_is_numbered_like_any_other(self):
        from models import Player
        base = 320 + 5 * self.tag
        for offset in range(3):
            self.session.add(Player(name=f"Paged {self.tag} {offset}",
                                    rating=base + offset, **PLAYER_DEFAULTS))
        self.session.flush()
        self.build_sets()
        added, _, label = self.A.add_rating_range_to_pool(
            self.session, self.season, base, base + 2, set_name="From the site")
        self.session.commit()
        self.assertEqual(3, added)
        entry = next(e for e in self.A.list_sets(self.session, self.season)
                     if e["name"] == label)
        # Added last, so it runs last until somebody says otherwise.
        self.assertEqual(max(self.numbers()), entry["set_no"])
        self.A.set_positions(self.session, self.season,
                            [(label, 1), ("Bats", 2), ("Others", 3)])
        self.session.commit()
        self.assertEqual([label, "Bats", "Others"], self.names())


# ══════════════════════════════════════════════════════════════════════
# Removing a franchise
# ══════════════════════════════════════════════════════════════════════

class RemoveFranchiseTests(FeatureCase):

    def setUp(self):
        super().setUp()
        self.build_sets()
        self.start()

    def test_its_players_go_back_and_its_purse_is_shared(self):
        bought = self.buy(self.mumbai)
        self.A.open_next_lot(self.session, self.season, now=NOW)
        self.session.commit()
        before = {f.name: f.purse_remaining_lakh
                  for f in self.A.franchises(self.session, self.season.id)}

        released, shares = self.A.remove_franchise(self.session, self.season,
                                                   self.mumbai)
        self.session.commit()

        self.assertEqual([bought.name], [lot.name for lot in released])
        lot = released[0]
        self.assertEqual(self.A.LOT_QUEUED, lot.status)
        self.assertIsNone(lot.sold_to_id)
        self.assertTrue(lot.set_name.startswith(self.A.RELEASED_SET_PREFIX))
        self.assertIsNone(lot.previous_franchise_id,
                          "no Right To Match for a side that is gone")

        field = self.A.franchises(self.session, self.season.id)
        self.assertEqual(["Chennai", "Delhi"], sorted(f.name for f in field))
        for franchise in field:
            self.assertEqual(before[franchise.name] + 5_000,
                             franchise.purse_remaining_lakh)
            self.assertEqual(15_000, franchise.purse_total_lakh)
        self.assert_ledger_agrees("after a franchise was removed")

    def test_the_odd_lakh_is_not_lost(self):
        self.mumbai.purse_total_lakh = 10_001
        self.session.commit()
        _released, shares = self.A.remove_franchise(self.session, self.season,
                                                    self.mumbai)
        self.session.commit()
        self.assertEqual(10_001, sum(shares.values()))
        self.assertEqual(sorted([5_001, 5_000]), sorted(shares.values()))
        self.assert_ledger_agrees()

    def test_the_standing_bidder_cannot_be_removed(self):
        lot = self.A.current_lot(self.session, self.season)
        self.A.place_bid(self.session, self.season, lot, self.mumbai,
                         lot.base_price_lakh, now=NOW)
        self.session.commit()
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.remove_franchise(self.session, self.season, self.mumbai)
        self.assertIn("/aundobid", str(caught.exception))

    def test_the_last_franchise_cannot_be_removed(self):
        self.A.remove_franchise(self.session, self.season, self.mumbai)
        self.A.remove_franchise(self.session, self.season, self.chennai)
        self.session.commit()
        with self.assertRaises(self.A.AuctionError):
            self.A.remove_franchise(self.session, self.season, self.delhi)

    def test_a_released_player_starts_his_new_round_with_no_old_bids(self):
        lot = self.A.current_lot(self.session, self.season)
        self.A.place_bid(self.session, self.season, lot, self.chennai,
                         lot.base_price_lakh, now=NOW)
        top = self.A.next_min_bid(self.season, lot)
        self.A.place_bid(self.session, self.season, lot, self.mumbai, top,
                         now=NOW)
        self.A.sell_lot(self.session, self.season, lot, now=NOW)
        self.session.commit()
        released, _shares = self.A.remove_franchise(self.session, self.season,
                                                    self.mumbai)
        self.session.commit()
        from models import AuctionBid
        live = (self.session.query(AuctionBid)
                .filter(AuctionBid.lot_id == released[0].id,
                        AuctionBid.is_void.is_(False)).count())
        self.assertEqual(0, live, "Chennai's old bid must not survive")
        self.assertEqual(0, released[0].bid_count)

        # In the new round, undoing the first bid falls back to nothing.
        self.A.bring_forward(self.session, self.season, released[0].set_name)
        self.A.open_next_lot(self.session, self.season, now=NOW)
        relot = self.A.current_lot(self.session, self.season)
        self.assertEqual(released[0].id, relot.id)
        self.A.place_bid(self.session, self.season, relot, self.delhi,
                         relot.base_price_lakh, now=NOW)
        relot = self.A.undo_last_bid(self.session, self.season, relot, now=NOW)
        self.assertIsNone(relot.current_bidder_id)
        self.assertIsNone(relot.current_bid_lakh)

    def test_removing_a_team_after_the_end_re_opens_the_auction(self):
        self.buy(self.mumbai)
        self.season.auto_accelerated = 0
        self.session.commit()
        self.pass_until(lambda: self.season.status == self.A.STATUS_COMPLETED)
        self.assertEqual(self.A.STATUS_COMPLETED, self.season.status)
        self.A.remove_franchise(self.session, self.season, self.mumbai)
        self.session.commit()
        self.assertEqual(self.A.STATUS_PAUSED, self.season.status,
                         "released players have to be sellable")
        self.A.start(self.session, self.season, now=NOW)
        self.session.commit()
        self.assertTrue(self.A.set_label(self.A.current_lot(
            self.session, self.season)).startswith(self.A.RELEASED_SET_PREFIX))

    def test_the_command_previews_before_it_acts(self):
        from handlers import auction as H
        self.buy(self.mumbai)
        with AdminEnv(CAROL):
            out = self.run_handler(H.aremoveteam_handler, CAROL, ("Mumbai",))
        self.assertIn("confirm", out.replies[-1])
        self.assertEqual(3, len(self.A.franchises(self.session, self.season.id)))
        with AdminEnv(CAROL):
            out = self.run_handler(H.aremoveteam_handler, CAROL,
                                   ("Mumbai", "|", "confirm"))
        self.assertIn("removed", out.replies[-1])
        self.assertEqual(2, len(self.A.franchises(self.session, self.season.id)))


# ══════════════════════════════════════════════════════════════════════
# The accelerated round, automatically — and the free auto-fill after it
# ══════════════════════════════════════════════════════════════════════

class AcceleratedAndAutofillTests(FeatureCase):
    min_squad = 2

    def setUp(self):
        super().setUp()
        self.build_sets()
        self.start()

    def _run_out(self):
        return self.pass_until(
            lambda: self.season.status == self.A.STATUS_COMPLETED)

    def test_unsold_players_come_back_once_as_the_accelerated_set(self):
        first_round = self.pass_until(
            lambda: bool(self.A.queued_lots(self.session, self.season))
            and self.A.set_label(self.A.queued_lots(
                self.session, self.season)[0]) == self.A.ACCELERATED_SET
            or self.season.status == self.A.STATUS_COMPLETED)
        self.assertEqual(len(CATALOGUE), len(first_round))
        self.assertEqual(self.A.STATUS_LIVE, self.season.status,
                         "the auction carries on into the accelerated round")
        self.assertEqual(1, self.season.accelerated_done)
        back = self.A.queued_lots(self.session, self.season)
        self.assertEqual(sorted(first_round), sorted(lot.name for lot in back))
        self.assertTrue(all(lot.set_name == self.A.ACCELERATED_SET for lot in back))

        self._run_out()
        self.assertEqual(self.A.STATUS_COMPLETED, self.season.status,
                         "only one accelerated round — then it finishes")

    def test_with_the_automatic_round_off_it_simply_finishes(self):
        self.season.auto_accelerated = 0
        self.session.commit()
        passed = self._run_out()
        self.assertEqual(len(CATALOGUE), len(passed))
        self.assertEqual(self.A.STATUS_COMPLETED, self.season.status)

    def test_short_squads_are_topped_up_free_at_the_end(self):
        self.buy(self.mumbai)                  # Mumbai 1, the others 0
        self._run_out()
        self.assertEqual(self.A.STATUS_COMPLETED, self.season.status)
        for franchise in self.A.franchises(self.session, self.season.id):
            squad = self.A.squad(self.session, franchise.id)
            self.assertEqual(self.min_squad, len(squad),
                             f"{franchise.name} is not topped up")
            filled = [lot for lot in squad
                      if lot.acquisition == self.A.ACQ_AUTOFILL]
            self.assertTrue(all(lot.sold_price_lakh == 0 for lot in filled))
        # Free: nobody's purse moved for an auto-filled player.
        chennai = self.A.franchises(self.session, self.season.id)[1]
        self.assertEqual(self.purse_lakh, chennai.purse_remaining_lakh)
        self.assert_ledger_agrees("after the auto-fill")

    def test_with_the_automatic_round_off_nobody_is_auto_filled(self):
        """The admin chose to leave them unsold; handing them out overrules it."""
        self.season.auto_accelerated = 0
        self.session.commit()
        self._run_out()
        self.assertEqual(self.A.STATUS_COMPLETED, self.season.status)
        for franchise in self.A.franchises(self.session, self.season.id):
            self.assertEqual([], self.A.squad(self.session, franchise.id))
        # A manual round counts: run it out and the short squads are filled.
        self.A.relist_all(self.session, self.season)
        self.A.start(self.session, self.season, now=NOW)
        self.session.commit()
        self._run_out()
        for franchise in self.A.franchises(self.session, self.season.id):
            self.assertEqual(self.min_squad,
                             len(self.A.squad(self.session, franchise.id)))

    def test_the_auto_fill_respects_the_overseas_cap(self):
        self.season.max_overseas = 0
        self.session.commit()
        self._run_out()
        for franchise in self.A.franchises(self.session, self.season.id):
            self.assertEqual(0, self.A.overseas_count(self.session, franchise.id))

    def test_the_auto_fill_never_gives_a_squad_the_same_cricketer_twice(self):
        from models import Player
        twin = Player(name=self.players[0].name, rating=99,
                      **dict(PLAYER_DEFAULTS, version="Icon"),
                      parent_player_id=self.players[0].id)
        self.session.add(twin)
        self.session.commit()
        # Pause first — a pool is only built while nothing is running.
        self.A.pause(self.session, self.season)
        self.A.add_players_to_pool(self.session, self.season, [twin],
                                   set_name="Icons")
        self.A.start(self.session, self.season, now=NOW)
        self.session.commit()
        # Mumbai buys the base Kohli; everything else goes unsold.
        lot = self.A.current_lot(self.session, self.season)
        self.assertEqual(self.players[0].name, lot.name)
        self.buy(self.mumbai, lot)
        self._run_out()
        names = [lot.name for lot in self.A.squad(self.session, self.mumbai.id)]
        self.assertEqual(len(names), len(set(names)))


# ══════════════════════════════════════════════════════════════════════
# Retention: the admin offers, the franchise accepts
# ══════════════════════════════════════════════════════════════════════

class RetentionOfferTests(FeatureCase):

    def setUp(self):
        super().setUp()
        self.season.max_retentions = 2
        self.mumbai.co_owner_ids_json = json.dumps([ERIN])
        self.session.commit()
        self.player = self.players[1]          # Jasprit Bumrah

    def offer(self, price=None):
        offer = self.A.offer_retention(self.session, self.season, self.mumbai,
                                       self.player, price, by_tg_id=CAROL)
        self.session.commit()
        return offer

    def test_nothing_is_signed_until_the_franchise_accepts(self):
        offer = self.offer(600)
        self.assertEqual([], self.A.retained(self.session, self.mumbai.id))
        lot = self.A.answer_retention_offer(self.session, self.season, offer,
                                            ALICE, True, now=NOW)
        self.session.commit()
        self.assertEqual(600, lot.sold_price_lakh)
        self.assertEqual(self.A.OFFER_ACCEPTED, offer.status)
        self.assertEqual(1, len(self.A.retained(self.session, self.mumbai.id)))
        self.assert_ledger_agrees("after an accepted retention")

    def test_the_accepted_card_shows_the_slab_and_price_paid(self):
        from services import auction_rich as AR
        offer = self.offer()                       # the ladder decides
        self.A.answer_retention_offer(self.session, self.season, offer,
                                      ALICE, True, now=NOW)
        self.session.commit()
        card = AR.retention_offer_card(self.session, self.season, offer)
        self.assertIn("retention 1/2", card)
        self.assertIn("₹18 Cr", card, "not re-priced off the next slab")
        self.assertIn("Accepted", card)

    def test_a_co_owner_may_accept(self):
        offer = self.offer()
        lot = self.A.answer_retention_offer(self.session, self.season, offer,
                                            ERIN, True, now=NOW)
        self.assertEqual(1800, lot.sold_price_lakh, "the ladder's first slab")

    def test_another_owner_and_a_bot_admin_cannot_answer(self):
        offer = self.offer()
        with AdminEnv(CAROL):
            for who in (BOB, CAROL):
                with self.assertRaises(self.A.AuctionError) as caught:
                    self.A.answer_retention_offer(self.session, self.season,
                                                  offer, who, True, now=NOW)
                self.assertIn("Mumbai", str(caught.exception))
        self.assertEqual(self.A.OFFER_PENDING, offer.status)

    def test_a_declined_offer_signs_nobody_and_cannot_be_answered_again(self):
        offer = self.offer()
        self.assertIsNone(self.A.answer_retention_offer(
            self.session, self.season, offer, ALICE, False))
        self.session.commit()
        self.assertEqual(self.A.OFFER_DECLINED, offer.status)
        self.assertEqual([], self.A.retained(self.session, self.mumbai.id))
        with self.assertRaises(self.A.AuctionError):
            self.A.answer_retention_offer(self.session, self.season, offer,
                                          ALICE, True)

    def test_the_caps_still_refuse_when_they_accept(self):
        offer = self.offer(20_000)             # more than the purse
        with self.assertRaises(self.A.AuctionError):
            self.A.answer_retention_offer(self.session, self.season, offer,
                                          ALICE, True, now=NOW)

    def test_one_waiting_offer_per_player(self):
        self.offer()
        with self.assertRaises(self.A.AuctionError) as caught:
            self.offer()
        self.assertIn("/aretcancel", str(caught.exception))

    def test_the_database_holds_one_pending_offer_per_player(self):
        """The read-then-insert check can race; the partial index cannot."""
        from sqlalchemy.exc import IntegrityError
        from models import AuctionRetentionOffer
        first = self.offer()

        def row(status="pending"):
            return AuctionRetentionOffer(
                season_id=self.season.id, franchise_id=self.chennai.id,
                player_id=self.player.id, player_name=self.player.name,
                status=status)

        self.session.add(row())
        with self.assertRaises(IntegrityError):
            self.session.flush()
        self.session.rollback()
        # Answered offers do not count against it.
        self.A.answer_retention_offer(self.session, self.season,
                                      self.A.retention_offer(self.session,
                                                             first.id),
                                      ALICE, False)
        self.session.add(row())
        self.session.flush()
        self.session.commit()

    def test_an_offer_that_cannot_be_posted_is_withdrawn(self):
        from handlers import auction as H
        from models import Player
        name = f"Unposted Player {self.tag}"
        self.session.add(Player(name=name, rating=90, **PLAYER_DEFAULTS))
        self.session.commit()

        async def refuse(text, **kwargs):
            raise RuntimeError("telegram said no")

        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=self.season.chat_id,
                                           type="supergroup"),
            effective_user=SimpleNamespace(id=CAROL, username="c",
                                           first_name="C"),
            effective_message=SimpleNamespace(reply_text=refuse, message_id=7,
                                              reply_to_message=None))
        context = SimpleNamespace(args=["Mumbai", "|", name],
                                  bot=SimpleNamespace())
        with AdminEnv(CAROL):
            asyncio.run(H.aretain_handler(update, context))
        self.session.expire_all()
        self.assertEqual([], [o for o in self.A.pending_retention_offers(
            self.session, self.season.id) if o.player_name == name],
            "a pending offer nobody can see would block the next one")

    def test_aretain_posts_an_offer_with_the_franchises_buttons(self):
        from handlers import auction as H
        name = f"Offer Player {self.tag}"
        from models import Player
        self.session.add(Player(name=name, rating=90, **PLAYER_DEFAULTS))
        self.session.commit()
        with AdminEnv(CAROL):
            out = self.run_handler(H.aretain_handler, CAROL,
                                   ("Mumbai", "|", name, "|", "5"))
        self.assertIn("Retention offer", out.replies[-1])
        buttons = out.markups[-1].inline_keyboard[0]
        self.assertTrue(buttons[0].callback_data.startswith("au_ret_"))
        self.assertEqual([], self.A.retained(self.session, self.mumbai.id))
        pending = self.A.pending_retention_offers(self.session, self.season.id)
        self.assertEqual([name], [o.player_name for o in pending])
        self.assertEqual(901, pending[0].message_id,
                         "the card is remembered so it can be updated")

    def test_the_accept_button_retains(self):
        from handlers import auction as H
        offer = self.offer(700)
        answers, edits = [], []

        async def answer(text=None, **kwargs):
            answers.append(text)

        async def edit_message_text(text, **kwargs):
            edits.append(text)

        def press(user_id):
            query = SimpleNamespace(data=f"au_ret_{offer.id}_yes",
                                    answer=answer,
                                    edit_message_text=edit_message_text)
            update = SimpleNamespace(
                callback_query=query,
                effective_user=SimpleNamespace(id=user_id),
                effective_chat=SimpleNamespace(id=self.season.chat_id))
            self.session.commit()
            asyncio.run(H.retention_offer_callback(update, SimpleNamespace()))
            self.session.expire_all()

        press(BOB)
        self.assertIn("Mumbai", answers[-1])
        self.assertEqual([], self.A.retained(self.session, self.mumbai.id))
        press(ALICE)
        self.assertIn("Retained", answers[-1])
        self.assertIn("Accepted", edits[-1])
        self.assertEqual(1, len(self.A.retained(self.session, self.mumbai.id)))


# ══════════════════════════════════════════════════════════════════════
# Auction admins
# ══════════════════════════════════════════════════════════════════════

class AuctionAdminTests(FeatureCase):

    def test_an_auction_admin_can_run_the_auction(self):
        from handlers import auction as H
        with AdminEnv(CAROL):
            out = self.run_handler(H.atimer_handler, ERIN, ("45",))
            self.assertIn("Only auction admins", out.replies[-1])
            self.A.add_auction_admin(self.session, ERIN, name="Erin",
                                     by_tg_id=CAROL)
            self.session.commit()
            out = self.run_handler(H.atimer_handler, ERIN, ("45",))
        self.assertIn("45s", out.replies[-1])
        self.assertEqual(45, self.season.bid_seconds)

    def test_only_a_bot_admin_can_appoint_one(self):
        from handlers import auction as H
        self.A.add_auction_admin(self.session, ERIN)
        self.session.commit()
        with AdminEnv(CAROL):
            out = self.run_handler(H.aadminadd_handler, ERIN, (str(DAVE),))
            self.assertIn("Only bot admins", out.replies[-1])
            self.assertFalse(self.A.is_auction_admin(self.session, DAVE))
            out = self.run_handler(H.aadminadd_handler, CAROL, (str(DAVE),))
            self.assertIn("auction admin", out.replies[-1])
            self.assertTrue(self.A.is_auction_admin(self.session, DAVE))
            out = self.run_handler(H.aadminremove_handler, CAROL, (str(DAVE),))
        self.assertFalse(self.A.is_auction_admin(self.session, DAVE))

    def test_a_reply_names_the_new_admin(self):
        from handlers import auction as H
        replied = SimpleNamespace(from_user=SimpleNamespace(
            id=DAVE, username="dave", first_name="Dave"))
        with AdminEnv(CAROL):
            self.run_handler(H.aadminadd_handler, CAROL, (), reply_to=replied)
        row = [r for r in self.A.auction_admins(self.session) if r.tg_id == DAVE]
        self.assertEqual("@dave", row[0].name)

    def test_the_auction_admin_table_opens_no_other_admin_gate(self):
        """Every other admin check in the bot reads ``is_admin`` alone."""
        from services.admin_ids import is_admin
        self.A.add_auction_admin(self.session, ERIN)
        self.session.commit()
        with AdminEnv(CAROL):
            self.assertTrue(self.A.is_auction_admin(self.session, ERIN))
            self.assertFalse(is_admin(ERIN))

    def test_admin_help_lists_the_commands_and_only_bot_admins_see_appointing(self):
        from services import auction_rich as AR
        _blocks, text = AR.admin_help(bot_admin=False)
        for command in ("/apool", "/anextset", "/aremoveteam", "/acall",
                        "/aretain", "/aretainforce", "/aaccelmode"):
            self.assertIn(command, text)
        self.assertNotIn("/aadminadd", text)
        _blocks, text = AR.admin_help(bot_admin=True)
        self.assertIn("/aadminadd", text)


# ══════════════════════════════════════════════════════════════════════
# Calling the teams, and the room's views
# ══════════════════════════════════════════════════════════════════════

class RoomViewTests(FeatureCase):

    def setUp(self):
        super().setUp()
        self.build_sets()
        self.start()

    def test_acall_tags_every_owner_and_co_owner(self):
        from handlers import auction as H
        self.chennai.co_owner_ids_json = json.dumps([ERIN])
        self.session.commit()
        with AdminEnv(CAROL):
            out = self.run_handler(H.acall_handler, CAROL,
                                   ("Resuming", "in", "5"))
        said = "\n".join(out.sent)
        for tg_id in (ALICE, BOB, DAVE, ERIN):
            self.assertIn(f"tg://user?id={tg_id}", said)
        self.assertIn("Resuming in 5", said)

    def test_a_long_call_is_split_under_telegrams_limit(self):
        from services import auction_rich as AR
        parts = AR.split_html("head", ["x" * 1000] * 10)
        self.assertTrue(len(parts) > 1)
        self.assertTrue(all(len(p) <= AR.TEXT_LIMIT for p in parts))

    def test_every_view_answers_in_the_group(self):
        from handlers import auction as H
        self.buy(self.mumbai)
        self.A.open_next_lot(self.session, self.season, now=NOW)
        self.session.commit()
        checks = [
            (H.asets_handler, (), "Bats"),
            (H.anextset_handler, (), "Others"),
            (H.anextplayer_handler, (), "Next up"),
            (H.asquad_handler, (), "Mumbai"),
            (H.asoldlist_handler, (), self.players[0].name),
            (H.aunsoldlist_handler, (), "Unsold"),
            (H.apurse_handler, (), "Chennai"),
            (H.ainfo_handler, (), "Features"),
        ]
        for handler, args, expected in checks:
            out = self.run_handler(handler, ALICE, args)
            self.assertTrue(out.replies, f"{handler.__name__} said nothing")
            self.assertIn(expected, out.replies[-1], handler.__name__)

    def _info_buttons(self, user_id=ALICE):
        """(the view keys on the /ainfo card, every callback datum on it)."""
        from handlers import auction as H
        from services.button_access import split_owner
        out = self.run_handler(H.ainfo_handler, user_id)
        data = [b.callback_data for row in out.markups[-1].inline_keyboard
                for b in row]
        views = {split_owner("au_info_", d, separator="_")[1]
                 for d in data if d.startswith("au_info_")}
        return views, data

    def test_ainfo_carries_a_button_per_view(self):
        views, _data = self._info_buttons()
        self.assertEqual({"rules", "sets", "nextset", "next", "squad", "sold",
                          "unsold", "purse"}, views)

    def test_the_card_belongs_to_whoever_asked_for_it(self):
        """Every button names its owner, and the card can be closed."""
        from services.button_access import (check_callback_owner, split_owner)
        _views, data = self._info_buttons()
        for datum in data:
            if not datum.startswith("au_info_"):
                continue
            self.assertEqual(ALICE, split_owner("au_info_", datum,
                                                separator="_")[0], datum)
        self.assertIn(f"au_x_u{ALICE}", data, "no ❌ Close on the card")

        # …and the guard agrees: Bob may not drive Alice's card.
        def press(datum, by):
            return check_callback_owner(SimpleNamespace(
                callback_query=SimpleNamespace(
                    data=datum, from_user=SimpleNamespace(id=by),
                    message=SimpleNamespace(chat_id=-1, message_id=1))))
        self.assertTrue(press(f"au_info_u{ALICE}_sets", ALICE))
        self.assertFalse(press(f"au_info_u{ALICE}_sets", BOB))
        self.assertFalse(press(f"au_x_u{ALICE}", BOB))
        # The pinned board stays the room's.
        self.assertTrue(press("au_bid_4_200", BOB))

    def test_every_admin_card_carries_a_close_button(self):
        """An admin's answer is a card in the same busy room as everyone's."""
        from handlers import auction as H
        checks = (
            (H.atimer_handler, ("45",)),
            (H.asnipe_handler, ()),
            (H.afocus_handler, ()),
            (H.adirect_handler, ()),
            (H.aretlock_handler, ()),
            (H.aadmins_handler, ()),
            (H.aadmin_handler, ()),          # the /adminhelp reference card
        )
        with AdminEnv(ALICE):
            for handler, args in checks:
                out = self.run_handler(handler, ALICE, args)
                markup = out.markups[-1]
                self.assertIsNotNone(markup,
                                     f"{handler.__name__} sent no keyboard")
                data = [b.callback_data for row in markup.inline_keyboard
                        for b in row]
                self.assertIn(f"au_x_u{ALICE}", data, handler.__name__)

    def test_a_refusal_is_not_a_card(self):
        """The Close button is for answers, not for one-line refusals."""
        from handlers import auction as H
        with AdminEnv(CAROL):
            out = self.run_handler(H.atimer_handler, ALICE, ("45",))
        self.assertIn("Only auction admins", out.replies[-1])
        self.assertIsNone(out.markups[-1])

    def test_every_view_card_carries_a_close_button(self):
        from handlers import auction as H
        for handler, args in ((H.arules_handler, ()), (H.asets_handler, ()),
                              (H.anextplayer_handler, ()),
                              (H.asquad_handler, ()), (H.apurse_handler, ()),
                              (H.asoldlist_handler, ()),
                              (H.aunsoldlist_handler, ()),
                              (H.aboard_handler, ())):
            out = self.run_handler(handler, ALICE, args)
            markup = out.markups[-1]
            self.assertIsNotNone(markup, f"{handler.__name__} sent no keyboard")
            data = [b.callback_data for row in markup.inline_keyboard
                    for b in row]
            self.assertIn(f"au_x_u{ALICE}", data, handler.__name__)

    def test_ainfo_only_offers_the_views_this_auction_has(self):
        """🔒 Retention and 🆕 Picks are buttons whose only answer would be
        "not a thing here" when the auction is configured for neither."""
        views, _data = self._info_buttons()
        self.assertNotIn("retention", views)
        self.assertNotIn("picks", views)

        self.season.max_retentions = 2
        self.season.expansion_picks = 3
        self.session.commit()
        views, _data = self._info_buttons()
        self.assertIn("retention", views)
        self.assertIn("picks", views)

    def test_my_squad_needs_a_franchise_or_a_name(self):
        from handlers import auction as H
        out = self.run_handler(H.asquad_handler, 9999)
        self.assertIn("do not own a franchise", out.replies[-1])
        out = self.run_handler(H.asquad_handler, 9999, ("Chennai",))
        self.assertIn("Chennai", out.replies[-1])

    def test_a_player_cannot_reorder_the_sets(self):
        from handlers import auction as H
        out = self.run_handler(H.anextset_handler, ALICE, ("Others",))
        self.assertIn("Only auction admins", out.replies[-1])

    def test_an_admin_brings_a_set_forward_from_the_group(self):
        from handlers import auction as H
        with AdminEnv(CAROL):
            out = self.run_handler(H.anextset_handler, CAROL, ("Others",))
        self.assertIn("comes next", out.replies[-1])
        upcoming = self.A.next_queued(self.session, self.season.id)
        self.assertEqual("Others", self.A.set_label(upcoming))


# ══════════════════════════════════════════════════════════════════════
# Before the auction
#
# The hour before /astart is when a franchise actually decides what it is
# going to do, and every number it decides against — the purse, the caps, the
# reserve the max-bid rule holds back, the bid ladder, the clock, who kept
# whom — used to live on the admin's setup page or behind an admin-only
# command. What is pinned here:
#
#   • every team view answers while the auction is still in setup;
#   • /arules prints the numbers the auction will run by, to anyone;
#   • /aretlock and /apicks read out to the room; only their switches are
#     the admin's;
#   • a team view answers in a DM, resolved from the team the asker owns —
#     and /bid still does not.
# ══════════════════════════════════════════════════════════════════════

class BeforeTheAuctionTests(FeatureCase):

    def setUp(self):
        super().setUp()
        self.build_sets()          # a pool, but deliberately NOT started
        self.season.max_retentions = 2
        # A co-owner who exists in this test's auction and in no other. The
        # DM path resolves an auction from the teams the asker is in, and the
        # shared database here carries every other test's season, so ALICE is
        # in a dozen of them by the time this runs.
        self.solo = 9_000_000 + self.tag
        self.A.add_co_owner(self.session, self.mumbai, self.solo)
        self.session.commit()

    def test_every_team_view_answers_before_the_auction_starts(self):
        from handlers import auction as H
        self.assertEqual(self.A.STATUS_SETUP, self.season.status)
        checks = [
            (H.ainfo_handler, (), "Features"),
            (H.arules_handler, (), "Opening purse"),
            (H.asets_handler, (), "Bats"),
            (H.anextset_handler, (), "Bats"),
            (H.anextplayer_handler, (), "Next up"),
            (H.asquad_handler, (), "Mumbai"),
            (H.apurse_handler, (), "Chennai"),
            (H.aretlock_handler, (), "Retention"),
            (H.asoldlist_handler, (), "Sold"),
            (H.aunsoldlist_handler, (), "Unsold"),
        ]
        for handler, args, expected in checks:
            out = self.run_handler(handler, ALICE, args)
            self.assertTrue(out.replies, f"{handler.__name__} said nothing")
            self.assertIn(expected, out.replies[-1], handler.__name__)

    def test_ainfo_says_where_setup_has_got_to_not_zero_lots_resolved(self):
        """"0/8 lots resolved" is true and useless before the first lot."""
        from handlers import auction as H
        out = self.run_handler(H.ainfo_handler, ALICE)
        body = out.replies[-1]
        self.assertIn("not started yet", body)
        self.assertNotIn("lots resolved", body)
        self.assertIn("Pool", body)
        self.assertIn("Retention", body)
        self.assertIn("/arules", body)

    def test_arules_carries_every_number_a_bid_is_decided_against(self):
        from handlers import auction as H
        out = self.run_handler(H.arules_handler, ALICE)
        body = out.replies[-1]
        for expected in ("Opening purse", "Squad", "Overseas", "Anti-snipe",
                         "Base prices", "Bid increments", "Retention"):
            self.assertIn(expected, body, expected)
        # The reserve the reachability rule holds back is a rule you can only
        # meet by breaking it unless it is written down.
        self.assertIn(self.A.render_money(self.season.min_base_price_lakh,
                                          self.season.currency_label), body)

    def test_arules_leaves_out_what_this_auction_does_not_use(self):
        from handlers import auction as H
        out = self.run_handler(H.arules_handler, ALICE)
        self.assertNotIn("Right To Match", out.replies[-1])
        self.assertNotIn("Expansion picks", out.replies[-1])

        self.A.set_rtm_rules(self.session, self.season, enabled=True,
                             per_team=2)
        self.A.set_expansion_picks(self.session, self.season, 3)
        self.session.commit()
        out = self.run_handler(H.arules_handler, ALICE)
        self.assertIn("Right To Match", out.replies[-1])
        self.assertIn("Expansion picks", out.replies[-1])

    def test_a_player_reads_retention_and_only_an_admin_closes_it(self):
        from handlers import auction as H
        self.A.retain(self.session, self.season, self.mumbai, self.players[0])
        self.session.commit()

        out = self.run_handler(H.aretlock_handler, ALICE)
        self.assertIn(self.players[0].name, out.replies[-1])
        self.assertIn("Mumbai", out.replies[-1])
        self.assertFalse(self.A.retention_locked(self.season))

        out = self.run_handler(H.aretlock_handler, ALICE, ("on",))
        self.assertIn("Only auction admins", out.replies[-1])
        self.session.expire_all()
        self.assertFalse(self.A.retention_locked(self.season))

        with AdminEnv(CAROL):
            self.run_handler(H.aretlock_handler, CAROL, ("on",))
        self.session.expire_all()
        self.assertTrue(self.A.retention_locked(self.season))

    def test_a_player_reads_the_expansion_pick_order(self):
        from handlers import auction as H
        out = self.run_handler(H.apicks_handler, ALICE)
        self.assertIn("Expansion picks", out.replies[-1])
        # Nobody is new here, so it says so rather than refusing the reader.
        self.assertNotIn("Only auction admins", out.replies[-1])

    def test_a_team_view_answers_in_a_dm_and_a_bid_does_not(self):
        from handlers import auction as H

        def dm(handler, args=()):
            return self.run_handler(handler, self.solo, args,
                                    chat_type="private", chat_id=self.solo)

        self.assertIn(self.season.name, dm(H.ainfo_handler).replies[-1])
        self.assertIn("Opening purse", dm(H.arules_handler).replies[-1])
        self.assertIn("Mumbai", dm(H.asquad_handler).replies[-1])
        self.assertIn("Bats", dm(H.asets_handler).replies[-1])
        self.assertIn("Chennai", dm(H.apurse_handler).replies[-1])

        # A bid nobody in the room saw is how a price gets disputed.
        out = dm(H.bid_handler)
        self.assertIn("only work in the group", out.replies[-1])

    def test_a_dm_from_somebody_with_no_team_says_so(self):
        from handlers import auction as H
        out = self.run_handler(H.ainfo_handler, 4_321, chat_type="private",
                               chat_id=4_321)
        self.assertIn("do not have a franchise", out.replies[-1])

    def test_a_group_with_no_auction_never_borrows_another_rooms(self):
        """The asker owns Mumbai here; asking in an unrelated group must not
        answer with this auction's numbers."""
        from handlers import auction as H
        out = self.run_handler(H.ainfo_handler, self.solo, chat_id=-999_000)
        self.assertIn("No auction is running in this chat", out.replies[-1])

    def test_two_auctions_are_named_rather_than_guessed_between(self):
        from handlers import auction as H
        other = self.A.create_season(self.session, f"Other {self.tag}")
        self.A.create_franchise(self.session, other, "Kolkata",
                                owner_tg_id=self.solo, owner_name="Solo")
        self.session.commit()
        out = self.run_handler(H.ainfo_handler, self.solo, chat_type="private",
                               chat_id=self.solo)
        self.assertIn("You have a team in", out.replies[-1])
        self.assertIn(other.name, out.replies[-1])
        self.assertIn(self.season.name, out.replies[-1])

    def test_the_purse_reads_from_both_ends_once_retention_has_spent(self):
        from handlers import auction as H
        self.A.retain(self.session, self.season, self.mumbai, self.players[0],
                      price_lakh=1800)
        self.session.commit()
        start = self.A.render_money(self.mumbai.purse_total_lakh,
                                    self.season.currency_label)
        left = self.A.render_money(self.mumbai.purse_remaining_lakh,
                                   self.season.currency_label)
        out = self.run_handler(H.asquad_handler, ALICE)
        self.assertIn(f"{left} left of {start}", out.replies[-1])


# ══════════════════════════════════════════════════════════════════════
# Rich messages
# ══════════════════════════════════════════════════════════════════════

def _cells(blocks):
    for block in blocks:
        if block.get("type") == "table":
            for row in block["cells"]:
                yield from row
        if block.get("type") == "details":
            yield from _cells(block["blocks"])


class SetsCardTests(FeatureCase):
    """The 🗂 Sets card: pages, a button per set, and a set opened in full.

    A pool is routinely dozens of sets of dozens of players — several messages'
    worth — so what is pinned here is that the card never tries to print all of
    it: it pages, every set on the page collapses, and opening one pages too.
    """

    def many_sets(self, count=8, per_set=3, big=None):
        """``count`` sets of ``per_set`` players, one of them ``big``."""
        from models import Player
        base = 200 + 40 * self.tag
        made = []
        for index in range(count):
            size = big if (big and index == count - 1) else per_set
            batch = []
            for offset in range(size):
                player = Player(name=f"S{self.tag}-{index}-{offset}",
                                rating=base + index, **PLAYER_DEFAULTS)
                self.session.add(player)
                batch.append(player)
            self.session.flush()
            name = f"Band {index + 1}" if not (big and index == count - 1) \
                else "Big one"
            self.A.add_players_to_pool(self.session, self.season, batch,
                                       set_name=name)
            made.append(name)
        self.session.commit()
        return made

    def labels(self, keyboard):
        return [b.text for row in keyboard.inline_keyboard for b in row]

    def datas(self, keyboard):
        return [b.callback_data for row in keyboard.inline_keyboard for b in row]

    def test_the_card_pages_and_offers_a_button_per_set(self):
        from services import auction_rich as AR
        self.many_sets(count=8)
        blocks, html_text, keyboard = AR.sets_view(self.session, self.season)
        labels = self.labels(keyboard)
        self.assertEqual(AR.SETS_PAGE_SIZE,
                         sum(1 for b in blocks if b.get("type") == "details"))
        self.assertEqual(AR.SETS_PAGE_SIZE,
                         sum(1 for label in labels if label.startswith("#")))
        self.assertIn("Next ➡️", labels)
        self.assertNotIn("⬅️ Prev", labels)
        self.assertIn("page 1/2", html_text)

        _, _, second = AR.sets_view(self.session, self.season, page=2)
        self.assertIn("⬅️ Prev", self.labels(second))
        self.assertNotIn("Next ➡️", self.labels(second))

    def test_a_page_beyond_the_end_clamps_instead_of_emptying(self):
        from services import auction_rich as AR
        self.many_sets(count=8)
        blocks, html_text, _ = AR.sets_view(self.session, self.season, page=99)
        self.assertIn("page 2/2", html_text)
        self.assertTrue(any(b.get("type") == "details" for b in blocks))

    def test_a_set_opens_in_full_and_pages_its_players(self):
        from services import auction_rich as AR
        self.many_sets(count=3, big=25)
        big = next(e for e in self.A.list_sets(self.session, self.season)
                   if e["name"] == "Big one")
        blocks, html_text, keyboard = AR.sets_view(
            self.session, self.season, expand=big["set_no"])
        self.assertIn(f"Players 1–{AR.LOT_PAGE_SIZE} of 25", html_text)
        self.assertIn("⬅️ All sets", self.labels(keyboard))
        rows = sum(len(b["cells"]) for b in blocks if b.get("type") == "table")
        self.assertEqual(AR.LOT_PAGE_SIZE + 1, rows)   # + the header row

        _, tail, _ = AR.sets_view(self.session, self.season,
                                  expand=big["set_no"], lot_page=2)
        self.assertIn(f"Players {AR.LOT_PAGE_SIZE + 1}–25 of 25", tail)

    def test_all_sets_goes_back_to_the_page_the_set_was_on(self):
        from services import auction_rich as AR
        self.many_sets(count=8)
        last = max(self.A.list_sets(self.session, self.season),
                   key=lambda e: e["set_no"])
        _, _, keyboard = AR.sets_view(self.session, self.season,
                                      expand=last["set_no"])
        # Not page 1: a set on the second page has to come back to the second
        # page, or paging to it again is the price of every look.
        self.assertIn(f"{AR.SETS_CB}p_2", self.datas(keyboard))

    def test_sets_are_addressed_by_number_not_by_name(self):
        from services import auction_rich as AR
        # Telegram caps callback data at 64 bytes and set names carry emoji,
        # spaces and commas — the number is what survives the round trip.
        self.A.add_players_to_pool(self.session, self.season, self.players,
                                   set_name="⚡ Accelerated, take two")
        self.session.commit()
        _, _, keyboard = AR.sets_view(self.session, self.season)
        for data in self.datas(keyboard):
            self.assertLessEqual(len(data.encode("utf-8")), 64)
            self.assertNotIn("Accelerated", data)

    def test_a_number_that_no_longer_exists_is_refused(self):
        from services import auction_rich as AR
        self.build_sets()
        # The set finished and the numbers moved while the card sat open. Saying
        # so beats opening whichever set now holds that number.
        self.assertIsNone(AR.sets_view(self.session, self.season, expand=999))

    def test_an_empty_pool_still_renders(self):
        from services import auction_rich as AR
        blocks, html_text, keyboard = AR.sets_view(self.session, self.season)
        self.assertIn("The pool is empty", html_text)
        self.assertEqual([], [b for b in blocks if b.get("type") == "details"])
        self.assertIn("🔄 Refresh", self.labels(keyboard))

    def test_a_page_press_edits_in_html_when_rich_text_is_off(self):
        from services import auction_rich as AR
        from services import rich_message
        import config

        edits = []

        class Bot:
            # No ``_post`` at all: rich sending is unavailable, which is the
            # state every chat is in when the feature flag is off.
            async def edit_message_text(self, chat_id=None, message_id=None,
                                        text="", **kwargs):
                edits.append((text, kwargs.get("reply_markup")))
                return SimpleNamespace(message_id=message_id)

        self.many_sets(count=8)
        blocks, html_text, keyboard = AR.sets_view(self.session, self.season)
        previous = getattr(config, "RICH_TEXT_ENABLED", False)
        config.RICH_TEXT_ENABLED = False
        try:
            result = asyncio.run(AR.edit(Bot(), 1, 2, blocks,
                                         reply_markup=keyboard,
                                         html_text=html_text))
        finally:
            config.RICH_TEXT_ENABLED = previous
        # Without the HTML fallback this returned None and edited nothing, which
        # is a page button that does nothing at all.
        self.assertTrue(result)
        self.assertEqual(1, len(edits))
        self.assertIn("page 1/2", edits[0][0])
        self.assertIs(keyboard, edits[0][1])
        # The board must NOT get this: it has its own HTML edit path, so it
        # passes no html_text and still opts out.
        self.assertIsNone(asyncio.run(
            AR.edit(Bot(), 1, 2, [rich_message.paragraph("x")])))

    def test_a_refused_rich_edit_still_reaches_the_reader(self):
        from telegram.error import BadRequest
        from services import auction_rich as AR
        from services import rich_message
        import config

        edits = []

        class Bot:
            async def _post(self, endpoint, payload):
                raise BadRequest("can't parse rich message")

            async def edit_message_text(self, chat_id=None, message_id=None,
                                        text="", **kwargs):
                edits.append(text)
                return SimpleNamespace(message_id=message_id)

        rich_message.reset_support_latch()
        previous = getattr(config, "RICH_TEXT_ENABLED", False)
        config.RICH_TEXT_ENABLED = True
        try:
            result = asyncio.run(AR.edit(Bot(), 1, 2,
                                         [rich_message.paragraph("x")],
                                         html_text="<b>fallback</b>"))
        finally:
            config.RICH_TEXT_ENABLED = previous
        self.assertTrue(result)
        self.assertEqual(["<b>fallback</b>"], edits)

    def test_the_live_set_starts_open_and_the_html_twin_fits_one_message(self):
        from services import auction_rich as AR
        self.many_sets(count=8, big=25)
        self.start()
        blocks, html_text, _ = AR.sets_view(self.session, self.season)
        opened = [b for b in blocks
                  if b.get("type") == "details" and b.get("is_open")]
        self.assertTrue(opened, "the set being auctioned should not need a tap")
        # One page has to be one message, or the buttons end up on a part of it.
        self.assertLessEqual(len(html_text), AR.TEXT_LIMIT)
        json.dumps(blocks)


class RichTests(FeatureCase):

    def setUp(self):
        super().setUp()
        self.build_sets()
        self.lot = self.start()

    def test_every_builder_serialises_and_every_cell_is_aligned(self):
        from services import auction_rich as AR
        self.buy(self.mumbai)
        builds = [
            AR.board_blocks(self.session, self.season, now=NOW),
            AR.squad_view(self.session, self.season, self.mumbai)[0],
            AR.purses_view(self.session, self.season)[0],
            AR.sets_view(self.session, self.season)[0],
            AR.next_set_view(self.session, self.season)[0],
            AR.next_players_view(self.session, self.season)[0],
            AR.sold_view(self.session, self.season)[0],
            AR.unsold_view(self.session, self.season)[0],
            AR.info_menu(self.session, self.season)[0],
            AR.admin_help(bot_admin=True)[0],
        ]
        for blocks in builds:
            json.dumps(blocks)
            for cell in _cells(blocks):
                self.assertIn("align", cell)
                self.assertIn("valign", cell)

    def test_the_board_names_the_lot_the_set_and_every_franchise(self):
        from services import auction_rich as AR
        text = json.dumps(AR.board_blocks(self.session, self.season, now=NOW),
                          ensure_ascii=False)
        self.assertIn(self.lot.name, text)
        self.assertIn("Bats", text)
        for name in ("Mumbai", "Chennai", "Delhi"):
            self.assertIn(name, text)

    def test_a_refused_rich_send_falls_back_to_html(self):
        from telegram.error import BadRequest
        from services import auction_rich as AR
        from services import rich_message
        import config

        sent = []

        class Bot:
            async def _post(self, endpoint, payload):
                raise BadRequest("can't parse rich message")

            async def send_message(self, chat_id=None, text="", **kwargs):
                sent.append(text)
                return SimpleNamespace(message_id=1)

        rich_message.reset_support_latch()
        previous = getattr(config, "RICH_TEXT_ENABLED", False)
        config.RICH_TEXT_ENABLED = True
        try:
            asyncio.run(AR.send(Bot(), 1, [rich_message.paragraph("x")],
                                "<b>fallback</b>"))
        finally:
            config.RICH_TEXT_ENABLED = previous
        self.assertEqual(["<b>fallback</b>"], sent)

    def test_a_long_html_view_is_split_without_cutting_a_quote(self):
        from services import auction_rich as AR
        sections = [f"🗂 <b>Set {i}</b>\n<blockquote expandable>"
                    + "\n".join(f"player {i}-{j} " + "x" * 60 for j in range(30))
                    + "</blockquote>" for i in range(6)]
        text = "head\n\n" + "\n\n".join(sections)
        parts = AR.html_parts(text)
        self.assertTrue(len(parts) > 1)
        for part in parts:
            self.assertLessEqual(len(part), AR.TEXT_LIMIT)
            self.assertEqual(part.count("<blockquote"),
                             part.count("</blockquote>"),
                             "a quote was cut in half")
        self.assertEqual(text.count("player"), sum(p.count("player")
                                                   for p in parts))

    def test_the_new_block_shapes(self):
        from services import rich_message as R
        self.assertEqual({"type": "pre", "text": "x", "language": "text"},
                         R.pre("x", "text"))
        self.assertEqual(
            {"type": "list", "items": [
                {"blocks": [{"type": "paragraph", "text": "a"}], "label": "1."},
                {"blocks": [{"type": "paragraph", "text": "b"}], "label": "2."}]},
            R.list_block(["a", "b"], ordered=True))
        self.assertEqual(
            {"type": "list",
             "items": [{"blocks": [{"type": "paragraph", "text": "a"}]}]},
            R.list_block(["a"]))
        self.assertEqual({"type": "pullquote", "text": "q"}, R.pullquote("q"))
        self.assertEqual({"type": "url", "text": "Al",
                          "url": "tg://user?id=5"}, R.mention("Al", 5))
        self.assertEqual("Al", R.mention("Al", None))


# ══════════════════════════════════════════════════════════════════════
# What reaches the room
# ══════════════════════════════════════════════════════════════════════

class FakeBot:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.pinned = []
        self.unpinned = []
        self.next_id = 500

    async def send_message(self, chat_id=None, text="", **kwargs):
        self.next_id += 1
        self.sent.append(text)
        return SimpleNamespace(message_id=self.next_id)

    async def edit_message_text(self, chat_id=None, message_id=None, text="",
                                **kwargs):
        self.edits.append((message_id, text, kwargs.get("reply_markup")))
        return True

    async def pin_chat_message(self, chat_id=None, message_id=None, **kwargs):
        self.pinned.append(message_id)
        return True

    async def unpin_chat_message(self, chat_id=None, message_id=None, **kwargs):
        self.unpinned.append(message_id)
        return True


class AnnouncementTests(FeatureCase):

    def setUp(self):
        super().setUp()
        from services import auction_scheduler as S
        self.S = S
        self._card = S.SEND_LOT_CARD
        S.SEND_LOT_CARD = False        # the caption, sent as text
        self.build_sets()
        self.lot = self.start()
        self.bot = FakeBot()

    def tearDown(self):
        self.S.SEND_LOT_CARD = self._card
        super().tearDown()

    def tick(self, now):
        asyncio.run(self.S._tick_one(SimpleNamespace(bot=self.bot),
                                     self.session, self.season, now))

    def test_a_new_player_is_announced_with_his_card_and_a_pinned_board(self):
        self.tick(NOW + timedelta(seconds=1))
        said = "\n".join(self.bot.sent)
        self.assertIn(f"LOT {self.lot.lot_no}", said)
        self.assertIn("Set: <b>Bats</b>", said)
        self.assertEqual([self.season.board_message_id], self.bot.pinned)
        self.assertEqual(self.lot.id, self.season.board_lot_id)

    def test_every_lot_gets_a_fresh_pinned_board(self):
        self.tick(NOW + timedelta(seconds=1))
        first_board = self.season.board_message_id
        self.tick(self.lot.deadline_at + timedelta(seconds=1))   # passes it
        self.assertNotEqual(first_board, self.season.board_message_id)
        self.assertEqual(2, len(self.bot.pinned))
        self.assertEqual(self.season.board_message_id, self.bot.pinned[-1])
        # Only the old board is unpinned — never "whatever was pinned last".
        self.assertEqual([first_board], self.bot.unpinned)

    def test_a_burst_of_bids_is_one_small_message(self):
        self.tick(NOW + timedelta(seconds=1))
        before = len(self.bot.sent)
        lot = self.lot
        for i in range(6):
            lot = self.A.place_bid(self.session, self.season, lot,
                                   (self.mumbai, self.chennai)[i % 2],
                                   self.A.next_min_bid(self.season, lot),
                                   now=NOW + timedelta(seconds=2))
        self.session.commit()
        self.tick(NOW + timedelta(seconds=3))
        new = self.bot.sent[before:]
        self.assertEqual(1, len(new), "six bids, one message")
        self.assertIn("💸", new[0])
        self.assertIn("Chennai", new[0])
        self.assertIn("leads", new[0])
        # And the pinned board is edited, with the quick-bid buttons on it.
        self.assertTrue(self.bot.edits)
        self.assertIsNotNone(self.bot.edits[-1][2], "the board carries buttons")

    def test_bid_messages_keep_under_the_groups_flood_limit(self):
        self.tick(NOW + timedelta(seconds=1))
        lot = self.A.place_bid(self.session, self.season, self.lot, self.mumbai,
                               self.lot.base_price_lakh,
                               now=NOW + timedelta(seconds=2))
        self.session.commit()
        self.tick(NOW + timedelta(seconds=3))
        before = len(self.bot.sent)
        # A second bid straight after the first message waits for the gap…
        lot = self.A.place_bid(self.session, self.season, lot, self.chennai,
                               self.A.next_min_bid(self.season, lot),
                               now=NOW + timedelta(seconds=4))
        self.session.commit()
        self.tick(NOW + timedelta(seconds=5))
        self.assertEqual(before, len(self.bot.sent), "held inside the gap")
        # …and goes out on the first tick after it, not lost.
        self.S._last_bid_message[self.season.id] -= self.S.BID_MESSAGE_GAP
        self.tick(NOW + timedelta(seconds=6))
        self.assertEqual(before + 1, len(self.bot.sent))
        self.assertIn("Chennai", self.bot.sent[-1])

    def test_a_rate_limited_bid_line_is_sent_again_not_lost(self):
        from telegram.error import RetryAfter
        self.tick(NOW + timedelta(seconds=1))
        self.A.place_bid(self.session, self.season, self.lot, self.mumbai,
                         self.lot.base_price_lakh,
                         now=NOW + timedelta(seconds=2))
        self.session.commit()
        real = self.bot.send_message

        async def flooded(chat_id=None, text="", **kwargs):
            if "💸" in text:
                raise RetryAfter(3)
            return await real(chat_id=chat_id, text=text, **kwargs)

        self.bot.send_message = flooded
        self.tick(NOW + timedelta(seconds=3))
        self.assertFalse([t for t in self.bot.sent if "💸" in t])
        self.bot.send_message = real
        self.tick(NOW + timedelta(seconds=4))
        self.assertEqual(1, len([t for t in self.bot.sent if "💸" in t]),
                         "the held burst goes out once the flood clears")

    def test_a_sale_is_announced_in_rich_html(self):
        self.A.place_bid(self.session, self.season, self.lot, self.mumbai,
                         self.lot.base_price_lakh, now=NOW)
        self.session.commit()
        self.tick(self.lot.deadline_at + timedelta(seconds=1))
        said = "\n".join(self.bot.sent)
        self.assertIn("<b>SOLD!</b>", said)
        self.assertIn("<blockquote>", said)
        self.assertIn("Mumbai", said)


# ══════════════════════════════════════════════════════════════════════
# Focus mode
# ══════════════════════════════════════════════════════════════════════

class FocusModeTests(FeatureCase):
    """The switch, end to end: the column, /afocus, and what locks the room.

    The gate's own rules are pinned in tests/test_auction_focus.py against
    plain objects; what is pinned here is the half that needs a database — a
    new season is created locked, the command flips it and says so, and what
    the middleware asks about (``locks_the_room``) follows the status.
    """

    def setUp(self):
        super().setUp()
        from services import auction_focus
        self.F = auction_focus
        self.F.invalidate()
        self.addCleanup(self.F.invalidate)

    def test_a_new_auction_is_locked_by_default(self):
        self.assertTrue(self.A.focus_mode_on(self.season))

    def test_the_lock_follows_the_status(self):
        # Setup locks nothing: retention and the pool happen here, and an
        # auction can sit in setup for weeks.
        self.assertFalse(self.F.locks_the_room(self.season))
        self.build_sets()
        self.start()
        self.assertTrue(self.F.locks_the_room(self.season))
        self.A.pause(self.session, self.season)
        self.session.commit()
        self.assertTrue(self.F.locks_the_room(self.season),
                        "a paused auction is still mid-flight")
        self.A.cancel(self.session, self.season)
        self.session.commit()
        self.assertFalse(self.F.locks_the_room(self.season))

    def test_afocus_reads_it_back_without_changing_it(self):
        from handlers import auction as H
        with AdminEnv(ALICE):
            out = self.run_handler(H.afocus_handler, ALICE)
        self.assertIn("on", out.replies[0])
        self.assertIn("/afocus off", out.replies[0])
        self.assertTrue(self.A.focus_mode_on(self.season))

    def test_afocus_off_unlocks_the_room_and_announces_it(self):
        from handlers import auction as H
        self.build_sets()
        self.start()
        with AdminEnv(ALICE):
            self.run_handler(H.afocus_handler, ALICE, ["off"])
        self.session.expire_all()
        self.assertFalse(self.A.focus_mode_on(self.season))
        self.assertFalse(self.F.locks_the_room(self.season))
        said = [e.headline for e in
                self.A.recent_events(self.session, self.season.id, limit=5)]
        self.assertTrue(any("focus" in line.lower() for line in said), said)

        with AdminEnv(ALICE):
            self.run_handler(H.afocus_handler, ALICE, ["on"])
        self.session.expire_all()
        self.assertTrue(self.A.focus_mode_on(self.season))

    def test_it_is_an_admin_switch(self):
        from handlers import auction as H
        with AdminEnv(CAROL):                    # ALICE owns a team, runs nothing
            out = self.run_handler(H.afocus_handler, ALICE, ["off"])
        self.assertIn("auction admins", out.replies[0].lower())
        self.assertTrue(self.A.focus_mode_on(self.season))

    def test_a_typo_is_refused_rather_than_guessed(self):
        from handlers import auction as H
        with AdminEnv(ALICE):
            out = self.run_handler(H.afocus_handler, ALICE, ["maybe"])
        self.assertIn("/afocus on", out.replies[0])
        self.assertTrue(self.A.focus_mode_on(self.season))

    def test_the_cached_lock_state_is_dropped_when_the_auction_starts(self):
        """/astart must land in the room now, not at the end of a TTL."""
        chat_id = self.season.chat_id
        self.assertIsNone(self.F.locked_season_for_chat(chat_id))
        self.build_sets()
        self.start()
        self.assertEqual(self.season.name,
                         self.F.locked_season_for_chat(chat_id))

    def test_the_next_season_inherits_the_switch(self):
        from handlers import auction as H
        with AdminEnv(ALICE):
            self.run_handler(H.afocus_handler, ALICE, ["off"])
        self.session.expire_all()
        clone = self.A.clone_season(self.session, self.season,
                                    f"Features {self.tag} II")
        self.session.commit()
        self.assertFalse(self.A.focus_mode_on(clone),
                         "a room that turned the lock off meant the room")


# ══════════════════════════════════════════════════════════════════════
# The purse, the bid ladder, and one pair of hands per franchise
# ══════════════════════════════════════════════════════════════════════

class OpeningPurseTests(FeatureCase):
    """Changing the season's purse moves the whole field with it."""

    def test_it_carries_every_franchise(self):
        self.A.set_opening_purse(self.session, self.season, 12_000)
        self.session.commit()
        for franchise in self.A.franchises(self.session, self.season.id):
            self.assertEqual(12_000, franchise.purse_total_lakh)
            self.assertEqual(12_000, franchise.purse_remaining_lakh)
        self.assert_ledger_agrees("after an opening-purse change")

    def test_what_was_spent_stays_spent(self):
        self.build_sets()
        self.start()
        lot = self.A.current_lot(self.session, self.season)
        self.buy(self.mumbai, lot, price=2_000)
        self.A.set_opening_purse(self.session, self.season, 12_000)
        self.session.commit()
        self.session.refresh(self.mumbai)
        self.assertEqual(12_000, self.mumbai.purse_total_lakh)
        self.assertEqual(10_000, self.mumbai.purse_remaining_lakh,
                         "the ₹20 Cr already spent came out of the new purse")
        self.assert_ledger_agrees("after a purse change mid-auction")

    def test_a_cut_that_would_overdraw_is_refused_by_name(self):
        self.build_sets()
        self.start()
        self.buy(self.mumbai, price=2_000)
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.set_opening_purse(self.session, self.season, 1_000)
        self.assertIn("Mumbai", str(caught.exception))
        self.session.rollback()
        self.session.refresh(self.chennai)
        self.assertEqual(self.purse_lakh, self.chennai.purse_total_lakh,
                         "a refused change must leave the field untouched")

    def test_the_move_is_on_the_record(self):
        self.A.set_opening_purse(self.session, self.season, 12_000)
        self.session.commit()
        kinds = [row.kind for row in
                 self.A.ledger(self.session, self.mumbai.id)]
        self.assertIn(self.A.LEDGER_CORRECTION, kinds)

    def test_saving_the_same_number_leaves_a_custom_purse_alone(self):
        """Otherwise every save of the anti-snipe numbers resets the field."""
        kochi = self.A.create_franchise(self.session, self.season, "Kochi",
                                        owner_tg_id=ERIN, quiet=True,
                                        purse_total_lakh=8_000)
        self.session.commit()
        self.assertEqual([], self.A.set_opening_purse(
            self.session, self.season, self.purse_lakh))
        self.session.refresh(kochi)
        self.assertEqual(8_000, kochi.purse_total_lakh)
        self.assert_ledger_agrees("after a no-op purse save")

        # …but when the number really moves, it moves with everybody else.
        self.A.set_opening_purse(self.session, self.season, 12_000)
        self.session.commit()
        self.session.refresh(kochi)
        self.assertEqual(12_000, kochi.purse_total_lakh)
        self.assert_ledger_agrees("after the purse moved")

    def test_the_ledger_row_records_the_balance_after_the_move(self):
        """``balance_after`` is stamped off the row, so it must be re-read."""
        self.A.set_opening_purse(self.session, self.season, 12_000)
        self.session.commit()
        latest = self.A.ledger(self.session, self.mumbai.id, limit=1)[0]
        self.assertEqual(self.A.LEDGER_CORRECTION, latest.kind)
        self.assertEqual(12_000, latest.balance_after)
        self.assertEqual(12_000, self.mumbai.purse_remaining_lakh)

    def test_a_sale_landing_mid_save_is_not_overwritten(self):
        """The move is one conditional statement, not a read-then-write.

        Two sessions on purpose: the second is the sweeper selling a lot while
        the first has the settings page open, which is the case the website
        makes possible and a read-modify-write would lose.
        """
        from database import get_session
        self.build_sets()
        self.start()
        lot = self.A.current_lot(self.session, self.season)
        self.buy(self.mumbai, lot, price=2_000)      # Mumbai: 10000 → 8000

        other = get_session()
        try:
            season = other.get(type(self.season), self.season.id)
            # This session still believes Mumbai has 8000 …
            mumbai = [f for f in self.A.franchises(other, season.id)
                      if f.name == "Mumbai"][0]
            self.assertEqual(8_000, mumbai.purse_remaining_lakh)
            # … and meanwhile another ₹10 Cr leaves the purse.
            self.A.correct_purse(self.session, self.season, self.mumbai,
                                 -1_000, note="a sale lands mid-save")
            self.session.commit()

            self.A.set_opening_purse(other, season, 12_000)
            other.commit()
        finally:
            other.close()

        self.session.expire_all()
        # 12000 total, 4000 spent (2000 + 1000 + … in lakh) — the debit that
        # landed mid-save survives rather than being written over.
        self.assertEqual(12_000, self.mumbai.purse_total_lakh)
        self.assertEqual(12_000 - 3_000, self.mumbai.purse_remaining_lakh)
        self.assert_ledger_agrees("after a concurrent debit and purse change")

    def test_a_new_franchise_still_opens_at_the_season_purse(self):
        self.A.set_opening_purse(self.session, self.season, 12_000)
        late = self.A.create_franchise(self.session, self.season, "Kolkata",
                                       owner_tg_id=ERIN, quiet=True)
        self.session.commit()
        self.assertEqual(12_000, late.purse_total_lakh)


class DirectBidTests(FeatureCase):
    """/adirect off leaves the ladder: bare /bid and the buttons."""

    def setUp(self):
        super().setUp()
        self.build_sets()
        # Real time, not the suite's frozen NOW: these drive the /bid HANDLER,
        # which reads the clock itself.
        self.start(now=datetime.utcnow())
        self.lot = self.A.current_lot(self.session, self.season)

    def test_it_is_on_by_default(self):
        self.assertTrue(self.A.direct_bids_on(self.season))

    def test_a_typed_amount_is_refused_when_it_is_off(self):
        from handlers import auction as H
        with AdminEnv(CAROL):
            self.A.set_direct_bids(self.session, self.season, False)
            self.session.commit()
        out = self.run_handler(H.bid_handler, ALICE, ("5",))
        self.assertIn("Direct bids are off", out.replies[-1])
        self.session.expire_all()
        self.assertIsNone(self.A.current_lot(self.session, self.season)
                          .current_bid_lakh, "the bid must not have landed")

    def test_bare_bid_still_works_when_it_is_off(self):
        from handlers import auction as H
        self.A.set_direct_bids(self.session, self.season, False)
        self.session.commit()
        out = self.run_handler(H.bid_handler, ALICE)
        self.session.expire_all()
        lot = self.A.current_lot(self.session, self.season)
        self.assertEqual(lot.base_price_lakh, lot.current_bid_lakh,
                         f"bare /bid was refused: {out.replies}")

    def test_adirect_reads_back_and_flips(self):
        from handlers import auction as H
        with AdminEnv(ALICE):
            out = self.run_handler(H.adirect_handler, ALICE)
            self.assertIn("on", out.replies[-1])
            self.run_handler(H.adirect_handler, ALICE, ("off",))
        self.session.expire_all()
        self.assertFalse(self.A.direct_bids_on(self.season))
        said = [e.headline for e in
                self.A.recent_events(self.session, self.season.id, limit=5)]
        self.assertTrue(any("Direct bids" in line for line in said), said)

    def test_it_is_an_admin_switch(self):
        from handlers import auction as H
        with AdminEnv(CAROL):
            out = self.run_handler(H.adirect_handler, ALICE, ("off",))
        self.assertIn("auction admins", out.replies[-1].lower())
        self.assertTrue(self.A.direct_bids_on(self.season))

    def test_the_next_season_inherits_it(self):
        self.A.set_direct_bids(self.session, self.season, False)
        clone = self.A.clone_season(self.session, self.season,
                                    f"Ladder {self.tag}")
        self.session.commit()
        self.assertFalse(self.A.direct_bids_on(clone))


class OneBidderPerFranchiseTests(FeatureCase):
    """Two co-owners bidding one lot is a franchise racing itself."""

    def setUp(self):
        super().setUp()
        self.A.add_co_owner(self.session, self.mumbai, CAROL)
        self.build_sets()
        self.start()
        self.lot = self.A.current_lot(self.session, self.season)
        self.session.commit()

    def _bid(self, tg_id, amount=None):
        lot = self.A.current_lot(self.session, self.season)
        return self.A.place_bid(
            self.session, self.season, lot, self.mumbai,
            amount or self.A.next_min_bid(self.season, lot), now=NOW,
            by_tg_id=tg_id)

    def test_the_first_bidder_holds_the_lot(self):
        self._bid(ALICE)
        # Chennai raises, so Mumbai may legally bid again — but only Alice.
        self.A.place_bid(self.session, self.season, self.lot, self.chennai,
                         self.A.next_min_bid(self.season, self.lot), now=NOW,
                         by_tg_id=BOB)
        with self.assertRaises(self.A.AuctionError) as caught:
            self._bid(CAROL)
        self.assertIn("only one of you", str(caught.exception))
        self._bid(ALICE)          # the holder carries on untouched

    def test_the_claim_is_per_lot(self):
        self._bid(ALICE)
        self.A.sell_lot(self.session, self.season,
                        self.A.current_lot(self.session, self.season), now=NOW)
        self.A.open_next_lot(self.session, self.season, now=NOW)
        self.session.commit()
        self._bid(CAROL)          # a new lot is open to either of them

    def test_an_undone_bid_releases_the_claim(self):
        self._bid(ALICE)
        self.A.undo_last_bid(self.session, self.season,
                             self.A.current_lot(self.session, self.season),
                             now=NOW)
        self.session.commit()
        self._bid(CAROL)

    def test_an_admin_may_still_bid_for_the_franchise(self):
        self._bid(ALICE)
        self.A.place_bid(self.session, self.season, self.lot, self.chennai,
                         self.A.next_min_bid(self.season, self.lot), now=NOW,
                         by_tg_id=BOB)
        lot = self.A.current_lot(self.session, self.season)
        self.A.place_bid(self.session, self.season, lot, self.mumbai,
                         self.A.next_min_bid(self.season, lot), now=NOW,
                         by_tg_id=DAVE, by_admin=True)

    def test_the_refusal_names_the_holder(self):
        from models import User
        self.session.add(User(telegram_id=ALICE, first_name="Alice"))
        self.session.flush()
        self._bid(ALICE)
        self.A.place_bid(self.session, self.season, self.lot, self.chennai,
                         self.A.next_min_bid(self.season, self.lot), now=NOW,
                         by_tg_id=BOB)
        with self.assertRaises(self.A.AuctionError) as caught:
            self._bid(CAROL)
        self.assertIn("Alice", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

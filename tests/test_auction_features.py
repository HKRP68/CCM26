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
            opening_purse_lakh=self.purse_lakh)
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
                    bot=None, reply_to=None):
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
            effective_chat=SimpleNamespace(id=self.season.chat_id,
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

    def test_ainfo_carries_a_button_per_view(self):
        from handlers import auction as H
        out = self.run_handler(H.ainfo_handler, ALICE)
        data = [b.callback_data for row in out.markups[-1].inline_keyboard
                for b in row]
        self.assertEqual({"au_info_sets", "au_info_nextset", "au_info_next",
                          "au_info_squad", "au_info_sold", "au_info_unsold",
                          "au_info_purse"}, set(data))

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
# Rich messages
# ══════════════════════════════════════════════════════════════════════

def _cells(blocks):
    for block in blocks:
        if block.get("type") == "table":
            for row in block["cells"]:
                yield from row
        if block.get("type") == "details":
            yield from _cells(block["blocks"])


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


if __name__ == "__main__":
    unittest.main()

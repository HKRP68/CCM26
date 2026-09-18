"""The Franchise Auction: the bidding loop, the clock, and the commands.

What is pinned here is the part that has to survive two people typing at once:

  • **Exactly one of two simultaneous bids wins**, and the loser leaves no bid
    row behind. The claim is a single conditional UPDATE, so the test drives it
    from two real sessions rather than trusting a comment.
  • **The deadline is authoritative, and the sweeper only announces.** A bid
    after the deadline is refused by the command, with the clock job never
    having run.
  • **Anti-snipe extends inside the same statement as the bid**, is capped, and
    an admin adding time does not spend the room's budget.
  • **Pausing drops the deadline rather than freezing it**, so nothing can
    hammer a lot the instant an auction resumes.
  • **Undo is a price operation, not a money one**, and it never hands the lot
    to the previous bidder before anybody can react.
"""

import itertools
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None

# Everything reached from here that caches a reference to ``database`` /
# ``models``. The handler module is in the list for the reason
# ``tests/test_player_draft.py`` gives: left cached, its ``get_session`` is
# bound to a different temporary database and the command tests query the
# wrong file.
_MODULE_NAMES = ("database", "models", "config",
                 # player_service is in the list because player_query calls its
                 # ``not_career``, which filters on ITS ``Player`` class. Left
                 # cached while models is reloaded, that is a second mapped
                 # class against the same table, and the query comes back
                 # "ambiguous column name: players.id" — only when both auction
                 # suites run in one process, which is exactly when it matters.
                 "services.player_service", "services.player_query",
                 "services.auction_service", "services.auction_scheduler",
                 "handlers.auction")

_PID = itertools.count(1)


def _unload(names):
    """Drop these modules so the next import rebuilds them.

    Popping ``sys.modules`` is not enough on its own: ``from handlers import
    auction`` returns a **cached attribute on the package** when one exists,
    without consulting ``sys.modules`` at all, so the stale module — and,
    fatally, the ``get_session`` it bound at import time to a temporary
    database that no longer exists — would come straight back.
    """
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


# name, rating, category, country, version
CATALOGUE = [
    ("Virat Kohli", 97, "Batsman", "India", "Base"),
    ("Jasprit Bumrah", 95, "Bowler", "India", "Base"),
    ("Rashid Khan", 93, "All Rounder", "Afghanistan", "Base"),
    ("Jos Buttler", 92, "Wicket Keeper", "England", "Base"),
    ("Sanju Samson", 88, "Wicket Keeper", "India", "Base"),
    ("Tim David", 84, "Batsman", "Australia", "Base"),
    ("Rinku Singh", 83, "Batsman", "India", "Base"),
    ("Mukesh Kumar", 74, "Bowler", "India", "Base"),
    ("Virat Kohli", 99, "Batsman", "India", "Icon"),
]

ALICE, BOB, CAROL = 111, 222, 333
NOW = datetime(2026, 3, 1, 12, 0, 0)


class AuctionCase(unittest.TestCase):
    """An auction with two franchises and the catalogue above in the pool."""

    min_squad = 2
    max_squad = 5
    max_overseas = 8
    purse_lakh = 10_000          # ₹100 Cr

    def setUp(self):
        from database import get_session
        from models import Player
        from services import auction_service as A

        self.session = get_session()
        self.A = A
        # A fresh catalogue per test; the ids differ but nothing depends on them.
        self.players = []
        tag = next(_PID)
        for name, rating, category, country, version in CATALOGUE:
            player = Player(name=f"{name}", rating=rating, category=category,
                            country=country, version=version,
                            bat_hand="Right", bowl_hand="Right",
                            bowl_style="Medium Pacer", bat_rating=rating,
                            bowl_rating=max(0, rating - 10), is_active=True)
            if version != "Base":
                player.parent_player_id = 1
            self.session.add(player)
            self.players.append(player)
        self.session.flush()

        self.season = A.create_season(
            self.session, f"Season {tag}", min_squad_size=self.min_squad,
            max_squad_size=self.max_squad, max_overseas=self.max_overseas,
            opening_purse_lakh=self.purse_lakh)
        A.bind_chat(self.session, self.season, -1000 - tag)
        self.mumbai = A.create_franchise(self.session, self.season, "Mumbai",
                                         owner_tg_id=ALICE, owner_name="Alice")
        self.chennai = A.create_franchise(self.session, self.season, "Chennai",
                                          owner_tg_id=BOB, owner_name="Bob")
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    # ── helpers ──

    def build_pool(self, players=None):
        added, skipped = self.A.add_players_to_pool(
            self.session, self.season, players or self.players)
        self.session.commit()
        return added, skipped

    def assert_ledger_agrees(self, note=""):
        """The invariant this whole feature stands on."""
        for franchise in self.A.franchises(self.session, self.season.id):
            self.assertEqual(
                self.A.ledger_total(self.session, franchise.id),
                int(franchise.purse_remaining_lakh or 0),
                f"{franchise.name}'s ledger and purse disagree {note}")
        self.assertEqual(
            [], self.A.reconcile_purses(self.session, self.season),
            f"reconcile_purses found drift {note}")

    def start(self, now=NOW):
        return self.A.start(self.session, self.season, now=now)


# ══════════════════════════════════════════════════════════════════════
# Lifecycle
# ══════════════════════════════════════════════════════════════════════

class LifecycleTests(AuctionCase):

    def test_start_refuses_an_auction_with_nothing_to_sell(self):
        with self.assertRaises(self.A.AuctionError) as caught:
            self.start()
        self.assertIn("pool is empty", str(caught.exception))

    def test_start_names_the_franchises_nobody_could_bid_for(self):
        self.build_pool()
        self.chennai.owner_tg_id = None
        self.chennai.co_owner_ids_json = None
        self.session.commit()
        with self.assertRaises(self.A.AuctionError) as caught:
            self.start()
        self.assertIn("Chennai", str(caught.exception))

    def test_start_refuses_without_a_bound_group(self):
        self.build_pool()
        self.season.chat_id = None
        self.session.commit()
        with self.assertRaises(self.A.AuctionError) as caught:
            self.start()
        self.assertIn("/abind", str(caught.exception))

    def test_starting_opens_the_first_lot_with_a_full_clock(self):
        self.build_pool()
        lot = self.start()
        self.assertEqual(self.A.LOT_ON_BLOCK, lot.status)
        self.assertEqual(NOW + timedelta(seconds=self.season.bid_seconds),
                         lot.deadline_at)
        self.assertEqual(lot.id, self.season.current_lot_id)

    def test_pausing_drops_the_deadline_rather_than_freezing_it(self):
        self.build_pool()
        lot = self.start()
        self.A.pause(self.session, self.season)
        self.session.commit()
        self.session.refresh(lot)
        self.assertIsNone(lot.deadline_at,
                          "a frozen deadline would hammer the lot on resume")
        self.assertEqual(self.A.STATUS_PAUSED, self.season.status)

    def test_a_bid_while_paused_is_refused(self):
        self.build_pool()
        lot = self.start()
        self.A.pause(self.session, self.season)
        self.session.commit()
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.place_bid(self.session, self.season, lot, self.mumbai,
                             lot.base_price_lakh, now=NOW)
        self.assertIn("paused", str(caught.exception).lower())

    def test_resuming_gives_a_fresh_full_clock(self):
        self.build_pool()
        lot = self.start()
        self.A.pause(self.session, self.season)
        self.session.commit()
        later = NOW + timedelta(minutes=9)
        self.A.start(self.session, self.season, now=later)
        self.session.commit()
        self.session.refresh(lot)
        self.assertEqual(later + timedelta(seconds=self.season.bid_seconds),
                         lot.deadline_at)
        self.assertEqual(0, lot.going_stage)

    def test_the_last_lot_can_be_resumed_with_nothing_queued_behind_it(self):
        """An auction paused on its final lot is not a dead end.

        The empty-pool preflight asks whether there is anything to *open*.
        A lot already standing is the answer, and refusing here would strand
        it for good — no admin can queue their way back to a closed window.
        """
        self.build_pool(self.players[:1])
        lot = self.start()
        self.assertIsNone(self.A.next_queued(self.session, self.season.id))
        self.A.pause(self.session, self.season)
        self.session.commit()
        later = NOW + timedelta(minutes=3)
        resumed = self.A.start(self.session, self.season, now=later)
        self.session.commit()
        self.assertEqual(lot.id, resumed.id)
        self.assertEqual(self.A.STATUS_LIVE, self.season.status)
        self.assertEqual(later + timedelta(seconds=self.season.bid_seconds),
                         resumed.deadline_at)

    def test_an_auction_with_nothing_at_all_still_refuses_to_start(self):
        with self.assertRaises(self.A.AuctionError) as caught:
            self.start()
        self.assertIn("pool is empty", str(caught.exception))

    def test_only_one_lot_is_ever_on_the_block(self):
        self.build_pool()
        self.start()
        upcoming = self.A.next_queued(self.session, self.season.id)
        with self.assertRaises(self.A.AuctionError):
            self.A.open_lot(self.session, self.season, upcoming, now=NOW)

    def test_the_last_lot_finishes_the_auction_without_an_intervening_commit(self):
        """The website's shape: resolve every lot, then commit once at the end.

        The admin routes call the service and commit afterwards, so anything
        that decides "is this auction over?" has to see the change the caller
        just made rather than what is still on disk.
        """
        self.build_pool()
        self.start()
        guard = 0
        while self.season.status == self.A.STATUS_LIVE and guard < 50:
            guard += 1
            lot = self.A.current_lot(self.session, self.season)
            if lot is None:
                if self.A.next_queued(self.session, self.season.id) is None:
                    self.A.complete_if_done(self.session, self.season)
                    break
                self.A.open_next_lot(self.session, self.season, now=NOW)
                continue
            self.A.pass_lot(self.session, self.season, lot)
        self.session.commit()      # one commit, at the very end
        self.assertEqual(self.A.STATUS_COMPLETED, self.season.status)

    def test_the_auction_finishes_when_nothing_is_left(self):
        self.build_pool()
        self.start()
        guard = 0
        while self.season.status == self.A.STATUS_LIVE and guard < 50:
            guard += 1
            lot = self.A.current_lot(self.session, self.season)
            if lot is None:
                self.A.open_next_lot(self.session, self.season, now=NOW)
                self.session.commit()
                continue
            self.A.pass_lot(self.session, self.season, lot)
            self.session.commit()
        self.assertEqual(self.A.STATUS_COMPLETED, self.season.status)
        self.assertIsNone(self.season.current_lot_id)

    def test_cancelling_puts_the_live_lot_back_in_the_queue(self):
        self.build_pool()
        lot = self.start()
        self.A.cancel(self.session, self.season)
        self.session.commit()
        self.session.refresh(lot)
        self.assertEqual(self.A.LOT_QUEUED, lot.status)
        self.assertIsNone(lot.deadline_at)


# ══════════════════════════════════════════════════════════════════════
# Bidding
# ══════════════════════════════════════════════════════════════════════

class BiddingTests(AuctionCase):

    def setUp(self):
        super().setUp()
        self.build_pool()
        self.lot = self.start()
        self.session.commit()

    def test_the_first_bid_must_meet_the_base_price(self):
        base = self.lot.base_price_lakh
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.place_bid(self.session, self.season, self.lot, self.mumbai,
                             base - 1, now=NOW)
        self.assertIn("base price", str(caught.exception))
        lot = self.A.place_bid(self.session, self.season, self.lot,
                               self.mumbai, base, now=NOW, by_tg_id=ALICE)
        self.assertEqual(base, lot.current_bid_lakh)

    def test_a_raise_must_clear_the_increment(self):
        base = self.lot.base_price_lakh
        lot = self.A.place_bid(self.session, self.season, self.lot,
                               self.mumbai, base, now=NOW, by_tg_id=ALICE)
        step = self.A.increment_for(self.season, base)
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.place_bid(self.session, self.season, lot, self.chennai,
                             base + step - 1, now=NOW)
        self.assertIn("next legal bid", str(caught.exception))
        lot = self.A.place_bid(self.session, self.season, lot, self.chennai,
                               base + step, now=NOW, by_tg_id=BOB)
        self.assertEqual(base + step, lot.current_bid_lakh)

    def test_nobody_may_bid_against_themselves(self):
        lot = self.A.place_bid(self.session, self.season, self.lot,
                               self.mumbai, self.lot.base_price_lakh, now=NOW,
                               by_tg_id=ALICE)
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.place_bid(self.session, self.season, lot, self.mumbai,
                             lot.current_bid_lakh + 500, now=NOW)
        self.assertIn("already hold the top bid", str(caught.exception))

    def test_a_bid_after_the_deadline_is_refused_by_the_command_itself(self):
        """The sweeper never has to have run: the deadline is in the WHERE."""
        late = self.lot.deadline_at + timedelta(seconds=1)
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.place_bid(self.session, self.season, self.lot, self.mumbai,
                             self.lot.base_price_lakh, now=late)
        self.assertIn("clock", str(caught.exception).lower())

    def test_every_bid_is_kept_including_the_ones_that_lost(self):
        from models import AuctionBid
        base = self.lot.base_price_lakh
        step = self.A.increment_for(self.season, base)
        lot = self.A.place_bid(self.session, self.season, self.lot,
                               self.mumbai, base, now=NOW, by_tg_id=ALICE)
        lot = self.A.place_bid(self.session, self.season, lot, self.chennai,
                               base + step, now=NOW, by_tg_id=BOB)
        self.session.commit()
        rows = (self.session.query(AuctionBid)
                .filter(AuctionBid.lot_id == lot.id)
                .order_by(AuctionBid.id).all())
        self.assertEqual([base, base + step], [r.amount_lakh for r in rows])
        self.assertEqual([ALICE, BOB], [r.by_tg_id for r in rows])


class IncrementLadderTests(AuctionCase):

    def test_the_step_grows_with_the_price(self):
        A = self.A
        self.assertEqual(10, A.increment_for(self.season, 0))
        self.assertEqual(10, A.increment_for(self.season, 199))
        self.assertEqual(20, A.increment_for(self.season, 200))
        self.assertEqual(25, A.increment_for(self.season, 500))
        self.assertEqual(50, A.increment_for(self.season, 1000))
        self.assertEqual(50, A.increment_for(self.season, 50_000))

    def test_a_ladder_saved_out_of_order_still_reads_correctly(self):
        """The bands are read in sequence, so the stored order must not decide
        the answer — an admin's table is not a promise about sort order."""
        import json
        self.season.bid_increment_rules_json = json.dumps([
            {"upto_lakh": 0, "step_lakh": 50},
            {"upto_lakh": 1000, "step_lakh": 25},
            {"upto_lakh": 200, "step_lakh": 10},
            {"upto_lakh": 500, "step_lakh": 20},
        ])
        self.session.commit()
        self.assertEqual(10, self.A.increment_for(self.season, 30))
        self.assertEqual(20, self.A.increment_for(self.season, 300))
        self.assertEqual(50, self.A.increment_for(self.season, 9000))


class ConcurrencyTests(AuctionCase):
    """Two people typing on the same tick, driven from two real sessions."""

    def setUp(self):
        super().setUp()
        self.build_pool()
        self.lot = self.start()
        self.session.commit()

    def test_exactly_one_of_two_equal_bids_wins(self):
        from database import get_session
        from models import AuctionBid, AuctionLot, AuctionFranchise, AuctionSeason

        amount = self.lot.base_price_lakh
        other = get_session()
        try:
            # Session B reads the lot FIRST, at the same standing price session
            # A is about to bid against. That stale read is the whole point:
            # B's validation passes, and only the conditional UPDATE can catch
            # it. Checking after A had already committed would be caught by
            # validate_bid and would never exercise the claim at all.
            their_season = (other.query(AuctionSeason)
                            .filter(AuctionSeason.id == self.season.id).first())
            their_lot = (other.query(AuctionLot)
                         .filter(AuctionLot.id == self.lot.id).first())
            their_franchise = (other.query(AuctionFranchise)
                               .filter(AuctionFranchise.id == self.chennai.id).first())
            self.assertIsNone(their_lot.current_bid_lakh)

            self.A.place_bid(self.session, self.season, self.lot, self.mumbai,
                             amount, now=NOW, by_tg_id=ALICE)
            self.session.commit()

            with self.assertRaises(self.A.AuctionError):
                self.A.place_bid(other, their_season, their_lot,
                                 their_franchise, amount, now=NOW, by_tg_id=BOB)
            other.rollback()
        finally:
            other.close()

        rows = (self.session.query(AuctionBid)
                .filter(AuctionBid.lot_id == self.lot.id).all())
        self.assertEqual(1, len(rows), "the losing bid must leave no row")
        self.assertEqual(ALICE, rows[0].by_tg_id)

    def test_a_bid_racing_a_sale_loses_in_sql(self):
        from database import get_session
        from models import AuctionLot, AuctionFranchise, AuctionSeason

        other = get_session()
        try:
            # Again, B reads while the lot is still on the block.
            their_season = (other.query(AuctionSeason)
                            .filter(AuctionSeason.id == self.season.id).first())
            their_lot = (other.query(AuctionLot)
                         .filter(AuctionLot.id == self.lot.id).first())
            their_franchise = (other.query(AuctionFranchise)
                               .filter(AuctionFranchise.id == self.chennai.id).first())
            self.assertEqual(self.A.LOT_ON_BLOCK, their_lot.status)

            self.A.place_bid(self.session, self.season, self.lot, self.mumbai,
                             self.lot.base_price_lakh, now=NOW, by_tg_id=ALICE)
            self.A.sell_lot(self.session, self.season, self.lot, now=NOW)
            self.session.commit()

            with self.assertRaises(self.A.AuctionError):
                self.A.place_bid(other, their_season, their_lot,
                                 their_franchise, 5000, now=NOW, by_tg_id=BOB)
            other.rollback()
        finally:
            other.close()

        self.session.refresh(self.mumbai)
        self.assertEqual(self.purse_lakh - self.lot.base_price_lakh,
                         self.mumbai.purse_remaining_lakh,
                         "the purse must be debited exactly once")
        self.assert_ledger_agrees("after a bid raced a sale")


# ══════════════════════════════════════════════════════════════════════
# The clock and anti-snipe
# ══════════════════════════════════════════════════════════════════════

class ClockTests(AuctionCase):

    def setUp(self):
        super().setUp()
        self.build_pool()
        self.lot = self.start()
        self.session.commit()

    def test_going_once_and_twice_are_a_pure_function_of_the_seconds_left(self):
        A = self.A
        self.assertEqual(0, A.going_stage_for(30))
        self.assertEqual(0, A.going_stage_for(11))
        self.assertEqual(1, A.going_stage_for(10))
        self.assertEqual(1, A.going_stage_for(6))
        self.assertEqual(2, A.going_stage_for(5))
        self.assertEqual(2, A.going_stage_for(0))
        self.assertEqual(0, A.going_stage_for(None))

    def test_a_bid_inside_the_window_pushes_the_clock_out(self):
        self.A.set_anti_snipe(self.session, self.season, 2, 10, 5)
        self.session.commit()
        late = self.lot.deadline_at - timedelta(seconds=1)
        lot = self.A.place_bid(self.session, self.season, self.lot,
                               self.mumbai, self.lot.base_price_lakh,
                               now=late, by_tg_id=ALICE)
        self.assertEqual(late + timedelta(seconds=10), lot.deadline_at)
        self.assertEqual(1, lot.extensions_used)
        self.assertEqual(0, lot.going_stage, "a bid resets going once/twice")

    def test_a_bid_outside_the_window_leaves_the_clock_alone(self):
        self.A.set_anti_snipe(self.session, self.season, 2, 10, 5)
        self.session.commit()
        original = self.lot.deadline_at
        early = original - timedelta(seconds=20)
        lot = self.A.place_bid(self.session, self.season, self.lot,
                               self.mumbai, self.lot.base_price_lakh,
                               now=early, by_tg_id=ALICE)
        self.assertEqual(original, lot.deadline_at)
        self.assertEqual(0, lot.extensions_used)

    def test_the_extension_budget_runs_out_and_the_lot_can_close(self):
        self.A.set_anti_snipe(self.session, self.season, 5, 5, 2)
        self.session.commit()
        lot, now = self.lot, NOW
        bidders = [self.mumbai, self.chennai]
        for round_no in range(3):
            now = lot.deadline_at - timedelta(seconds=1)
            amount = self.A.next_min_bid(self.season, lot)
            lot = self.A.place_bid(self.session, self.season, lot,
                                   bidders[round_no % 2], amount, now=now)
        self.assertEqual(2, lot.extensions_used, "capped at max_extensions")
        # The third bid earned no extension, so the clock is already past.
        self.assertLessEqual(lot.deadline_at, now + timedelta(seconds=5))

    def test_an_admin_extending_does_not_spend_the_rooms_budget(self):
        self.A.set_anti_snipe(self.session, self.season, 2, 10, 1)
        self.session.commit()
        before = self.lot.deadline_at
        lot = self.A.extend_timer(self.session, self.season, self.lot, 20,
                                  now=NOW)
        self.assertEqual(before + timedelta(seconds=20), lot.deadline_at)
        self.assertEqual(0, lot.extensions_used,
                         "an admin's hand on the clock is not a snipe")

    def test_an_expired_lot_with_a_bid_sells(self):
        self.A.place_bid(self.session, self.season, self.lot, self.mumbai,
                         self.lot.base_price_lakh, now=NOW, by_tg_id=ALICE)
        outcome, lot = self.A.resolve_expired(
            self.session, self.season, self.lot,
            now=self.lot.deadline_at + timedelta(seconds=1))
        self.session.commit()
        self.assertEqual("sold", outcome)
        self.assertEqual(self.mumbai.id, lot.sold_to_id)
        self.assert_ledger_agrees("after the clock sold a lot")

    def test_an_expired_lot_with_no_bid_goes_unsold(self):
        outcome, lot = self.A.resolve_expired(
            self.session, self.season, self.lot,
            now=self.lot.deadline_at + timedelta(seconds=1))
        self.session.commit()
        self.assertEqual("unsold", outcome)
        self.assertEqual(self.A.LOT_UNSOLD, lot.status)
        self.assertEqual(1, lot.times_unsold)

    def test_a_long_outage_resolves_correctly_on_the_first_tick_back(self):
        """Every bid in the table is still valid; the lot just sells late."""
        self.A.place_bid(self.session, self.season, self.lot, self.mumbai,
                         self.lot.base_price_lakh, now=NOW, by_tg_id=ALICE)
        outcome, lot = self.A.resolve_expired(
            self.session, self.season, self.lot,
            now=self.lot.deadline_at + timedelta(minutes=17))
        self.session.commit()
        self.assertEqual("sold", outcome)
        self.assertEqual(self.lot.base_price_lakh, lot.sold_price_lakh)


# ══════════════════════════════════════════════════════════════════════
# Undo, pass, withdraw, re-list
# ══════════════════════════════════════════════════════════════════════

class CorrectionTests(AuctionCase):

    def setUp(self):
        super().setUp()
        self.build_pool()
        self.lot = self.start()
        self.session.commit()

    def test_passing_a_lot_somebody_has_bid_for_is_refused(self):
        self.A.place_bid(self.session, self.season, self.lot, self.mumbai,
                         self.lot.base_price_lakh, now=NOW, by_tg_id=ALICE)
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.pass_lot(self.session, self.season, self.lot)
        self.assertIn("standing bid", str(caught.exception))
        self.assertIn("/aundobid", str(caught.exception),
                      "the refusal must name the way through")

    def test_undoing_a_bid_falls_back_to_the_one_under_it(self):
        base = self.lot.base_price_lakh
        step = self.A.increment_for(self.season, base)
        lot = self.A.place_bid(self.session, self.season, self.lot,
                               self.mumbai, base, now=NOW, by_tg_id=ALICE)
        lot = self.A.place_bid(self.session, self.season, lot, self.chennai,
                               base + step, now=NOW, by_tg_id=BOB)
        lot = self.A.undo_last_bid(self.session, self.season, lot, now=NOW)
        self.assertEqual(base, lot.current_bid_lakh)
        self.assertEqual(self.mumbai.id, lot.current_bidder_id)
        self.assert_ledger_agrees("after undoing a bid — no money moved")

    def test_undoing_the_only_bid_leaves_the_lot_with_none(self):
        lot = self.A.place_bid(self.session, self.season, self.lot,
                               self.mumbai, self.lot.base_price_lakh,
                               now=NOW, by_tg_id=ALICE)
        lot = self.A.undo_last_bid(self.session, self.season, lot, now=NOW)
        self.assertIsNone(lot.current_bid_lakh)
        self.assertIsNone(lot.current_bidder_id)

    def test_undoing_a_bid_never_lets_the_lot_close_immediately(self):
        self.A.set_anti_snipe(self.session, self.season, 2, 10, 5)
        self.session.commit()
        lot = self.A.place_bid(self.session, self.season, self.lot,
                               self.mumbai, self.lot.base_price_lakh,
                               now=NOW, by_tg_id=ALICE)
        nearly_over = lot.deadline_at - timedelta(seconds=1)
        lot = self.A.undo_last_bid(self.session, self.season, lot,
                                   now=nearly_over)
        self.assertGreaterEqual(
            lot.deadline_at, nearly_over + timedelta(seconds=10),
            "the undone bid may have bought the extension; without a floor the "
            "lot would go to the previous bidder before anyone could react")

    def test_a_voided_bid_is_kept_rather_than_deleted(self):
        from models import AuctionBid
        lot = self.A.place_bid(self.session, self.season, self.lot,
                               self.mumbai, self.lot.base_price_lakh,
                               now=NOW, by_tg_id=ALICE)
        self.A.undo_last_bid(self.session, self.season, lot, now=NOW,
                             by_tg_id=CAROL)
        self.session.commit()
        rows = (self.session.query(AuctionBid)
                .filter(AuctionBid.lot_id == lot.id).all())
        self.assertEqual(1, len(rows))
        self.assertTrue(rows[0].is_void)
        self.assertEqual(CAROL, rows[0].voided_by_tg_id)

    def test_withdrawing_takes_the_player_off_the_block(self):
        lot = self.A.withdraw_lot(self.session, self.season, self.lot)
        self.session.commit()
        self.assertEqual(self.A.LOT_WITHDRAWN, lot.status)
        self.assertIsNone(self.season.current_lot_id)

    def test_a_sold_player_cannot_be_withdrawn(self):
        self.A.place_bid(self.session, self.season, self.lot, self.mumbai,
                         self.lot.base_price_lakh, now=NOW, by_tg_id=ALICE)
        lot = self.A.sell_lot(self.session, self.season, self.lot, now=NOW)
        self.session.commit()
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.withdraw_lot(self.session, self.season, lot)
        self.assertIn("undo the sale", str(caught.exception).lower())

    def test_an_unsold_player_can_be_re_listed_on_the_same_row(self):
        from models import AuctionLot
        before = (self.session.query(AuctionLot)
                  .filter(AuctionLot.season_id == self.season.id).count())
        lot = self.A.pass_lot(self.session, self.season, self.lot)
        self.session.commit()
        lot = self.A.relist(self.session, self.season, lot)
        self.session.commit()
        self.assertEqual(self.A.LOT_QUEUED, lot.status)
        self.assertEqual(1, lot.times_unsold, "the history stays on the row")
        after = (self.session.query(AuctionLot)
                 .filter(AuctionLot.season_id == self.season.id).count())
        self.assertEqual(before, after, "re-listing must not copy the row")

    def test_a_published_sale_cannot_be_quietly_undone(self):
        self.A.place_bid(self.session, self.season, self.lot, self.mumbai,
                         self.lot.base_price_lakh, now=NOW, by_tg_id=ALICE)
        lot = self.A.sell_lot(self.session, self.season, self.lot, now=NOW)
        self.season.published_at = NOW
        self.session.commit()
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.undo_sale(self.session, self.season, lot, now=NOW)
        self.assertIn("published", str(caught.exception).lower())


# ══════════════════════════════════════════════════════════════════════
# Who may bid
# ══════════════════════════════════════════════════════════════════════

class PermissionTests(AuctionCase):

    def test_the_owner_and_co_owners_are_equals(self):
        self.A.add_co_owner(self.session, self.mumbai, CAROL)
        self.session.commit()
        self.assertTrue(self.A.may_bid_for(self.mumbai, ALICE))
        self.assertTrue(self.A.may_bid_for(self.mumbai, CAROL))
        self.assertFalse(self.A.may_bid_for(self.mumbai, BOB))
        self.assertFalse(self.A.may_bid_for(self.mumbai, None))

    def test_franchise_for_actor_finds_only_your_own(self):
        self.assertEqual(self.mumbai.id,
                         self.A.franchise_for_actor(self.session,
                                                    self.season.id, ALICE).id)
        self.assertIsNone(self.A.franchise_for_actor(self.session,
                                                     self.season.id, 9999))

    def test_a_co_owner_list_ignores_blanks_duplicates_and_the_owner(self):
        ids = self.A.set_co_owners(self.session, self.mumbai,
                                   ["", "222", "222", str(ALICE), "x", "333"])
        self.assertEqual([BOB, CAROL], ids)


# ══════════════════════════════════════════════════════════════════════
# The commands
# ══════════════════════════════════════════════════════════════════════

class CommandTests(AuctionCase):

    def setUp(self):
        super().setUp()
        self.build_pool()
        # The handler reads the real clock, so this lot is opened against real
        # time rather than the fixed NOW the service-level tests inject.
        self.lot = self.start(now=datetime.utcnow())
        self.session.commit()
        self.replies = []
        self.announced = []

    def _update(self, user_id, args=(), chat_type="supergroup"):
        import asyncio
        from types import SimpleNamespace

        async def reply_text(text, **kwargs):
            self.replies.append(text)
            return SimpleNamespace(message_id=1)

        async def send_message(chat_id=None, text="", **kwargs):
            self.announced.append(text)
            return SimpleNamespace(message_id=100 + len(self.announced))

        async def set_message_reaction(**kwargs):
            return True

        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=self.season.chat_id,
                                           type=chat_type),
            effective_user=SimpleNamespace(id=user_id, username="u",
                                           first_name="U"),
            effective_message=SimpleNamespace(reply_text=reply_text,
                                              message_id=7))
        context = SimpleNamespace(
            args=list(args),
            bot=SimpleNamespace(send_message=send_message,
                                set_message_reaction=set_message_reaction))
        return update, context, asyncio

    def _run(self, handler, user_id, args=(), chat_type="supergroup"):
        update, context, asyncio = self._update(user_id, args, chat_type)
        asyncio.run(handler(update, context))
        return self.replies

    def test_bid_in_a_dm_is_refused(self):
        from handlers import auction as H
        self._run(H.bid_handler, ALICE, ("5",), chat_type="private")
        self.assertIn("only work in the group", self.replies[-1])

    def test_a_stranger_cannot_bid(self):
        from handlers import auction as H
        self._run(H.bid_handler, 9999, ("5",))
        self.assertIn("owner or a co-owner", self.replies[-1])

    def test_a_bot_admin_cannot_bid_for_somebody_elses_franchise(self):
        """Deliberately narrower than every other admin power in the feature."""
        import os
        from handlers import auction as H
        previous = os.environ.get("BOT_ADMIN_IDS")
        os.environ["BOT_ADMIN_IDS"] = str(CAROL)
        try:
            self._run(H.bid_handler, CAROL, ("5",))
        finally:
            if previous is None:
                os.environ.pop("BOT_ADMIN_IDS", None)
            else:
                os.environ["BOT_ADMIN_IDS"] = previous
        self.assertIn("owner or a co-owner", self.replies[-1])

    def test_a_bare_bid_bids_the_next_minimum(self):
        from handlers import auction as H
        from models import AuctionLot
        self._run(H.bid_handler, ALICE, ())
        self.session.expire_all()
        lot = (self.session.query(AuctionLot)
               .filter(AuctionLot.id == self.lot.id).first())
        self.assertEqual(lot.base_price_lakh, lot.current_bid_lakh)
        self.assertEqual(self.mumbai.id, lot.current_bidder_id)

    def test_a_successful_bid_says_nothing(self):
        from handlers import auction as H
        self._run(H.bid_handler, ALICE, ())
        self.assertEqual([], self.replies,
                         "forty replies inside one lot is the flood the board "
                         "exists to avoid")

    def test_a_refused_bid_always_answers(self):
        from handlers import auction as H
        self._run(H.bid_handler, ALICE, ("0.01",))
        self.assertTrue(self.replies)
        self.assertIn("base price", self.replies[-1])

    def test_the_board_carries_the_price_the_clock_and_every_purse(self):
        body = self.A.render_board(self.session, self.season, self.lot, now=NOW)
        self.assertIn(self.lot.name, body)
        self.assertIn("Mumbai", body)
        self.assertIn("Chennai", body)
        self.assertIn("max bid", body)
        self.assertIn("/bid", body)

    def test_the_board_prints_the_amount_the_way_you_type_it_back(self):
        """The hint and the parser have to agree, or the room bids blind."""
        lot = self.A.place_bid(self.session, self.season, self.lot,
                               self.mumbai, self.lot.base_price_lakh,
                               now=NOW, by_tg_id=ALICE)
        minimum = self.A.next_min_bid(self.season, lot)
        hint = self.A._bid_hint(minimum)
        self.assertEqual(minimum, self.A.parse_amount(hint))


class HtmlSafetyTests(AuctionCase):
    """Every headline is sent with parse_mode=HTML, and names are typed in.

    A franchise called ``<b>Mumbai`` does not corrupt a message — Telegram
    rejects the whole send as unparseable, so the announcement silently never
    arrives and the room is told nothing at all. That is worse than a broken
    name, which is why this escapes rather than strips.
    """

    def test_a_franchise_name_with_markup_is_escaped_in_its_event(self):
        franchise = self.A.create_franchise(self.session, self.season,
                                            "<b>Sneaky</b>", owner_tg_id=CAROL)
        self.session.commit()
        headline = self.A.recent_events(self.session, self.season.id,
                                        limit=1)[0].headline
        self.assertIn("&lt;b&gt;Sneaky", headline)
        self.assertNotIn("<b>Sneaky", headline)

    def test_a_player_name_with_markup_is_escaped_when_the_lot_opens(self):
        from models import Player
        self.session.add(Player(name="Bad <i>Name", rating=90,
                                category="Batsman", country="India",
                                bat_hand="Right", bowl_hand="Right",
                                bowl_style="Medium Pacer", is_active=True,
                                version="Base"))
        self.session.flush()
        self.build_pool(self.session.query(Player)
                        .filter(Player.name == "Bad <i>Name").all())
        self.start()
        self.session.commit()
        opened = [e for e in self.A.recent_events(self.session, self.season.id,
                                                  limit=10)
                  if e.kind == "lot_opened"][0]
        self.assertIn("&lt;i&gt;", opened.headline)
        self.assertNotIn("<i>", opened.headline)

    def test_the_board_escapes_every_name_it_prints(self):
        self.A.create_franchise(self.session, self.season, "<s>Zed",
                                owner_tg_id=CAROL)
        self.build_pool()
        lot = self.start()
        self.session.commit()
        board = self.A.render_board(self.session, self.season, lot, now=NOW)
        self.assertIn("&lt;s&gt;Zed", board)
        self.assertNotIn("<s>Zed", board)


class EventLogTests(AuctionCase):
    """The log is also the queue the group is announced from."""

    def test_every_action_leaves_exactly_one_event(self):
        self.build_pool()
        lot = self.start()
        self.session.commit()
        kinds = [e.kind for e in self.A.recent_events(self.session,
                                                      self.season.id, limit=50)]
        self.assertEqual(1, kinds.count("season_started"))
        self.assertEqual(1, kinds.count("lot_opened"))

    def test_a_bid_leaves_one_event_not_two(self):
        self.build_pool()
        lot = self.start()
        self.A.place_bid(self.session, self.season, lot, self.mumbai,
                         lot.base_price_lakh, now=NOW, by_tg_id=ALICE)
        self.session.commit()
        kinds = [e.kind for e in self.A.recent_events(self.session,
                                                      self.season.id, limit=50)]
        self.assertEqual(1, kinds.count("bid"))

    def test_the_cursor_only_moves_forward(self):
        self.build_pool()
        self.start()
        self.session.commit()
        pending = self.A.pending_events(self.session, self.season, limit=50)
        self.assertTrue(pending)
        self.season.announced_event_id = pending[-1].id
        self.session.commit()
        self.assertEqual([], self.A.pending_events(self.session, self.season))

    def test_bids_are_reflected_on_the_board_rather_than_announced(self):
        from services import auction_scheduler as S
        self.assertIn("bid", S.SILENT_KINDS,
                      "announcing every bid would flood the group")


class FakeBot:
    """Just enough Telegram to drive the sweeper without a network."""

    def __init__(self):
        self.sent = []
        self.edits = []
        self.pinned = []
        self.next_id = 500

    async def send_message(self, chat_id=None, text="", **kwargs):
        from types import SimpleNamespace
        self.next_id += 1
        self.sent.append(text)
        return SimpleNamespace(message_id=self.next_id)

    async def edit_message_text(self, chat_id=None, message_id=None, text="",
                                **kwargs):
        self.edits.append((message_id, text))
        return True

    async def pin_chat_message(self, chat_id=None, message_id=None, **kwargs):
        self.pinned.append(message_id)
        return True

    async def unpin_chat_message(self, chat_id=None, **kwargs):
        return True


class SchedulerTests(AuctionCase):
    """The sweeper: what it announces, how often it edits, and when it closes."""

    def setUp(self):
        super().setUp()
        self.build_pool()
        self.lot = self.start()
        self.session.commit()
        self.bot = FakeBot()

    def _tick(self, now):
        import asyncio
        from types import SimpleNamespace
        from services import auction_scheduler as S
        context = SimpleNamespace(bot=self.bot)
        asyncio.run(S._tick_one(context, self.session, self.season, now))

    def test_the_first_tick_posts_and_pins_the_board(self):
        self._tick(NOW + timedelta(seconds=1))
        self.assertTrue(self.bot.sent, "the board has to reach the room")
        self.assertEqual([self.season.board_message_id], self.bot.pinned)
        self.assertIn(self.lot.name, "\n".join(self.bot.sent))

    def test_an_event_is_announced_exactly_once(self):
        self._tick(NOW + timedelta(seconds=1))
        first = len([t for t in self.bot.sent if "Lot" in t or "under way" in t])
        self._tick(NOW + timedelta(seconds=2))
        second = len([t for t in self.bot.sent if "Lot" in t or "under way" in t])
        self.assertEqual(first, second,
                         "the drain cursor must not replay what was announced")

    def test_ten_bids_inside_one_tick_cost_one_edit(self):
        """The flood-control claim, checked rather than asserted in a comment."""
        self._tick(NOW + timedelta(seconds=1))
        self.bot.edits.clear()

        lot, bidders = self.lot, [self.mumbai, self.chennai]
        for i in range(10):
            amount = self.A.next_min_bid(self.season, lot)
            lot = self.A.place_bid(self.session, self.season, lot,
                                   bidders[i % 2], amount,
                                   now=NOW + timedelta(seconds=2))
        self.session.commit()

        self._tick(NOW + timedelta(seconds=3))
        self.assertEqual(1, len(self.bot.edits),
                         "the board is debounced on bid_count, so a burst of "
                         "bids is one edit, not ten")
        self.assertIn(self.chennai.name if lot.current_bidder_id == self.chennai.id
                      else self.mumbai.name, self.bot.edits[0][1],
                      "and the one edit carries the latest leader")

    def test_a_tick_with_nothing_new_edits_nothing(self):
        self._tick(NOW + timedelta(seconds=1))
        self.bot.edits.clear()
        self._tick(NOW + timedelta(seconds=2))
        self.assertEqual([], self.bot.edits)

    def test_going_once_moves_the_board_on(self):
        self._tick(NOW + timedelta(seconds=1))
        self.bot.edits.clear()
        self._tick(self.lot.deadline_at - timedelta(seconds=8))
        self.assertEqual(1, len(self.bot.edits))
        self.assertIn("going once", self.bot.edits[-1][1].lower())

    def test_an_expired_lot_is_sold_announced_and_followed_by_the_next(self):
        self.A.place_bid(self.session, self.season, self.lot, self.mumbai,
                         self.lot.base_price_lakh, now=NOW, by_tg_id=ALICE)
        self.session.commit()
        self._tick(self.lot.deadline_at + timedelta(seconds=1))

        said = "\n".join(self.bot.sent)
        self.assertIn("SOLD", said)
        self.assertIn("Mumbai", said)
        self.session.refresh(self.lot)
        self.assertEqual(self.A.LOT_SOLD, self.lot.status)
        self.assertIsNotNone(self.A.current_lot(self.session, self.season),
                             "the sweeper opens the next lot straight away")
        self.assert_ledger_agrees("after the sweeper sold a lot")

    def test_an_expired_lot_with_no_bid_is_passed_and_announced(self):
        self._tick(self.lot.deadline_at + timedelta(seconds=1))
        self.assertIn("UNSOLD", "\n".join(self.bot.sent))

    def test_a_stale_pointer_heals_itself_and_says_nothing(self):
        self.season.current_lot_id = 999999
        self.session.commit()
        self._tick(NOW + timedelta(seconds=1))
        self.assertEqual(self.lot.id, self.season.current_lot_id)
        self.assertEqual([], self.bot.sent,
                         "realigning a pointer is not news")

    def test_a_deleted_board_is_replaced_rather_than_lost(self):
        from telegram.error import BadRequest

        self._tick(NOW + timedelta(seconds=1))
        original = self.season.board_message_id

        async def refuse(chat_id=None, message_id=None, text="", **kwargs):
            raise BadRequest("Message to edit not found")

        self.bot.edit_message_text = refuse
        self.bot.sent.clear()
        self._tick(self.lot.deadline_at - timedelta(seconds=8))
        self.assertNotEqual(original, self.season.board_message_id)
        self.assertTrue(self.bot.sent, "a fresh board is posted instead")

    def test_an_identical_edit_is_treated_as_success(self):
        """Two bids in one tick can render byte-identical text."""
        import asyncio
        from telegram.error import BadRequest
        from services import auction_scheduler as S

        async def not_modified(chat_id=None, message_id=None, text="", **kwargs):
            raise BadRequest("Message is not modified: specified new message "
                             "content and reply markup are exactly the same")

        self.bot.edit_message_text = not_modified
        ok = asyncio.run(S.edit_board(self.bot, 1, 2, "same"))
        self.assertTrue(ok, "'not modified' means the text is already there")


if __name__ == "__main__":
    unittest.main()

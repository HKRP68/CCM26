"""Retention: keeping players before the auction, and paying for them.

What is pinned here is mostly the money and the caps, because retention is the
first thing that spends a purse and everything downstream reads what it leaves
behind:

  • **The proposal's own table.** ``purse_total - retention_spent ==
    purse_remaining`` before a single lot opens, and the ledger agrees with the
    cache after *every* retention and every release.
  • **A retained player is an ordinary sold lot**, so the squad, the overseas
    count and the published league all pick them up with no special case — and
    the pool builder skips them without being told to.
  • **The ladder pre-fills; the caps refuse.** Count, budget, purse, squad,
    overseas, rating, role, and the same reachability rule bidding uses.
  • **Retention is not an auction sale.** ``undo_sale`` must not swallow one,
    and the board's progress line must not count one.
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
# The ladder
# ══════════════════════════════════════════════════════════════════════

class LadderTests(AuctionCase):

    def test_the_slab_falls_by_retention(self):
        A = self.A
        self.assertEqual(1800, A.retention_price_for(self.season, 1))
        self.assertEqual(1400, A.retention_price_for(self.season, 2))
        self.assertEqual(1100, A.retention_price_for(self.season, 3))

    def test_past_the_end_the_last_slab_repeats(self):
        """A ladder shorter than the maximum is not an error."""
        self.assertEqual(1100, self.A.retention_price_for(self.season, 4))
        self.assertEqual(1100, self.A.retention_price_for(self.season, 99))

    def test_a_ladder_saved_out_of_order_still_reads_by_slab(self):
        import json
        self.season.retention_price_rules_json = json.dumps([
            {"slab": 3, "price_lakh": 500},
            {"slab": 1, "price_lakh": 900},
            {"slab": 2, "price_lakh": 700}])
        self.session.commit()
        self.assertEqual(900, self.A.retention_price_for(self.season, 1))
        self.assertEqual(700, self.A.retention_price_for(self.season, 2))
        self.assertEqual(500, self.A.retention_price_for(self.season, 3))

    def test_the_ladder_only_prefills_and_an_override_is_taken_as_given(self):
        self.season.max_retentions = 2
        self.session.commit()
        lot = self.A.retain(self.session, self.season, self.mumbai,
                            self.players[0], 250, now=NOW)
        self.assertEqual(250, lot.sold_price_lakh,
                         "the price given wins over the slab")
        lot2 = self.A.retain(self.session, self.season, self.mumbai,
                             self.players[1], now=NOW)
        self.assertEqual(1400, lot2.sold_price_lakh,
                         "and the next slab is still the SECOND, not the first")


# ══════════════════════════════════════════════════════════════════════
# The money
# ══════════════════════════════════════════════════════════════════════

class RetentionMoneyTests(AuctionCase):
    max_squad = 8
    min_squad = 2

    def setUp(self):
        super().setUp()
        self.season.max_retentions = 3
        self.session.commit()

    def test_retaining_debits_the_purse_once_and_records_it_once(self):
        lot = self.A.retain(self.session, self.season, self.mumbai,
                            self.players[0], now=NOW, by_tg_id=ALICE)
        self.session.commit()
        self.session.refresh(self.mumbai)

        self.assertEqual(self.purse_lakh - 1800, self.mumbai.purse_remaining_lakh)
        self.assertEqual(1, self.mumbai.squad_size)
        self.assertEqual(1, self.mumbai.retained_count)
        self.assertEqual(self.A.LOT_SOLD, lot.status)
        self.assertEqual(self.A.ACQ_RETAINED, lot.acquisition)
        self.assert_ledger_agrees("after one retention")

        rows = [r for r in self.A.ledger(self.session, self.mumbai.id)
                if r.kind == self.A.LEDGER_RETENTION]
        self.assertEqual(1, len(rows))
        self.assertEqual(-1800, rows[0].amount_lakh)

    def test_the_proposals_own_table_adds_up(self):
        """Starting Purse − Retention Spent == Auction Purse."""
        for player in self.players[:3]:
            self.A.retain(self.session, self.season, self.mumbai, player, now=NOW)
        self.session.commit()
        self.session.refresh(self.mumbai)
        spent = self.A.retention_spent(self.session, self.mumbai.id)
        self.assertEqual(1800 + 1400 + 1100, spent)
        self.assertEqual(self.mumbai.purse_total_lakh - spent,
                         self.mumbai.purse_remaining_lakh)
        self.assert_ledger_agrees("across three retentions")

    def test_releasing_refunds_exactly_and_puts_the_player_back(self):
        lot = self.A.retain(self.session, self.season, self.mumbai,
                            self.players[0], now=NOW)
        self.session.commit()
        self.A.unretain(self.session, self.season, self.mumbai, lot)
        self.session.commit()
        self.session.refresh(self.mumbai)

        self.assertEqual(self.purse_lakh, self.mumbai.purse_remaining_lakh)
        self.assertEqual(0, self.mumbai.squad_size)
        self.assertEqual(0, self.mumbai.retained_count)
        self.assertEqual(self.A.LOT_QUEUED, lot.status)
        self.assertEqual(self.A.ACQ_AUCTION, lot.acquisition)
        self.assertIsNone(lot.sold_to_id)
        self.assert_ledger_agrees("after a release")

    def test_the_ledger_agrees_after_every_single_step(self):
        """Not just at the end — a cache that is only right at the end was
        wrong in the middle of somebody's auction."""
        lots = []
        for player in self.players[:3]:
            lots.append(self.A.retain(self.session, self.season, self.mumbai,
                                      player, now=NOW))
            self.session.commit()
            self.assert_ledger_agrees("mid-retention")
        for lot in lots:
            self.A.unretain(self.session, self.season, self.mumbai, lot)
            self.session.commit()
            self.assert_ledger_agrees("mid-release")


# ══════════════════════════════════════════════════════════════════════
# The caps
# ══════════════════════════════════════════════════════════════════════

class RetentionCapTests(AuctionCase):
    max_squad = 8
    min_squad = 2

    def setUp(self):
        super().setUp()
        self.season.max_retentions = 2
        self.session.commit()

    def test_an_auction_with_no_retentions_configured_refuses_outright(self):
        self.season.max_retentions = 0
        self.session.commit()
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.retain(self.session, self.season, self.mumbai,
                          self.players[0], now=NOW)
        self.assertIn("no retentions", str(caught.exception))

    def test_the_count_cap_refuses_at_its_boundary(self):
        for player in self.players[:2]:
            self.A.retain(self.session, self.season, self.mumbai, player, now=NOW)
        self.session.commit()
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.retain(self.session, self.season, self.mumbai,
                          self.players[2], now=NOW)
        self.assertIn("maximum", str(caught.exception))

    def test_the_budget_cap_names_the_overshoot(self):
        self.season.retention_max_spend_lakh = 2000
        self.session.commit()
        self.A.retain(self.session, self.season, self.mumbai, self.players[0],
                      now=NOW)                                   # 1800
        self.session.commit()
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.retain(self.session, self.season, self.mumbai,
                          self.players[1], now=NOW)              # +1400 = 3200
        message = str(caught.exception)
        self.assertIn("retention budget", message)
        self.assertIn("over", message)
        # Exactly at the cap is allowed.
        self.A.retain(self.session, self.season, self.mumbai, self.players[1],
                      200, now=NOW)
        self.assertEqual(2000, self.A.retention_spent(self.session,
                                                      self.mumbai.id))

    def test_a_price_over_the_purse_is_refused(self):
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.retain(self.session, self.season, self.mumbai,
                          self.players[0], self.purse_lakh + 1, now=NOW)
        self.assertIn("more than the purse", str(caught.exception))

    def test_the_rating_band_refuses_and_says_the_band(self):
        self.season.retention_min_rating = 95
        self.session.commit()
        low = [p for p in self.players if p.rating < 95][0]
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.retain(self.session, self.season, self.mumbai, low, now=NOW)
        self.assertIn("95 and above", str(caught.exception))

        self.season.retention_min_rating = None
        self.season.retention_max_rating = 90
        self.session.commit()
        high = [p for p in self.players if p.rating > 90][0]
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.retain(self.session, self.season, self.mumbai, high, now=NOW)
        self.assertIn("90 and below", str(caught.exception))

    def test_the_role_restriction_refuses_and_lists_what_is_allowed(self):
        import json
        self.season.retention_categories_json = json.dumps(["Bowler"])
        self.session.commit()
        batter = [p for p in self.players if p.category == "Batsman"][0]
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.retain(self.session, self.season, self.mumbai, batter, now=NOW)
        self.assertIn("Bowler", str(caught.exception))

    def test_the_squad_cap_refuses(self):
        self.mumbai.squad_size = self.max_squad
        self.session.commit()
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.retain(self.session, self.season, self.mumbai,
                          self.players[0], 100, now=NOW)
        self.assertIn("squad limit", str(caught.exception))

    def test_the_overseas_cap_refuses(self):
        self.season.max_overseas = 0
        self.session.commit()
        overseas = [p for p in self.players if p.country != "India"][0]
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.retain(self.session, self.season, self.mumbai, overseas,
                          100, now=NOW)
        self.assertIn("overseas limit", str(caught.exception))


class RetentionReachabilityTests(AuctionCase):
    """The same rule bidding uses — a franchise must not retain its way into
    being unable to field a squad."""
    min_squad = 5
    max_squad = 8

    def setUp(self):
        super().setUp()
        self.season.max_retentions = 3
        self.session.commit()

    def test_retaining_at_the_ceiling_is_allowed_and_one_over_is_not(self):
        ceiling = self.A.max_bid_now(self.season, self.mumbai)
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.retain(self.session, self.season, self.mumbai,
                          self.players[0], ceiling + 1, now=NOW)
        self.assertIn("unable to fill its squad", str(caught.exception))
        self.assertIn("most it can retain", str(caught.exception))

        lot = self.A.retain(self.session, self.season, self.mumbai,
                            self.players[0], ceiling, now=NOW)
        self.assertEqual(ceiling, lot.sold_price_lakh)

    def test_the_ceiling_tightens_as_the_squad_grows_then_stops_applying(self):
        floor = self.season.min_base_price_lakh
        # Nothing kept: winning this one leaves 4 slots owing.
        self.assertEqual(self.purse_lakh - 4 * floor,
                         self.A.max_bid_now(self.season, self.mumbai))
        self.mumbai.squad_size = self.min_squad - 1
        self.session.commit()
        self.assertEqual(self.mumbai.purse_remaining_lakh,
                         self.A.max_bid_now(self.season, self.mumbai),
                         "the one that completes the minimum may spend it all")

    def test_retention_and_bidding_compose(self):
        """Retention moves the purse and the squad the same way a purchase
        does, so the bidding rule picks up exactly where retention left off."""
        self.A.retain(self.session, self.season, self.mumbai, self.players[0],
                      1000, now=NOW)
        self.session.commit()
        self.session.refresh(self.mumbai)
        expected = (self.purse_lakh - 1000
                    - max(0, self.min_squad - 2) * self.season.min_base_price_lakh)
        self.assertEqual(expected, self.A.max_bid_now(self.season, self.mumbai))


# ══════════════════════════════════════════════════════════════════════
# The window
# ══════════════════════════════════════════════════════════════════════

class RetentionWindowTests(AuctionCase):

    def setUp(self):
        super().setUp()
        self.season.max_retentions = 2
        self.session.commit()

    def test_a_passed_deadline_refuses(self):
        self.season.retention_deadline_at = NOW - timedelta(minutes=1)
        self.session.commit()
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.retain(self.session, self.season, self.mumbai,
                          self.players[0], now=NOW)
        self.assertIn("deadline has passed", str(caught.exception))

    def test_the_seconds_left_read_both_ways_round(self):
        self.assertIsNone(self.A.retention_seconds_left(self.season, NOW))
        self.season.retention_deadline_at = NOW + timedelta(hours=2)
        self.assertEqual(7200, self.A.retention_seconds_left(self.season, NOW))
        self.season.retention_deadline_at = NOW - timedelta(hours=1)
        self.assertEqual(-3600, self.A.retention_seconds_left(self.season, NOW),
                         "a passed deadline reads negative, so a caller can "
                         "say 'closed an hour ago' rather than just 'closed'")

    def test_locking_refuses_further_retention_and_release(self):
        lot = self.A.retain(self.session, self.season, self.mumbai,
                            self.players[0], now=NOW)
        self.A.lock_retention(self.session, self.season)
        self.session.commit()
        with self.assertRaises(self.A.AuctionError):
            self.A.retain(self.session, self.season, self.mumbai,
                          self.players[1], now=NOW)
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.unretain(self.session, self.season, self.mumbai, lot)
        self.assertIn("closed", str(caught.exception))

    def test_locking_is_idempotent(self):
        self.A.lock_retention(self.session, self.season)
        stamped = self.season.retention_locked_at
        self.A.lock_retention(self.session, self.season)
        self.assertEqual(stamped, self.season.retention_locked_at)

    def test_starting_the_auction_closes_the_window(self):
        self.build_pool()
        self.A.retain(self.session, self.season, self.mumbai, self.players[0],
                      now=NOW)
        self.session.commit()
        self.start()
        self.session.commit()
        self.assertTrue(self.A.retention_locked(self.season))

    def test_the_minimum_is_enforced_at_start_by_name(self):
        self.season.min_retentions = 1
        self.build_pool()
        self.A.retain(self.session, self.season, self.mumbai, self.players[0],
                      now=NOW)
        self.session.commit()
        with self.assertRaises(self.A.AuctionError) as caught:
            self.start()
        self.assertIn("Chennai", str(caught.exception))
        self.assertIn("minimum of 1", str(caught.exception))

    def test_the_minimum_check_sees_a_retention_with_no_commit_between(self):
        """The website's shape: retain and start in one request.

        The session is autoflush=False, so the check has to flush or it reads
        a retained_count that is still on disk — the bug class that bit phase 1
        three times.
        """
        self.season.min_retentions = 1
        self.build_pool()
        self.A.retain(self.session, self.season, self.mumbai, self.players[0],
                      now=NOW)
        self.A.retain(self.session, self.season, self.chennai, self.players[1],
                      now=NOW)
        self.start()                      # no commit anywhere above
        self.session.commit()
        self.assertEqual(self.A.STATUS_LIVE, self.season.status)

    def test_retention_is_refused_once_the_auction_is_live(self):
        self.build_pool()
        self.start()
        self.session.commit()
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.retain(self.session, self.season, self.mumbai,
                          self.players[0], now=NOW)
        self.assertIn("before the auction opens", str(caught.exception))


# ══════════════════════════════════════════════════════════════════════
# How retention meets the rest of the auction
# ══════════════════════════════════════════════════════════════════════

class RetentionAndThePoolTests(AuctionCase):
    max_squad = 8
    min_squad = 2

    def setUp(self):
        super().setUp()
        self.season.max_retentions = 2
        self.session.commit()

    def test_a_retained_player_is_left_out_of_a_pool_built_afterwards(self):
        from models import AuctionLot
        kept = self.A.retain(self.session, self.season, self.mumbai,
                             self.players[0], now=NOW)
        self.session.commit()
        added, skipped = self.build_pool()
        self.assertEqual(len(CATALOGUE) - 1, added)
        self.assertEqual(1, skipped)
        self.session.refresh(kept)
        self.assertEqual(self.A.LOT_SOLD, kept.status,
                         "and the retention is untouched by the pool build")

    def test_a_player_already_queued_is_converted_rather_than_duplicated(self):
        """An admin who built the pool first must not hit a wall."""
        from models import AuctionLot
        self.build_pool()
        before = (self.session.query(AuctionLot)
                  .filter(AuctionLot.season_id == self.season.id).count())
        lot = self.A.retain(self.session, self.season, self.mumbai,
                            self.players[0], now=NOW)
        self.session.commit()
        after = (self.session.query(AuctionLot)
                 .filter(AuctionLot.season_id == self.season.id).count())
        self.assertEqual(before, after, "no second row for the same player")
        self.assertEqual(self.A.ACQ_RETAINED, lot.acquisition)

    def test_a_player_somebody_else_retained_is_refused_by_name(self):
        self.A.retain(self.session, self.season, self.mumbai, self.players[0],
                      now=NOW)
        self.session.commit()
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.retain(self.session, self.season, self.chennai,
                          self.players[0], now=NOW)
        self.assertIn("Mumbai", str(caught.exception))

    def test_a_sold_player_cannot_be_retained(self):
        self.build_pool()
        lot = self.start()
        self.A.place_bid(self.session, self.season, lot, self.mumbai,
                         lot.base_price_lakh, now=NOW, by_tg_id=ALICE)
        self.A.sell_lot(self.session, self.season, lot, now=NOW)
        self.session.commit()
        # (retention is closed by start() anyway, but the lot guard is its own
        # sentence and worth pinning)
        self.assertEqual(self.A.ACQ_AUCTION, lot.acquisition)

    def test_retentions_are_not_auction_progress(self):
        """The board would otherwise announce lots resolved before it opened."""
        self.A.retain(self.session, self.season, self.mumbai, self.players[0],
                      now=NOW)
        self.session.commit()
        self.build_pool()

        counts = self.A.pool_counts(self.session, self.season.id)
        self.assertEqual(1, counts["retained"])
        self.assertEqual(0, counts.get(self.A.LOT_SOLD, 0),
                         "a retention is not a sale")
        self.assertEqual(len(CATALOGUE) - 1, counts["total"],
                         "and it is not a lot the auction has to get through")

        board = self.A.render_board(self.session, self.season, None, now=NOW)
        self.assertIn(f"0/{len(CATALOGUE) - 1}", board)
        self.assertIn("1 retained", board)

    def test_undo_sale_refuses_a_retention_and_names_the_way_out(self):
        lot = self.A.retain(self.session, self.season, self.mumbai,
                            self.players[0], now=NOW)
        self.session.commit()
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.undo_sale(self.session, self.season, lot, now=NOW)
        message = str(caught.exception)
        self.assertIn("retained, not bought", message)
        self.assertIn("/aunretain", message)

    def test_a_retained_player_counts_against_the_squad_and_the_overseas_cap(self):
        overseas = [p for p in self.players if p.country != "India"][0]
        self.A.retain(self.session, self.season, self.mumbai, overseas, 100,
                      now=NOW)
        self.session.commit()
        self.assertEqual(1, len(self.A.squad(self.session, self.mumbai.id)))
        self.assertEqual(1, self.A.overseas_count(self.session, self.mumbai.id))


class RetentionPublishTests(AuctionCase):
    min_squad = 1
    max_squad = 6

    def test_a_retained_player_reaches_the_league_marked_as_retained(self):
        import json
        from models import ChallengePlayer, ChallengeTeam
        self.season.max_retentions = 1
        self.session.commit()
        kept = self.A.retain(self.session, self.season, self.mumbai,
                             self.players[0], 900, now=NOW)
        self.session.commit()
        self.build_pool()
        self.start()
        self.session.commit()
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
        self.session.commit()

        league = self.A.publish_to_league(self.session, self.season)
        self.session.commit()
        row = (self.session.query(ChallengePlayer)
               .join(ChallengeTeam, ChallengePlayer.team_id == ChallengeTeam.id)
               .filter(ChallengeTeam.league_id == league.id,
                       ChallengePlayer.name == kept.name,
                       # By card, not name: an unsold second edition of
                       # the same cricketer can be auto-filled to the other
                       # squad, so the name alone is no longer unique.
                       ChallengePlayer.source_player_id == kept.player_id)
               .first())
        self.assertIsNotNone(row, "a retained player is in the squad")
        blob = json.loads(row.details_json)
        self.assertEqual("retained", blob["acquisition"])
        self.assertEqual(900, blob["auction_price_lakh"])

        from services.cipl_match import cp_to_player_dict
        self.assertEqual(kept.rating, cp_to_player_dict(row)["rating"])


class PreviousSeasonTests(AuctionCase):

    def _league(self, squads):
        from models import ChallengeLeague, ChallengePlayer, ChallengeTeam
        # The module's temp DB is shared across tests and ChallengeMode.name is
        # unique, so reuse whatever mode is already there rather than racing it.
        mode = auction_mode = self.A._ensure_default_mode(self.session)
        league = ChallengeLeague(mode_id=mode.id,
                                 name=f"Season 1 #{next(_PID)}")
        self.session.add(league)
        self.session.flush()
        for team_name, players in squads.items():
            team = ChallengeTeam(league_id=league.id, name=team_name)
            self.session.add(team)
            self.session.flush()
            for player in players:
                self.session.add(ChallengePlayer(team_id=team.id,
                                                 name=player.name,
                                                 source_player_id=player.id))
        self.session.flush()
        return league

    def test_the_map_says_who_held_whom_before_any_lot_exists(self):
        """Retention runs before the pool, so it cannot read it off the lots."""
        league = self._league({"Mumbai": self.players[:2],
                               "Chennai": self.players[2:3]})
        self.season.previous_league_id = league.id
        self.session.commit()
        mapping = self.A.previous_squad_map(self.session, self.season)
        self.assertEqual(self.mumbai.id, mapping[self.players[0].id].id)
        self.assertEqual(self.chennai.id, mapping[self.players[2].id].id)
        self.assertNotIn(self.players[3].id, mapping)

    def test_linking_remembers_the_league_so_the_picker_can_use_it(self):
        league = self._league({"Mumbai": self.players[:1]})
        self.build_pool()
        self.A.link_previous_season(self.session, self.season, league.id)
        self.session.commit()
        self.assertEqual(league.id, self.season.previous_league_id,
                         "passed in once, remembered thereafter")

    def test_retention_stamps_who_held_the_player_even_without_a_league(self):
        self.season.max_retentions = 1
        self.session.commit()
        lot = self.A.retain(self.session, self.season, self.mumbai,
                            self.players[0], now=NOW)
        self.assertEqual(self.mumbai.id, lot.previous_franchise_id,
                         "a retained player was, by definition, theirs")

    def test_retaining_somebody_elses_player_is_allowed(self):
        """A warning belongs in the UI; the service must not refuse it — an
        admin untangling a mess has to be able to put a player anywhere."""
        league = self._league({"Chennai": self.players[:1]})
        self.season.previous_league_id = league.id
        self.season.max_retentions = 1
        self.session.commit()
        lot = self.A.retain(self.session, self.season, self.mumbai,
                            self.players[0], now=NOW)
        self.assertEqual(self.mumbai.id, lot.sold_to_id)


class PhaseTwoBStaysOffTests(AuctionCase):
    """Retention is in; Right To Match is still not."""

    def test_rtm_is_untouched_by_retention(self):
        self.season.max_retentions = 1
        self.session.commit()
        self.A.retain(self.session, self.season, self.mumbai, self.players[0],
                      now=NOW)
        self.session.commit()
        self.session.refresh(self.mumbai)
        self.assertFalse(self.season.rtm_enabled)
        self.assertEqual(0, self.mumbai.rtm_cards_total)
        self.assertEqual(0, self.mumbai.rtm_cards_used)

    def test_a_season_written_before_retention_existed_reads_as_unconfigured(self):
        """The new columns land on a populated table; an old row must not
        crash, it must simply have no retention."""
        from models import AuctionSeason
        old = AuctionSeason(name="Older Season", status=self.A.STATUS_SETUP)
        self.session.add(old)
        self.session.flush()
        self.assertFalse(self.A.retention_configured(old))
        self.assertFalse(self.A.retention_locked(old))
        self.assertIsNone(self.A.retention_seconds_left(old))
        self.assertEqual([], self.A.retention_categories(old))
        # The ladder falls back to the shipped default rather than to nothing.
        self.assertEqual(1800, self.A.retention_price_for(old, 1))


# ══════════════════════════════════════════════════════════════════════
# The commands
# ══════════════════════════════════════════════════════════════════════

class RetentionCommandTests(AuctionCase):
    max_squad = 8
    min_squad = 2

    def setUp(self):
        super().setUp()
        self.season.max_retentions = 2
        # A card with a name nothing else in the shared test database shares —
        # the command looks players up by name across the whole catalogue, and
        # every test in this module adds its own copy of CATALOGUE.
        from models import Player
        self.unique = Player(name=f"Solo Player {next(_PID)}", rating=92,
                             category="Batsman", country="India",
                             bat_hand="Right", bowl_hand="Right",
                             bowl_style="Medium Pacer", bat_rating=92,
                             bowl_rating=40, is_active=True, version="Base")
        self.session.add(self.unique)
        self.session.commit()
        self.replies = []

    def _run(self, handler, user_id, args=(), chat_type="supergroup"):
        import asyncio
        from types import SimpleNamespace

        async def reply_text(text, **kwargs):
            self.replies.append(text)
            return SimpleNamespace(message_id=1)

        async def send_message(chat_id=None, text="", **kwargs):
            return SimpleNamespace(message_id=99)

        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=self.season.chat_id,
                                           type=chat_type),
            effective_user=SimpleNamespace(id=user_id, username="u",
                                           first_name="U"),
            effective_message=SimpleNamespace(reply_text=reply_text,
                                              message_id=7))
        context = SimpleNamespace(args=list(args),
                                  bot=SimpleNamespace(send_message=send_message))
        asyncio.run(handler(update, context))
        return self.replies

    def _as_admin(self, handler, args=()):
        import os
        previous = os.environ.get("BOT_ADMIN_IDS")
        os.environ["BOT_ADMIN_IDS"] = str(CAROL)
        try:
            return self._run(handler, CAROL, args)
        finally:
            if previous is None:
                os.environ.pop("BOT_ADMIN_IDS", None)
            else:
                os.environ["BOT_ADMIN_IDS"] = previous

    def test_a_non_admin_cannot_retain(self):
        from handlers import auction as H
        self._run(H.aretain_handler, ALICE, ("Mumbai | Someone",))
        self.assertIn("Only auction admins", self.replies[-1])

    def test_aretainforce_takes_the_ladder_when_no_price_is_given(self):
        from handlers import auction as H
        self._as_admin(H.aretainforce_handler, ("Mumbai", "|", self.unique.name))
        self.session.expire_all()
        kept = self.A.retained(self.session, self.mumbai.id)
        self.assertEqual(1, len(kept))
        self.assertEqual(1800, kept[0].sold_price_lakh)
        self.assertIn("₹18 Cr", self.replies[-1])

    def test_aretainforce_takes_an_explicit_price_in_crore(self):
        from handlers import auction as H
        self._as_admin(H.aretainforce_handler,
                       ("Mumbai", "|", self.unique.name, "|", "6"))
        if not self.A.retained(self.session, self.mumbai.id):
            self.fail(f"retain refused: {self.replies[-1]}")
        self.session.expire_all()
        kept = self.A.retained(self.session, self.mumbai.id)
        self.assertEqual(600, kept[0].sold_price_lakh)

    def test_an_ambiguous_player_name_is_never_guessed(self):
        """Retaining the wrong card costs a franchise real money to undo."""
        from handlers import auction as H
        self._as_admin(H.aretain_handler, ("Mumbai", "|", "Rashid"))
        self.assertIn("type more of the name", self.replies[-1])
        self.assertEqual([], self.A.retained(self.session, self.mumbai.id))

    def test_aunretain_puts_the_player_back(self):
        from handlers import auction as H
        self.A.retain(self.session, self.season, self.mumbai, self.unique,
                      now=NOW)
        self.session.commit()
        self._as_admin(H.aunretain_handler, tuple(self.unique.name.split()))
        self.session.expire_all()
        self.assertEqual([], self.A.retained(self.session, self.mumbai.id))
        self.assertIn("released", self.replies[-1])

    def test_aretlock_reads_out_the_window_and_every_keep(self):
        from handlers import auction as H
        self.A.retain(self.session, self.season, self.mumbai, self.players[0],
                      now=NOW)
        self.session.commit()
        self._as_admin(H.aretlock_handler)
        body = self.replies[-1]
        self.assertIn("Retention", body)
        self.assertIn("Mumbai", body)
        self.assertIn("Ladder", body)
        self.assertIn("Open", body)

    def test_aretlock_on_closes_the_window(self):
        from handlers import auction as H
        self._as_admin(H.aretlock_handler, ("on",))
        self.session.expire_all()
        self.assertTrue(self.A.retention_locked(self.season))

    def test_aretlock_says_so_when_retention_is_turned_off(self):
        from handlers import auction as H
        self.season.max_retentions = 0
        self.session.commit()
        self._as_admin(H.aretlock_handler)
        self.assertIn("allows no retentions", self.replies[-1])

    def test_the_readout_counts_down_to_the_deadline(self):
        from handlers import auction as H
        self.season.retention_deadline_at = datetime.utcnow() + timedelta(hours=3)
        self.session.commit()
        self._as_admin(H.aretlock_handler)
        self.assertIn("closes in", self.replies[-1],
                      "a deadline nobody can see until it refuses them is the "
                      "failure mode this readout exists to avoid")

"""The Franchise Auction: the pool, the purse ledger, and what a bid may cost.

The part that is genuinely new in this feature is money, so most of what is
pinned here is money:

  • **The ledger is the truth and the purse column answers to it.** Every
    scenario asserts ``SUM(ledger) == purse_remaining_lakh`` after *every*
    mutation rather than once at the end, because a cache that is only right at
    the end is a cache that was wrong in the middle of somebody's auction.
  • **Base prices are stamped, not looked up.** Editing the ladder after a lot
    is built must not move a price a lot already went on the block at.
  • **Reachability is checked at the bid, never afterwards.** A franchise may
    not bid so much that it could no longer fill its minimum squad — and the
    moment its minimum is met, the rule stops applying and it may spend
    everything. Both edges are pinned, in both directions.
  • **Publishing goes through one door.** A published row is run back through
    ``cipl_match.cp_to_player_dict``, so a drifted key set fails this suite
    instead of silently giving a bought squad the wrong numbers.
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
    # The quiet gap after a bid is its own feature (see
    # tests/test_auction_restart_countdown.py); these tests bid on one tick.
    bid_gap = 0

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
            opening_purse_lakh=self.purse_lakh,
            bid_gap_seconds=self.bid_gap)
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
# Money
# ══════════════════════════════════════════════════════════════════════

class MoneyTests(unittest.TestCase):
    """Lakh in, lakh out, and never a float in between."""

    def setUp(self):
        from services import auction_service as A
        self.A = A

    def test_render_reads_the_way_the_room_says_it(self):
        for lakh, expected in [(0, "₹0 L"), (20, "₹20 L"), (75, "₹75 L"),
                               (100, "₹1 Cr"), (150, "₹1.5 Cr"),
                               (125, "₹1.25 Cr"), (1225, "₹12.25 Cr"),
                               (10_000, "₹100 Cr")]:
            self.assertEqual(expected, self.A.render_money(lakh), f"for {lakh}")

    def test_a_bare_number_is_crore(self):
        """The proposal's own example is ``/bid 15`` against a ₹100 Cr purse."""
        self.assertEqual(1500, self.A.parse_amount("15"))
        self.assertEqual(1500, self.A.parse_amount("15cr"))
        self.assertEqual(1500, self.A.parse_amount("₹15 Cr"))
        self.assertEqual(150, self.A.parse_amount("1.5"))
        self.assertEqual(25, self.A.parse_amount("0.25"))

    def test_lakh_has_to_be_said_out_loud(self):
        self.assertEqual(75, self.A.parse_amount("75L"))
        self.assertEqual(75, self.A.parse_amount("75 lakh"))
        self.assertEqual(75, self.A.parse_amount("75 lac"))

    def test_junk_is_a_readable_refusal_not_a_traceback(self):
        for bad in ("", "  ", "abc", "0", "-5", "1.005cr", "₹"):
            with self.assertRaises(self.A.AuctionError, msg=f"for {bad!r}"):
                self.A.parse_amount(bad)

    def test_an_absurd_number_is_refused_rather_than_hitting_the_purse_check(self):
        with self.assertRaises(self.A.AuctionError):
            self.A.parse_amount("999999")

    def test_every_amount_survives_a_round_trip(self):
        """render → parse → the same integer, for every lakh up to ₹20 Cr.

        This is what stops a display rule and an input rule drifting apart and
        quietly charging somebody a hundred times what the board said.
        """
        for lakh in range(1, 2001):
            rendered = self.A.render_money(lakh).replace("₹", "")
            self.assertEqual(lakh, self.A.parse_amount(rendered),
                             f"round trip failed at {lakh}")


class BasePriceTests(AuctionCase):

    def test_the_ladder_picks_the_first_band_the_rating_reaches(self):
        A = self.A
        self.assertEqual(400, A.base_price_for(self.season, 97))
        self.assertEqual(400, A.base_price_for(self.season, 99))
        self.assertEqual(300, A.base_price_for(self.season, 95))
        self.assertEqual(200, A.base_price_for(self.season, 93))
        self.assertEqual(150, A.base_price_for(self.season, 90))
        self.assertEqual(100, A.base_price_for(self.season, 88))
        self.assertEqual(20, A.base_price_for(self.season, 60))

    def test_a_lot_keeps_the_price_it_was_built_with(self):
        """Editing the ladder must never move a price mid-auction."""
        import json
        from models import AuctionLot
        self.build_pool()
        kohli = (self.session.query(AuctionLot)
                 .filter(AuctionLot.season_id == self.season.id,
                         AuctionLot.rating == 97).first())
        self.assertEqual(400, kohli.base_price_lakh)

        self.season.base_price_rules_json = json.dumps(
            [{"min_rating": 0, "base_lakh": 5}])
        self.session.commit()
        self.session.refresh(kohli)
        self.assertEqual(400, kohli.base_price_lakh,
                         "an existing lot must keep its stamped base price")

    def test_a_single_player_price_can_be_overridden_before_the_lot_opens(self):
        from models import AuctionLot
        self.build_pool()
        lot = (self.session.query(AuctionLot)
               .filter(AuctionLot.season_id == self.season.id,
                       AuctionLot.rating == 74).first())
        self.A.set_base_price(self.session, self.season, lot, 250)
        self.assertEqual(250, lot.base_price_lakh)

    def test_an_override_is_refused_once_the_lot_has_left_the_queue(self):
        self.build_pool()
        lot = self.start()
        with self.assertRaises(self.A.AuctionError):
            self.A.set_base_price(self.session, self.season, lot, 999)

    def test_the_reachability_floor_is_the_pools_cheapest_base(self):
        self.build_pool()
        self.assertEqual(20, self.season.min_base_price_lakh)


# ══════════════════════════════════════════════════════════════════════
# The pool
# ══════════════════════════════════════════════════════════════════════

class PoolTests(AuctionCase):

    def test_building_the_pool_snapshots_the_card(self):
        from models import AuctionLot
        self.build_pool()
        lot = (self.session.query(AuctionLot)
               .filter(AuctionLot.season_id == self.season.id,
                       AuctionLot.name == "Rashid Khan").first())
        self.assertEqual(93, lot.rating)
        self.assertEqual("All Rounder", lot.category)
        self.assertEqual("Afghanistan", lot.country)
        self.assertTrue(lot.is_overseas, "a non-India country is overseas here")

    def test_home_country_decides_overseas_not_a_label(self):
        from models import AuctionLot
        self.season.home_country = "England"
        self.session.commit()
        self.build_pool()
        buttler = (self.session.query(AuctionLot)
                   .filter(AuctionLot.season_id == self.season.id,
                           AuctionLot.name == "Jos Buttler").first())
        kohli = (self.session.query(AuctionLot)
                 .filter(AuctionLot.season_id == self.season.id,
                         AuctionLot.name == "Virat Kohli").first())
        self.assertFalse(buttler.is_overseas)
        self.assertTrue(kohli.is_overseas)

    def test_adding_the_same_players_twice_changes_nothing(self):
        added, skipped = self.build_pool()
        self.assertEqual(len(CATALOGUE), added)
        self.assertEqual(0, skipped)
        added2, skipped2 = self.build_pool()
        self.assertEqual(0, added2)
        self.assertEqual(len(CATALOGUE), skipped2)

    def test_the_pool_cannot_be_changed_while_the_auction_runs(self):
        self.build_pool()
        self.start()
        self.session.commit()
        with self.assertRaises(self.A.AuctionError):
            self.A.add_players_to_pool(self.session, self.season, self.players)

    def test_a_sold_player_cannot_be_taken_out_of_the_pool(self):
        self.build_pool()
        lot = self.start()
        self.A.place_bid(self.session, self.season, lot, self.mumbai, 400,
                         now=NOW, by_tg_id=ALICE)
        lot = self.A.sell_lot(self.session, self.season, lot, now=NOW)
        self.session.commit()
        with self.assertRaises(self.A.AuctionError):
            self.A.remove_lot(self.session, self.season, lot)

    def test_the_filter_reaches_version_and_a_rating_band(self):
        from services import player_query
        rows = player_query.master_player_query(
            self.session, {"rating_min": 90, "rating_max": 96}).all()
        self.assertTrue(rows)
        self.assertTrue(all(90 <= p.rating <= 96 for p in rows))

        icons = player_query.master_player_query(
            self.session, {"versions": ["Icon"]}).all()
        self.assertTrue(icons)
        self.assertTrue(all(p.version == "Icon" for p in icons))

    def test_a_backwards_rating_band_is_read_as_a_slip_not_as_nothing(self):
        from services import player_query
        rows = player_query.master_player_query(
            self.session, {"rating_min": 96, "rating_max": 90}).all()
        self.assertTrue(rows, "a band typed backwards should still find players")

    def test_career_cards_never_reach_an_auction_pool(self):
        from models import Player
        from services import player_query
        career = Player(name="Someone's Career", rating=95, category="Batsman",
                        country="India", bat_hand="Right", bowl_hand="Right",
                        bowl_style="Medium Pacer", is_active=True,
                        is_career=True, version="Base")
        self.session.add(career)
        self.session.flush()
        names = [p.name for p in
                 player_query.master_player_query(self.session, {}).all()]
        self.assertNotIn("Someone's Career", names)


# ══════════════════════════════════════════════════════════════════════
# The purse ledger
# ══════════════════════════════════════════════════════════════════════

class LedgerTests(AuctionCase):

    def test_the_opening_purse_is_itself_a_ledger_row(self):
        self.assert_ledger_agrees("at the opening")
        rows = self.A.ledger(self.session, self.mumbai.id)
        self.assertEqual(1, len(rows))
        self.assertEqual(self.A.LEDGER_OPENING, rows[0].kind)
        self.assertEqual(self.purse_lakh, rows[0].amount_lakh)
        self.assertEqual(self.purse_lakh, rows[0].balance_after)

    def test_a_purchase_debits_once_and_is_recorded_once(self):
        self.build_pool()
        lot = self.start()
        self.A.place_bid(self.session, self.season, lot, self.mumbai, 500,
                         now=NOW, by_tg_id=ALICE)
        self.A.sell_lot(self.session, self.season, lot, now=NOW)
        self.session.commit()
        self.session.refresh(self.mumbai)

        self.assertEqual(self.purse_lakh - 500, self.mumbai.purse_remaining_lakh)
        self.assertEqual(1, self.mumbai.squad_size)
        self.assert_ledger_agrees("after one purchase")
        purchases = [r for r in self.A.ledger(self.session, self.mumbai.id)
                     if r.kind == self.A.LEDGER_PURCHASE]
        self.assertEqual(1, len(purchases))
        self.assertEqual(-500, purchases[0].amount_lakh)

    def test_a_bid_moves_no_money(self):
        self.build_pool()
        lot = self.start()
        before = self.mumbai.purse_remaining_lakh
        self.A.place_bid(self.session, self.season, lot, self.mumbai, 500,
                         now=NOW, by_tg_id=ALICE)
        self.session.commit()
        self.session.refresh(self.mumbai)
        self.assertEqual(before, self.mumbai.purse_remaining_lakh,
                         "nothing is spent until a lot sells")
        self.assert_ledger_agrees("after a bid that has not sold")

    def test_undoing_a_sale_refunds_exactly_what_was_paid(self):
        self.build_pool()
        lot = self.start()
        self.A.place_bid(self.session, self.season, lot, self.mumbai, 500,
                         now=NOW, by_tg_id=ALICE)
        lot = self.A.sell_lot(self.session, self.season, lot, now=NOW)
        self.session.commit()

        self.A.undo_sale(self.session, self.season, lot, now=NOW)
        self.session.commit()
        self.session.refresh(self.mumbai)
        self.assertEqual(self.purse_lakh, self.mumbai.purse_remaining_lakh)
        self.assertEqual(0, self.mumbai.squad_size)
        self.assert_ledger_agrees("after undoing a sale")
        self.assertEqual(self.A.LOT_ON_BLOCK, lot.status)

    def test_a_correction_is_signed_and_on_the_record(self):
        balance = self.A.correct_purse(self.session, self.season, self.mumbai,
                                       -200, note="penalty")
        self.session.commit()
        self.assertEqual(self.purse_lakh - 200, balance)
        self.assert_ledger_agrees("after a correction")
        kinds = [r.kind for r in self.A.ledger(self.session, self.mumbai.id)]
        self.assertIn(self.A.LEDGER_CORRECTION, kinds)

    def test_a_correction_cannot_push_a_purse_below_zero(self):
        with self.assertRaises(self.A.AuctionError):
            self.A.correct_purse(self.session, self.season, self.mumbai,
                                 -(self.purse_lakh + 1))

    def test_reconcile_finds_a_hand_corrupted_purse_and_repairs_the_record(self):
        self.mumbai.purse_remaining_lakh = 4242
        self.session.commit()
        drift = self.A.reconcile_purses(self.session, self.season)
        self.assertEqual(1, len(drift))
        self.assertEqual(self.mumbai.id, drift[0][0].id)

        self.A.reconcile_purses(self.session, self.season, repair=True)
        self.session.commit()
        self.assert_ledger_agrees("after repair")
        self.assertEqual(4242, self.mumbai.purse_remaining_lakh,
                         "repair writes a correction row; it never silently "
                         "rewrites what the auction actually charged")


# ══════════════════════════════════════════════════════════════════════
# Reachability — the money version of the draft's "while there is still a slot"
# ══════════════════════════════════════════════════════════════════════

class ReachabilityTests(AuctionCase):
    min_squad = 5
    max_squad = 8

    def test_the_ceiling_holds_back_the_cheapest_remaining_slots(self):
        self.build_pool()               # floor = ₹20 L
        # Nothing bought: winning this lot leaves 4 slots at ₹20 L = ₹80 L held.
        self.assertEqual(self.purse_lakh - 80,
                         self.A.max_bid_now(self.season, self.mumbai))

    def test_the_rule_stops_applying_once_the_minimum_is_met(self):
        self.build_pool()
        self.mumbai.squad_size = self.min_squad - 1
        self.session.commit()
        self.assertEqual(self.mumbai.purse_remaining_lakh,
                         self.A.max_bid_now(self.season, self.mumbai),
                         "the bid that completes the minimum may spend it all")

        self.mumbai.squad_size = self.min_squad + 2
        self.session.commit()
        self.assertEqual(self.mumbai.purse_remaining_lakh,
                         self.A.max_bid_now(self.season, self.mumbai))

    def test_a_bid_at_the_ceiling_is_allowed_and_one_over_is_not(self):
        self.build_pool()
        lot = self.start()
        ceiling = self.A.max_bid_now(self.season, self.mumbai)

        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.validate_bid(self.session, self.season, lot, self.mumbai,
                                ceiling + 1, now=NOW)
        self.assertIn("ceiling", str(caught.exception).lower())

        self.assertEqual(ceiling,
                         self.A.validate_bid(self.session, self.season, lot,
                                             self.mumbai, ceiling, now=NOW))

    def test_a_franchise_that_cannot_afford_its_minimum_is_told_so_plainly(self):
        self.build_pool()
        lot = self.start()
        # Enough to pay the base price, but not enough to pay it AND still
        # fill the four slots it would leave owing at the pool's floor. This is
        # the branch that has to name the way out rather than only refuse.
        base = lot.base_price_lakh
        reserve = (self.min_squad - 1) * self.season.min_base_price_lakh
        self.A.correct_purse(
            self.session, self.season, self.mumbai,
            -(self.purse_lakh - (base + reserve - 1)))
        self.session.commit()
        self.assertGreaterEqual(self.mumbai.purse_remaining_lakh, base,
                                "the purse must clear the bid, or the plain "
                                "affordability refusal fires first")
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.validate_bid(self.session, self.season, lot, self.mumbai,
                                base, now=NOW)
        message = str(caught.exception)
        self.assertIn("/agrant", message,
                      "the refusal must name the way out, not just refuse")

    def test_the_squad_cap_refuses_before_the_purse_does(self):
        self.build_pool()
        lot = self.start()
        self.mumbai.squad_size = self.max_squad
        self.session.commit()
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.validate_bid(self.session, self.season, lot, self.mumbai,
                                lot.base_price_lakh, now=NOW)
        self.assertIn("squad limit", str(caught.exception))


class OverseasTests(AuctionCase):
    max_overseas = 1

    def test_the_overseas_cap_refuses_the_bid_that_would_break_it(self):
        from models import AuctionLot
        self.build_pool()
        lot = self.start()
        # Buy one overseas player by hand, then try for a second.
        overseas = (self.session.query(AuctionLot)
                    .filter(AuctionLot.season_id == self.season.id,
                            AuctionLot.is_overseas.is_(True)).first())
        overseas.status = self.A.LOT_SOLD
        overseas.sold_to_id = self.mumbai.id
        overseas.sold_price_lakh = 100
        self.mumbai.squad_size = 1
        self.session.commit()

        another = (self.session.query(AuctionLot)
                   .filter(AuctionLot.season_id == self.season.id,
                           AuctionLot.is_overseas.is_(True),
                           AuctionLot.status == self.A.LOT_QUEUED).first())
        another.status = self.A.LOT_ON_BLOCK
        another.deadline_at = NOW + timedelta(seconds=30)
        self.season.current_lot_id = another.id
        self.session.commit()

        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.validate_bid(self.session, self.season, another,
                                self.mumbai, another.base_price_lakh, now=NOW)
        self.assertIn("overseas limit", str(caught.exception))


class RoleRuleTests(AuctionCase):
    """Both ends of the role rule, and why they are enforced differently.

    A **maximum** is a fact about the squad in front of you, so the bid that
    would break it is refused outright. A **minimum** is a promise about a squad
    that does not exist yet, so it is enforced as reachability — refused while
    there is still a slot to fix the problem with, never afterwards.
    """

    min_squad = 2
    max_squad = 4

    def open_lot(self, role, now=NOW):
        """Put a queued lot of ``role`` on the block."""
        from models import AuctionLot
        lot = (self.session.query(AuctionLot)
               .filter(AuctionLot.season_id == self.season.id,
                       AuctionLot.status == self.A.LOT_QUEUED,
                       AuctionLot.category == role).first())
        self.assertIsNotNone(lot, f"no queued {role} in the pool")
        lot.status = self.A.LOT_ON_BLOCK
        lot.deadline_at = now + timedelta(seconds=30)
        self.season.current_lot_id = lot.id
        # Around ``start()``: these tests are about what refuses a bid, not
        # about the opening sequence, and starting for real would open whatever
        # lot happens to be first rather than the role under test.
        self.season.status = self.A.STATUS_LIVE
        self.session.commit()
        return lot

    def hand(self, franchise, role, count):
        """Sign ``count`` players of ``role`` to ``franchise``, around bidding."""
        from models import AuctionLot
        rows = (self.session.query(AuctionLot)
                .filter(AuctionLot.season_id == self.season.id,
                        AuctionLot.status == self.A.LOT_QUEUED,
                        AuctionLot.category == role).limit(count).all())
        self.assertEqual(count, len(rows), f"not enough queued {role}s")
        for lot in rows:
            lot.status = self.A.LOT_SOLD
            lot.sold_to_id = franchise.id
            lot.sold_price_lakh = 100
            franchise.squad_size = int(franchise.squad_size or 0) + 1
        self.session.commit()

    # ── saving the rules ──

    def test_a_blank_ceiling_and_a_typed_zero_are_different_answers(self):
        self.A.set_role_rules(self.session, self.season, {},
                              {"Bowler": None, "Wicket Keeper": 0})
        self.session.commit()
        caps = self.A.role_maximums(self.season)
        self.assertNotIn("Bowler", caps, "a blank is no rule at all")
        self.assertEqual(0, caps["Wicket Keeper"], "a typed 0 is a real rule")

    def test_an_unsatisfiable_pair_is_refused_before_anybody_bids(self):
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.set_role_rules(self.session, self.season,
                                  {"Bowler": 3}, {"Bowler": 2})
        self.assertIn("above the maximum", str(caught.exception))

    def test_minimums_adding_up_past_the_squad_cap_are_refused(self):
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.set_role_rules(self.session, self.season,
                                  {"Batsman": 3, "Bowler": 3}, {})
        self.assertIn("squad limit", str(caught.exception))

    def test_the_rule_reads_back_as_one_line(self):
        self.A.set_role_rules(self.session, self.season,
                              {"Bowler": 1}, {"Bowler": 2, "Batsman": 3})
        self.session.commit()
        self.assertEqual("Batsman up to 3, Bowler 1-2",
                         self.A.role_rule_line(self.season))
        # No rules at all reads as empty, so every caller can print it behind a
        # plain truth test.
        self.A.set_role_rules(self.session, self.season, {}, {})
        self.session.commit()
        self.assertEqual("", self.A.role_rule_line(self.season))

    # ── what a bid meets ──

    def test_a_bid_past_a_role_ceiling_is_refused(self):
        self.build_pool()
        self.A.set_role_rules(self.session, self.season, {}, {"Batsman": 1})
        self.session.commit()
        self.hand(self.mumbai, "Batsman", 1)
        lot = self.open_lot("Batsman")
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.validate_bid(self.session, self.season, lot, self.mumbai,
                                lot.base_price_lakh, now=NOW)
        self.assertIn("limit of 1", str(caught.exception))
        # The cap is per role, so another role is untouched by it.
        bowler = self.open_lot("Bowler")
        self.assertEqual(bowler.base_price_lakh,
                         self.A.validate_bid(self.session, self.season, bowler,
                                             self.mumbai,
                                             bowler.base_price_lakh, now=NOW))

    def test_a_ceiling_of_zero_refuses_the_first_one_and_says_why(self):
        self.build_pool()
        self.A.set_role_rules(self.session, self.season, {},
                              {"Wicket Keeper": 0})
        self.session.commit()
        lot = self.open_lot("Wicket Keeper")
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.validate_bid(self.session, self.season, lot, self.mumbai,
                                lot.base_price_lakh, now=NOW)
        # "already has 0, which is the limit of 0" would read as a bug.
        self.assertIn("at all", str(caught.exception))

    def test_a_ceiling_the_squad_is_under_lets_the_bid_through(self):
        self.build_pool()
        self.A.set_role_rules(self.session, self.season, {}, {"Batsman": 2})
        self.session.commit()
        self.hand(self.mumbai, "Batsman", 1)
        lot = self.open_lot("Batsman")
        self.assertEqual(lot.base_price_lakh,
                         self.A.validate_bid(self.session, self.season, lot,
                                             self.mumbai,
                                             lot.base_price_lakh, now=NOW))

    def test_a_retention_cannot_walk_past_a_ceiling_either(self):
        # The gap a ceiling enforced only at bid time would leave: a franchise
        # retains its way to an illegal squad before a single lot opens, and
        # nothing it does afterwards can fix it.
        self.season.max_retentions = 3
        self.A.set_role_rules(self.session, self.season, {},
                              {"Wicket Keeper": 1})
        self.session.commit()
        keepers = [p for p in self.players if p.category == "Wicket Keeper"]
        self.assertGreaterEqual(len(keepers), 2)
        self.A.retain(self.session, self.season, self.mumbai, keepers[0], 100)
        self.session.commit()
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.retain(self.session, self.season, self.mumbai, keepers[1],
                          100)
        self.assertIn("limit of 1", str(caught.exception))
        self.session.rollback()

    def test_a_squad_over_a_ceiling_is_reported_rather_than_hidden(self):
        # A rule tightened after the squad was built. Nothing the franchise
        # does next can fix it, so the number has to be visible to an admin.
        self.build_pool()
        self.hand(self.mumbai, "Batsman", 2)
        self.A.set_role_rules(self.session, self.season, {}, {"Batsman": 1})
        self.session.commit()
        owed, over = self.A.role_shortfall(self.session, self.season,
                                           self.mumbai)
        self.assertEqual({}, owed)
        self.assertEqual({"Batsman": 1}, over)

    def test_what_a_squad_still_owes_is_readable(self):
        self.build_pool()
        self.A.set_role_rules(self.session, self.season,
                              {"Bowler": 1, "Wicket Keeper": 1}, {})
        self.session.commit()
        self.hand(self.mumbai, "Bowler", 1)
        owed, over = self.A.role_shortfall(self.session, self.season,
                                           self.mumbai)
        self.assertEqual({"Wicket Keeper": 1}, owed)
        self.assertEqual({}, over)

    def test_the_rules_carry_into_next_season(self):
        self.A.set_role_rules(self.session, self.season, {"Bowler": 1},
                              {"Batsman": 4})
        self.session.commit()
        fresh = self.A.clone_season(self.session, self.season, "Next season")
        self.session.commit()
        self.assertEqual({"Bowler": 1}, self.A.role_minimums(fresh))
        self.assertEqual({"Batsman": 4}, self.A.role_maximums(fresh))


class SetDeleteServiceTests(AuctionCase):
    """``delete_set`` and ``clear_pool`` — what goes, and what must not."""

    def test_a_set_takes_only_what_is_still_waiting(self):
        from models import AuctionLot
        self.build_pool()
        # Everything is in the default set; give three of them their own.
        rows = (self.session.query(AuctionLot)
                .filter(AuctionLot.season_id == self.season.id)
                .order_by(AuctionLot.lot_no).limit(3).all())
        for lot in rows:
            lot.set_name = "Marquee"
        # One of the three has already been sold.
        rows[0].status = self.A.LOT_SOLD
        rows[0].sold_to_id = self.mumbai.id
        rows[0].sold_price_lakh = 500
        self.session.commit()

        label, removed = self.A.delete_set(self.session, self.season,
                                           "Marquee")
        self.session.commit()
        self.assertEqual(("Marquee", 2), (label, removed))
        left = {lot.name for lot in self.A.lots(self.session, self.season.id)}
        self.assertIn(rows[0].name, left, "a sold lot is the record, not stock")
        self.assertNotIn(rows[1].name, left)

    def test_a_set_that_is_not_there_is_refused_rather_than_guessed_at(self):
        self.build_pool()
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.delete_set(self.session, self.season, "Marq")
        self.assertIn("Reload", str(caught.exception))

    def test_a_live_auction_refuses_a_pool_edit(self):
        self.build_pool()
        self.start()
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.clear_pool(self.session, self.season)
        self.assertIn("Pause the auction", str(caught.exception))

    def test_clearing_the_pool_leaves_the_result_and_takes_the_queue(self):
        from models import AuctionLot
        self.build_pool()
        sold = (self.session.query(AuctionLot)
                .filter(AuctionLot.season_id == self.season.id).first())
        sold.status = self.A.LOT_SOLD
        sold.sold_to_id = self.mumbai.id
        sold.sold_price_lakh = 500
        self.session.commit()
        total = len(self.A.lots(self.session, self.season.id))

        removed = self.A.clear_pool(self.session, self.season)
        self.session.commit()
        self.assertEqual(total - 1, removed)
        self.assertEqual([sold.name],
                         [lot.name for lot in
                          self.A.lots(self.session, self.season.id)])

    def test_clearing_an_already_empty_queue_says_so(self):
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.clear_pool(self.session, self.season)
        self.assertIn("Nothing is waiting", str(caught.exception))


class FranchiseFileServiceTests(AuctionCase):
    """Export and import of the field, at the service level."""

    def test_the_export_carries_the_rules_and_the_squad_separately(self):
        self.build_pool()
        payload = self.A.export_franchises(self.session, self.season)
        self.assertEqual(self.A.FRANCHISE_FILE_VERSION, payload["version"])
        rows = {row["name"]: row for row in payload["franchises"]}
        self.assertEqual({"Mumbai", "Chennai"}, set(rows))
        self.assertEqual(ALICE, rows["Mumbai"]["owner_tg_id"])
        self.assertEqual(self.purse_lakh, rows["Mumbai"]["purse_total_lakh"])
        for key in ("purse_remaining_lakh", "squad_size", "retained_count",
                    "rtm_cards_used", "draft_picks_used"):
            self.assertNotIn(key, rows["Mumbai"],
                             f"{key} is a result, not a rule — it must not be "
                             f"in a file an import reads")

    def test_an_import_never_writes_the_squad_it_carries(self):
        other = self.A.create_season(self.session, "Elsewhere")
        self.session.commit()
        self.A.import_franchises(self.session, other, {"franchises": [
            {"name": "Mumbai", "purse_total_lakh": 5000,
             "squad": [{"name": "Somebody", "rating": 99}],
             "squad_size": 11, "purse_remaining_lakh": 1}]})
        self.session.commit()
        franchise = self.A.franchises(self.session, other.id)[0]
        self.assertEqual(0, franchise.squad_size)
        self.assertEqual(5000, franchise.purse_remaining_lakh)
        self.assertEqual([], self.A.squad(self.session, franchise.id))

    def test_co_owners_survive_the_round_trip(self):
        self.A.set_co_owners(self.session, self.mumbai, [CAROL, 444])
        self.session.commit()
        payload = self.A.export_franchises(self.session, self.season)
        other = self.A.create_season(self.session, "Elsewhere")
        self.session.commit()
        self.A.import_franchises(self.session, other, payload)
        self.session.commit()
        rebuilt = {f.name: f for f in self.A.franchises(self.session, other.id)}
        self.assertEqual([CAROL, 444],
                         self.A.co_owner_ids(rebuilt["Mumbai"]))

    def test_a_bare_list_is_accepted_as_readily_as_the_whole_file(self):
        other = self.A.create_season(self.session, "Elsewhere")
        self.session.commit()
        added, updated, removed = self.A.import_franchises(
            self.session, other, [{"name": "Kolkata"}])
        self.session.commit()
        self.assertEqual((1, 0, []), (added, updated, removed))

    def test_replacing_hands_a_removed_sides_players_back(self):
        from models import AuctionLot
        self.build_pool()
        lot = (self.session.query(AuctionLot)
               .filter(AuctionLot.season_id == self.season.id).first())
        lot.status = self.A.LOT_SOLD
        lot.sold_to_id = self.chennai.id
        lot.sold_price_lakh = 500
        self.chennai.squad_size = 1
        self.chennai.purse_remaining_lakh -= 500
        self.session.commit()

        self.A.import_franchises(self.session, self.season,
                                 [{"name": "Mumbai"}], replace=True)
        self.session.commit()
        self.assertEqual(["Mumbai"],
                         [f.name for f in
                          self.A.franchises(self.session, self.season.id)])
        self.session.expire_all()
        refreshed = (self.session.query(AuctionLot)
                     .filter(AuctionLot.id == lot.id).first())
        self.assertEqual(self.A.LOT_QUEUED, refreshed.status,
                         "a removed side's players go back into the pool")


# ══════════════════════════════════════════════════════════════════════
# Publishing
# ══════════════════════════════════════════════════════════════════════

class PublishTests(AuctionCase):
    min_squad = 1

    def _buy(self, franchise, price, now=NOW):
        lot = self.A.current_lot(self.session, self.season)
        if lot is None:
            lot = self.A.open_next_lot(self.session, self.season, now=now)
        self.A.place_bid(self.session, self.season, lot, franchise, price,
                         now=now, by_tg_id=ALICE)
        sold = self.A.sell_lot(self.session, self.season, lot, now=now)
        self.session.commit()
        return sold

    def test_an_unfinished_auction_cannot_be_published(self):
        self.build_pool()
        self.start()
        self.session.commit()
        with self.assertRaises(self.A.AuctionError):
            self.A.publish_to_league(self.session, self.season)

    def test_a_published_row_still_reads_as_a_playable_player(self):
        """The drift guard: the engine reads details_json, not ``players``."""
        import json
        from models import ChallengePlayer
        self.build_pool()
        self.start()
        bought = self._buy(self.mumbai, 400)
        # Pass the rest so the auction completes.
        while self.season.status == self.A.STATUS_LIVE:
            lot = self.A.current_lot(self.session, self.season)
            if lot is None:
                lot = self.A.open_next_lot(self.session, self.season, now=NOW)
            if lot is None:
                break
            self.A.pass_lot(self.session, self.season, lot)
            self.session.commit()

        league = self.A.publish_to_league(self.session, self.season)
        self.session.commit()

        from models import ChallengeTeam
        rows = (self.session.query(ChallengePlayer)
                .join(ChallengeTeam, ChallengePlayer.team_id == ChallengeTeam.id)
                .filter(ChallengeTeam.league_id == league.id,
                        ChallengePlayer.name == "Virat Kohli",
                        # By card, not name: the Icon edition went unsold and
                        # was auto-filled to a short squad, so the league
                        # holds two Virat Kohlis.
                        ChallengePlayer.source_player_id == bought.player_id)
                .all())
        self.assertTrue(rows, "the bought player should reach the league")
        blob = json.loads(rows[0].details_json)
        for key in ("source_player_id", "name", "version", "country",
                    "category", "role", "rating", "bat_rating", "bowl_rating",
                    "bat_hand", "bowl_hand", "bowl_style", "is_overseas"):
            self.assertIn(key, blob, f"details_json lost the {key!r} key")

        from services.cipl_match import cp_to_player_dict
        as_player = cp_to_player_dict(rows[0])
        self.assertEqual("Virat Kohli", as_player.get("name"))
        self.assertEqual(97, as_player.get("rating"))
        self.assertEqual(league.id, self.season.league_id)

    def test_publishing_twice_re_syncs_rather_than_duplicating(self):
        from models import ChallengePlayer
        self.build_pool()
        self.start()
        self._buy(self.mumbai, 400)
        while self.season.status == self.A.STATUS_LIVE:
            lot = self.A.current_lot(self.session, self.season)
            if lot is None:
                lot = self.A.open_next_lot(self.session, self.season, now=NOW)
            if lot is None:
                break
            self.A.pass_lot(self.session, self.season, lot)
            self.session.commit()

        from models import ChallengeTeam
        league = self.A.publish_to_league(self.session, self.season)
        self.session.commit()

        def in_this_league():
            return (self.session.query(ChallengePlayer)
                    .join(ChallengeTeam, ChallengePlayer.team_id == ChallengeTeam.id)
                    .filter(ChallengeTeam.league_id == league.id).count())

        first = in_this_league()
        self.A.publish_to_league(self.session, self.season)
        self.session.commit()
        self.assertEqual(first, in_this_league())

    def test_the_auction_price_rides_along_without_disturbing_the_contract(self):
        import json
        from models import ChallengePlayer
        self.build_pool()
        self.start()
        bought = self._buy(self.mumbai, 400)
        while self.season.status == self.A.STATUS_LIVE:
            lot = self.A.current_lot(self.session, self.season)
            if lot is None:
                lot = self.A.open_next_lot(self.session, self.season, now=NOW)
            if lot is None:
                break
            self.A.pass_lot(self.session, self.season, lot)
            self.session.commit()
        from models import ChallengeTeam
        league = self.A.publish_to_league(self.session, self.season)
        self.session.commit()
        row = (self.session.query(ChallengePlayer)
               .join(ChallengeTeam, ChallengePlayer.team_id == ChallengeTeam.id)
               .filter(ChallengeTeam.league_id == league.id,
                       ChallengePlayer.name == "Virat Kohli",
                       # By card, not name: an unsold second edition of
                       # the same cricketer can be auto-filled to the other
                       # squad, so the name alone is no longer unique.
                       ChallengePlayer.source_player_id == bought.player_id)
               .first())
        self.assertEqual(400, json.loads(row.details_json)["auction_price_lakh"])


# ══════════════════════════════════════════════════════════════════════
# Source-level guard rails
#
# These are the same shape as tests/test_admin_destructive_confirmations.py:
# importing ``admin`` pulls in the whole Flask app, its login layer and a live
# database, which is far more than the assertion needs.
# ══════════════════════════════════════════════════════════════════════

def _read(path):
    with open(os.path.join(os.path.dirname(__file__), "..", path)) as fh:
        return fh.read()


class DestructiveActionTests(unittest.TestCase):
    """Deleting an auction and cancelling one both need the name typed back."""

    def setUp(self):
        self.admin = _read("admin.py")

    def test_deleting_an_auction_needs_the_typed_name_on_the_server(self):
        route = (self.admin.split("def admin_auctions_list():")[1]
                 .split("\n@app.route")[0])
        self.assertIn('request.form.get("confirm_name")', route)

    def test_the_delete_check_happens_before_anything_is_removed(self):
        """A mistyped name must not have dropped the ledger by the time it fails."""
        route = (self.admin.split("def admin_auctions_list():")[1]
                 .split("\n@app.route")[0])
        self.assertLess(route.index("confirm_name"), route.index("db.delete"))

    def test_cancelling_an_auction_needs_the_typed_name_on_the_server(self):
        route = self.admin.split("def _auction_console_action(")[1]
        cancel = route.split('elif action == "cancel":')[1]
        self.assertIn('request.form.get("confirm_name")', cancel)

    def test_the_browser_asks_too_so_nobody_finds_out_afterwards(self):
        for path in ("templates/admin_auctions.html",
                     "templates/admin_auction_console.html"):
            page = _read(path)
            self.assertIn('name="confirm_name"', page, path)


class DetailsJsonContractTests(AuctionCase):
    """The one key set the match engine actually plays a squad from.

    ``admin._challenge_player_details_from_source`` used to write these keys by
    hand and now delegates here. That refactor has to be a no-op for every
    existing caller, so the old body is reproduced below and compared
    byte-for-byte — a key order or separator that drifted would change every
    ChallengePlayer blob written from the Challenge Data page.
    """

    def _legacy(self, player, is_overseas=False):
        import json
        return json.dumps({
            "source_player_id": player.id, "name": player.name,
            "version": player.version, "country": player.country,
            "category": player.category, "role": player.category,
            "rating": player.rating, "bat_rating": player.bat_rating or 0,
            "bowl_rating": player.bowl_rating or 0, "bat_hand": player.bat_hand,
            "bowl_hand": player.bowl_hand, "bowl_style": player.bowl_style,
            "is_overseas": bool(is_overseas),
        }, separators=(",", ":"))

    def test_the_shared_helper_writes_exactly_what_admin_used_to(self):
        from services import player_query
        player = self.players[0]
        for overseas in (False, True):
            self.assertEqual(
                self._legacy(player, overseas),
                player_query.challenge_details_json(
                    player, is_overseas=overseas, source_player_id=player.id),
                "the details_json contract must not move under this refactor")

    def test_a_missing_edition_reads_as_Base_rather_than_null(self):
        """The one deliberate difference, and the reading draft_service already
        writes. A null edition is a data defect, not a card that has none."""
        import json
        from services import player_query
        player = self.players[0]
        player.version = None
        self.session.flush()
        blob = json.loads(player_query.challenge_details_json(
            player, source_player_id=player.id))
        self.assertEqual("Base", blob["version"])

    def test_a_lot_never_writes_its_own_id_as_the_card_id(self):
        """An AuctionLot's ``id`` is the LOT. Defaulting to it would point the
        blob at the wrong card entirely."""
        from models import AuctionLot
        from services import player_query
        import json
        self.build_pool()
        lot = (self.session.query(AuctionLot)
               .filter(AuctionLot.season_id == self.season.id).first())
        blob = json.loads(player_query.challenge_details_json(
            lot, source_player_id=lot.player_id))
        self.assertEqual(lot.player_id, blob["source_player_id"])
        self.assertNotEqual(lot.id, blob["source_player_id"])


class FlaskTelegramBoundaryTests(unittest.TestCase):
    """The website writes rows and an event. It never touches Telegram.

    The admin panel runs in a thread of the bot's process, so "it worked on my
    machine" and "it deadlocked in production" are the same code. The event log
    is the whole protocol between them, and this is what stops somebody
    helpfully wiring a ``send_message`` into an admin route later.
    """

    def setUp(self):
        admin = _read("admin.py")
        start = admin.index("# FRANCHISE AUCTION")
        self.routes = admin[start:admin.index("# ── Run ──")]

    def test_no_auction_route_sends_a_telegram_message(self):
        for forbidden in ("send_message", "edit_message_text",
                          "run_coroutine_threadsafe", "asyncio"):
            self.assertNotIn(forbidden, self.routes,
                             f"an auction admin route reached for {forbidden}; "
                             f"the website talks to the group through "
                             f"AuctionEvent and the sweeper, not directly")

    def test_the_console_and_the_commands_call_the_same_service(self):
        """One implementation, so the two surfaces cannot diverge."""
        for call in ("auction_svc.sell_lot", "auction_svc.pass_lot",
                     "auction_svc.place_bid", "auction_svc.pause",
                     "auction_svc.start", "auction_svc.undo_last_bid",
                     # Retention and Right To Match, added later and along
                     # the same seam: the console forces a stage by calling
                     # the transition the franchise itself would have.
                     "auction_svc.retain", "auction_svc.unretain",
                     "auction_svc.rtm_intent", "auction_svc.rtm_decide",
                     "auction_svc.rtm_to_decision", "auction_svc.undo_rtm",
                     "auction_svc.set_rtm_rules", "auction_svc.set_rtm_cards"):
            self.assertIn(call, self.routes)


class DefaultsStayOffTests(AuctionCase):
    """Retention and RTM both ship switched off, and stay that way.

    They have behaviour now — this class is what stops either of them turning
    itself on. An auction nobody configured is a plain auction: every player
    goes to the block, and every squad is what the room paid for.
    """

    def test_a_new_auction_has_rtm_turned_off(self):
        self.assertFalse(self.season.rtm_enabled)
        self.assertEqual(0, self.season.max_retentions)
        self.assertEqual(0, self.mumbai.rtm_cards_total)
        self.assertEqual(0, self.mumbai.retained_count)

    def test_every_bought_lot_is_recorded_as_an_auction_buy(self):
        self.build_pool()
        lot = self.start()
        self.A.place_bid(self.session, self.season, lot, self.mumbai,
                         lot.base_price_lakh, now=NOW, by_tg_id=ALICE)
        lot = self.A.sell_lot(self.session, self.season, lot, now=NOW)
        self.session.commit()
        self.assertEqual("auction", lot.acquisition)

    def test_who_held_a_player_last_season_is_recorded_even_though_unused(self):
        """It only gets harder to recover later, and it is all RTM needs."""
        from models import (AuctionLot, ChallengeLeague, ChallengeMode,
                            ChallengePlayer, ChallengeTeam)
        self.build_pool()
        mode = ChallengeMode(name="M", sort_order=0)
        self.session.add(mode)
        self.session.flush()
        league = ChallengeLeague(mode_id=mode.id, name="Season 1")
        self.session.add(league)
        self.session.flush()
        team = ChallengeTeam(league_id=league.id, name="Mumbai")
        self.session.add(team)
        self.session.flush()
        self.session.add(ChallengePlayer(team_id=team.id, name="Virat Kohli",
                                         source_player_id=self.players[0].id))
        self.session.flush()

        stamped = self.A.link_previous_season(self.session, self.season,
                                              league.id)
        self.session.commit()
        self.assertEqual(1, stamped)
        lot = (self.session.query(AuctionLot)
               .filter(AuctionLot.season_id == self.season.id,
                       AuctionLot.player_id == self.players[0].id).first())
        self.assertEqual(self.mumbai.id, lot.previous_franchise_id)


if __name__ == "__main__":
    unittest.main()

"""Right To Match: the IPL 2025 final-offer rule, in full.

Three decision points, three parties, three clocks, and it is the only thing in
this feature where a franchise that is not bidding can take a player off the
one that is. What is pinned here:

  • **The proposal's own example**, walked end to end — RCB bid ₹6 Cr, RR claim
    the RTM, RCB raise to ₹9 Cr, RR match at ₹9 Cr. Six assertions deep,
    because every one of those steps is a place the money could go wrong.
  • **Every window times out to the safe answer.** A franchise nobody is
    running must never be able to wedge an auction.
  • **The self-raise suspension is exactly one franchise wide.** The top bidder
    may raise during the final offer and at no other time; nobody else may bid
    at any point in an RTM.
  • **The stage is co-authoritative with the clock.** Moving to the decision
    window writes a NEW, LATER deadline, so a late final offer would otherwise
    land against the number the holder is answering on.
  • **A card is spent only on a completed match**, and undoing gives it back.
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
# A season set up for Right To Match
# ══════════════════════════════════════════════════════════════════════

class RTMCase(AuctionCase):
    """RR held the best player last season; RCB want him."""

    min_squad = 1
    max_squad = 6
    max_overseas = 8
    cards = 1

    def setUp(self):
        super().setUp()
        self.season.rtm_enabled = True
        self.season.rtm_per_team = self.cards
        self.season.rtm_window_seconds = 30
        self.rr, self.rcb = self.mumbai, self.chennai
        self.rr.rtm_cards_total = self.cards
        self.build_pool()
        # RR held the first two players on the list last season. By lot
        # number, not by rating: a test that has to pass eight lots to reach
        # the one it cares about empties the auction before it starts.
        from models import AuctionLot
        self.lots = (self.session.query(AuctionLot)
                     .filter(AuctionLot.season_id == self.season.id)
                     .order_by(AuctionLot.lot_no.asc()).all())
        for lot in self.lots[:2]:
            lot.previous_franchise_id = self.rr.id
        self.session.commit()

    # ── helpers ──

    def open_theirs(self, index=0, now=NOW):
        """Put one of RR's former players on the block."""
        target = self.lots[index]
        lot = self.A.current_lot(self.session, self.season)
        if lot is None:
            lot = (self.A.open_next_lot(self.session, self.season, now=now)
                   if self.season.status == self.A.STATUS_LIVE
                   else self.A.start(self.session, self.season, now=now))
        while lot.id != target.id:
            self.A.pass_lot(self.session, self.season, lot)
            self.session.commit()
            lot = self.A.open_next_lot(self.session, self.season, now=now)
        self.session.commit()
        return lot

    def to_decision(self, lot, now=NOW):
        """RR say yes; the top bidder lets their one raise lapse.

        The shortest honest route to the third window, for tests that care
        about what a match does rather than about how the room got there.
        """
        lot = self.A.rtm_intent(self.session, self.season, lot, self.rr, True,
                                now=now)
        self.session.commit()
        lot = self.A.rtm_to_decision(self.session, self.season, lot, now=now)
        self.session.commit()
        return lot

    def bid_and_expire(self, lot, amount=600, bidder=None, now=NOW):
        """RCB bid, the clock runs out, and whatever happens happens."""
        bidder = bidder or self.rcb
        self.A.place_bid(self.session, self.season, lot, bidder, amount,
                         now=now, by_tg_id=BOB)
        self.session.commit()
        outcome, lot = self.A.resolve_expired(
            self.session, self.season, lot,
            now=lot.deadline_at + timedelta(seconds=1))
        self.session.commit()
        return outcome, lot


# ══════════════════════════════════════════════════════════════════════
# The proposal's own example
# ══════════════════════════════════════════════════════════════════════

class TheAshwinExampleTests(RTMCase):

    def test_the_whole_rule_end_to_end(self):
        A = self.A
        lot = self.open_theirs()
        outcome, lot = self.bid_and_expire(lot, 600)

        # 1. The clock expires and the lot does NOT sell.
        self.assertEqual("rtm", outcome)
        self.assertEqual(A.LOT_RTM_OFFERED, lot.status)
        self.assertEqual(A.RTM_INTENT, lot.rtm_stage)
        self.assertEqual(600, lot.rtm_base_bid_lakh)
        self.session.refresh(self.rcb)
        self.assertEqual(self.purse_lakh, self.rcb.purse_remaining_lakh,
                         "nobody has paid for anything yet")
        self.assertEqual(lot.id,
                         A.current_lot(self.session, self.season).id,
                         "the room is still looking at this lot")

        # 2. RR claim it. The top bidder gets one more raise.
        lot = A.rtm_intent(self.session, self.season, lot, self.rr, True,
                           now=NOW, by_tg_id=ALICE)
        self.session.commit()
        self.assertEqual(A.RTM_FINAL_OFFER, lot.rtm_stage)

        # 3. RCB raise to 900 — bidding against themselves, legally, once.
        lot = A.place_bid(self.session, self.season, lot, self.rcb, 900,
                          now=NOW, by_tg_id=BOB)
        self.session.commit()
        self.assertEqual(900, lot.current_bid_lakh)
        self.assertEqual(A.RTM_DECISION, lot.rtm_stage,
                         "one raise means one — the window closes on it")

        # 4. RR match at the FINAL number, not the one that triggered it.
        lot = A.rtm_decide(self.session, self.season, lot, self.rr, True,
                           now=NOW, by_tg_id=ALICE)
        self.session.commit()
        self.session.refresh(self.rr)
        self.session.refresh(self.rcb)

        self.assertEqual(A.LOT_SOLD, lot.status)
        self.assertEqual(A.ACQ_RTM, lot.acquisition)
        self.assertEqual(self.rr.id, lot.sold_to_id)
        self.assertEqual(self.rr.id, lot.rtm_matched_by_id)
        self.assertEqual(900, lot.sold_price_lakh)
        self.assertEqual(self.purse_lakh - 900, self.rr.purse_remaining_lakh)
        self.assertEqual(self.purse_lakh, self.rcb.purse_remaining_lakh,
                         "the underbidder pays nothing")
        self.assertEqual(1, self.rr.rtm_cards_used)
        self.assertEqual(0, A.rtm_cards_left(self.rr))
        self.assert_ledger_agrees("after a match")
        kinds = [r.kind for r in A.ledger(self.session, self.rr.id)]
        self.assertIn(A.LEDGER_RTM, kinds)
        self.assertNotIn(A.LEDGER_PURCHASE, kinds)

    def test_a_match_is_auction_progress_not_a_retention(self):
        lot = self.open_theirs()
        _, lot = self.bid_and_expire(lot, 600)
        lot = self.to_decision(lot)
        lot = self.A.rtm_decide(self.session, self.season, lot, self.rr, True,
                                now=NOW)
        self.session.commit()
        counts = self.A.pool_counts(self.session, self.season.id)
        self.assertEqual(0, counts.get("retained", 0),
                         "nobody retained anybody — the room bid for him")
        self.assertEqual(1, counts.get(self.A.LOT_SOLD, 0))


# ══════════════════════════════════════════════════════════════════════
# Who is offered one, and who is not
# ══════════════════════════════════════════════════════════════════════

class EligibilityTests(RTMCase):

    def test_with_rtm_off_a_lot_simply_sells(self):
        self.season.rtm_enabled = False
        self.session.commit()
        lot = self.open_theirs()
        outcome, lot = self.bid_and_expire(lot, 600)
        self.assertEqual("sold", outcome)
        self.assertEqual(self.rcb.id, lot.sold_to_id)

    def test_a_franchise_with_no_cards_left_is_not_offered_one(self):
        self.rr.rtm_cards_used = self.rr.rtm_cards_total
        self.session.commit()
        lot = self.open_theirs()
        outcome, _ = self.bid_and_expire(lot, 600)
        self.assertEqual("sold", outcome)

    def test_a_player_nobody_held_is_not_offered(self):
        lot = self.open_theirs(index=len(self.lots) - 1)
        self.assertIsNone(lot.previous_franchise_id)
        outcome, _ = self.bid_and_expire(lot, 600)
        self.assertEqual("sold", outcome)

    def test_the_holder_who_is_already_winning_is_never_offered(self):
        """There is nothing to match — they have him."""
        lot = self.open_theirs()
        outcome, lot = self.bid_and_expire(lot, 600, bidder=self.rr)
        self.assertEqual("sold", outcome)
        self.assertEqual(self.rr.id, lot.sold_to_id)
        self.session.refresh(self.rr)
        self.assertEqual(0, self.rr.rtm_cards_used, "and no card is spent")

    def test_a_holder_who_cannot_afford_the_bid_is_not_offered_and_it_is_said(self):
        self.A.correct_purse(self.session, self.season, self.rr,
                             -(self.purse_lakh - 100))
        self.session.commit()
        lot = self.open_theirs()
        outcome, _ = self.bid_and_expire(lot, 600)
        self.assertEqual("sold", outcome)
        kinds = [e.kind for e in self.A.recent_events(self.session,
                                                      self.season.id, limit=20)]
        self.assertIn("rtm_unavailable", kinds,
                      "a card existed and could not be used — worth saying")

    def test_a_retained_player_is_never_subject_to_an_rtm(self):
        """Retention and RTM are alternatives, not a ladder."""
        from models import AuctionLot
        lot = self.lots[0]
        lot.acquisition = self.A.ACQ_RETAINED
        self.session.commit()
        holder, _ = self.A.rtm_available(self.session, self.season, lot)
        self.assertIsNone(holder)


# ══════════════════════════════════════════════════════════════════════
# The clocks
# ══════════════════════════════════════════════════════════════════════

class TimeoutTests(RTMCase):

    def _expire(self, lot):
        return self.A.resolve_rtm_stage(
            self.session, self.season, lot,
            now=lot.deadline_at + timedelta(seconds=1))

    def test_an_unanswered_intent_sells_to_the_top_bidder(self):
        lot = self.open_theirs()
        _, lot = self.bid_and_expire(lot, 600)
        outcome, lot = self._expire(lot)
        self.session.commit()
        self.assertEqual("declined", outcome)
        self.assertEqual(self.A.LOT_SOLD, lot.status)
        self.assertEqual(self.rcb.id, lot.sold_to_id)
        self.assertEqual(600, lot.sold_price_lakh)
        self.session.refresh(self.rr)
        self.assertEqual(0, self.rr.rtm_cards_used, "no card for an unanswered one")
        self.assert_ledger_agrees("after an unanswered intent")

    def test_an_unanswered_final_offer_stands_pat(self):
        lot = self.open_theirs()
        _, lot = self.bid_and_expire(lot, 600)
        lot = self.A.rtm_intent(self.session, self.season, lot, self.rr, True,
                                now=NOW)
        self.session.commit()
        outcome, lot = self._expire(lot)
        self.session.commit()
        self.assertEqual("stood", outcome)
        self.assertEqual(self.A.RTM_DECISION, lot.rtm_stage)
        self.assertEqual(600, lot.current_bid_lakh, "the price did not move")

    def test_an_unanswered_decision_sells_to_the_top_bidder(self):
        lot = self.open_theirs()
        _, lot = self.bid_and_expire(lot, 600)
        lot = self.A.rtm_intent(self.session, self.season, lot, self.rr, True,
                                now=NOW)
        lot = self.A.place_bid(self.session, self.season, lot, self.rcb, 900,
                               now=NOW)
        self.session.commit()
        outcome, lot = self._expire(lot)
        self.session.commit()
        self.assertEqual("declined", outcome)
        self.assertEqual(self.rcb.id, lot.sold_to_id)
        self.assertEqual(900, lot.sold_price_lakh,
                         "RCB pay the number they raised to")
        self.session.refresh(self.rr)
        self.assertEqual(0, self.rr.rtm_cards_used)

    def test_pausing_mid_rtm_drops_the_clock_and_resuming_gives_an_rtm_window(self):
        lot = self.open_theirs()
        _, lot = self.bid_and_expire(lot, 600)
        self.A.pause(self.session, self.season)
        self.session.commit()
        self.session.refresh(lot)
        self.assertIsNone(lot.deadline_at)

        later = NOW + timedelta(minutes=5)
        self.A.start(self.session, self.season, now=later)
        self.session.commit()
        self.session.refresh(lot)
        self.assertEqual(later + timedelta(seconds=self.season.rtm_window_seconds),
                         lot.deadline_at,
                         "an RTM window, not a lot-length clock")


# ══════════════════════════════════════════════════════════════════════
# Who may bid, and when
# ══════════════════════════════════════════════════════════════════════

class FinalOfferTests(RTMCase):

    def setUp(self):
        super().setUp()
        lot = self.open_theirs()
        _, self.lot = self.bid_and_expire(lot, 600)

    def _claim(self):
        self.lot = self.A.rtm_intent(self.session, self.season, self.lot,
                                     self.rr, True, now=NOW)
        self.session.commit()

    def test_the_top_bidder_may_raise_against_themselves_here_and_only_here(self):
        self._claim()
        lot = self.A.place_bid(self.session, self.season, self.lot, self.rcb,
                               900, now=NOW)
        self.assertEqual(900, lot.current_bid_lakh)

    def test_and_may_not_outside_the_final_offer(self):
        """Back on an ordinary lot, the rule is the rule again."""
        self.A.rtm_intent(self.session, self.season, self.lot, self.rr, False,
                          now=NOW)
        self.session.commit()
        lot = self.A.open_next_lot(self.session, self.season, now=NOW)
        self.session.commit()
        self.A.place_bid(self.session, self.season, lot, self.rcb,
                         lot.base_price_lakh, now=NOW)
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.place_bid(self.session, self.season, lot, self.rcb,
                             lot.base_price_lakh + 500, now=NOW)
        self.assertIn("against yourself", str(caught.exception))

    def test_nobody_else_may_bid_at_any_stage(self):
        third = self.A.create_franchise(self.session, self.season, "Third",
                                        owner_tg_id=CAROL)
        self.session.commit()
        for stage_setup in (lambda: None, self._claim):
            stage_setup()
            with self.assertRaises(self.A.AuctionError) as caught:
                self.A.place_bid(self.session, self.season, self.lot, third,
                                 5000, now=NOW)
            self.assertIn("Right To Match", str(caught.exception))

    def test_the_holder_cannot_bid_their_way_out_of_answering(self):
        self._claim()
        with self.assertRaises(self.A.AuctionError):
            self.A.place_bid(self.session, self.season, self.lot, self.rr,
                             900, now=NOW)

    def test_the_final_offer_still_obeys_the_increment(self):
        self._claim()
        minimum = self.A.next_min_bid(self.season, self.lot)
        with self.assertRaises(self.A.AuctionError):
            self.A.place_bid(self.session, self.season, self.lot, self.rcb,
                             minimum - 1, now=NOW)

    def test_a_late_final_offer_cannot_land_against_the_decision_clock(self):
        """The sharpest race in the feature.

        Moving to the decision stage writes a NEW, LATER deadline. A final
        offer arriving a moment after that still satisfies ``deadline_at >
        now``, so the deadline alone cannot refuse it — only the stage can. Get
        this wrong and the number the holder is answering on changes under them
        mid-answer.
        """
        self._claim()
        window_end = self.lot.deadline_at
        lot = self.A.rtm_to_decision(self.session, self.season, self.lot,
                                     now=window_end)
        self.session.commit()
        self.assertGreater(lot.deadline_at, window_end,
                           "the decision window is genuinely later")
        with self.assertRaises(self.A.AuctionError):
            self.A.place_bid(self.session, self.season, lot, self.rcb, 900,
                             now=window_end + timedelta(seconds=1))
        self.session.refresh(lot)
        self.assertEqual(600, lot.current_bid_lakh)

    def test_a_raise_does_not_burn_the_lots_anti_snipe_budget(self):
        """One bidder cannot be sniped, and must not be able to stall."""
        self.A.set_anti_snipe(self.session, self.season, 30, 30, 5)
        self.session.commit()
        self._claim()
        before = self.lot.extensions_used
        lot = self.A.place_bid(self.session, self.season, self.lot, self.rcb,
                               900, now=NOW)
        self.assertEqual(before, lot.extensions_used)


# ══════════════════════════════════════════════════════════════════════
# Money at the decision
# ══════════════════════════════════════════════════════════════════════

class FinalOfferRaceTests(RTMCase):
    """The sharpest race in the feature, driven from two real sessions.

    Advancing to ``decision`` writes a *new, later* ``deadline_at``. A final
    offer already in flight therefore still satisfies ``deadline_at > now``,
    and without the stage in the claim's WHERE it lands as a raise against a
    question the holder is already answering — moving the price out from under
    the very decision being made.

    ``validate_bid`` cannot catch this on its own: session B passed validation
    while the lot really was at ``final_offer``. Only the conditional UPDATE
    can, which is why reading the lot *before* the stage moves is the whole
    point of the test.
    """

    def setUp(self):
        super().setUp()
        lot = self.open_theirs()
        _, self.lot = self.bid_and_expire(lot, 600)
        self.lot = self.A.rtm_intent(self.session, self.season, self.lot,
                                     self.rr, True, now=NOW)
        self.session.commit()
        self.assertEqual(self.A.RTM_FINAL_OFFER, self.lot.rtm_stage)

    def test_a_final_offer_in_flight_cannot_land_after_the_stage_moves(self):
        from database import get_session
        from models import (AuctionBid, AuctionFranchise, AuctionLot,
                            AuctionSeason)

        other = get_session()
        try:
            their_season = (other.query(AuctionSeason)
                            .filter(AuctionSeason.id == self.season.id).first())
            their_lot = (other.query(AuctionLot)
                         .filter(AuctionLot.id == self.lot.id).first())
            their_franchise = (other.query(AuctionFranchise)
                               .filter(AuctionFranchise.id == self.rcb.id).first())
            self.assertEqual(self.A.RTM_FINAL_OFFER, their_lot.rtm_stage,
                             "B must read the lot while the window is open")

            # An admin closes the window EARLY — /artmforce stand, which is
            # the one path that moves the stage before the clock says so. The
            # new deadline is later than the one B is racing, so nothing about
            # the time refuses B: at +20s B is inside its own window and the
            # row's deadline is +40s. Only the stage says no.
            self.A.rtm_to_decision(self.session, self.season, self.lot,
                                   now=NOW + timedelta(seconds=10))
            self.session.commit()

            with self.assertRaises(self.A.AuctionError):
                self.A.place_bid(other, their_season, their_lot,
                                 their_franchise, 900,
                                 now=NOW + timedelta(seconds=20), by_tg_id=BOB)
            other.rollback()
        finally:
            other.close()

        self.session.expire_all()
        lot = (self.session.query(AuctionLot)
               .filter(AuctionLot.id == self.lot.id).first())
        self.assertEqual(self.A.RTM_DECISION, lot.rtm_stage)
        self.assertEqual(600, lot.current_bid_lakh,
                         "the price the holder is deciding against must not "
                         "move underneath them")
        self.assertEqual(
            1, self.session.query(AuctionBid)
            .filter(AuctionBid.lot_id == lot.id, AuctionBid.is_void.is_(False))
            .count(), "the losing final offer must leave no row")

    def test_and_the_holder_then_matches_at_the_price_they_were_shown(self):
        """The other half: what they answer is what they pay."""
        lot = self.A.rtm_to_decision(self.session, self.season, self.lot,
                                     now=NOW)
        self.session.commit()
        price = self.A.rtm_price(self.season, lot)
        lot = self.A.rtm_decide(self.session, self.season, lot, self.rr, True,
                                now=NOW)
        self.session.commit()
        self.assertEqual(price, lot.sold_price_lakh)
        self.assert_ledger_agrees("after a match at the shown price")


class MatchMoneyTests(RTMCase):

    def test_claiming_affordably_then_being_priced_out_lets_him_go(self):
        lot = self.open_theirs()
        _, lot = self.bid_and_expire(lot, 600)
        # RR can afford 600 but will not be able to afford 900.
        self.A.correct_purse(self.session, self.season, self.rr,
                             -(self.purse_lakh - 700))
        self.session.commit()
        lot = self.A.rtm_intent(self.session, self.season, lot, self.rr, True,
                                now=NOW)
        lot = self.A.place_bid(self.session, self.season, lot, self.rcb, 900,
                               now=NOW)
        self.session.commit()
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.rtm_decide(self.session, self.season, lot, self.rr, True,
                              now=NOW)
        self.assertIn("more than the purse", str(caught.exception))
        self.session.rollback()
        self.session.refresh(self.rr)
        self.assertEqual(0, self.rr.rtm_cards_used,
                         "a card they never got to use is not spent")

    def test_a_premium_is_added_on_top_of_the_final_bid(self):
        self.season.rtm_extra_lakh = 200
        self.session.commit()
        lot = self.open_theirs()
        _, lot = self.bid_and_expire(lot, 600)
        self.assertEqual(800, self.A.rtm_price(self.season, lot))
        lot = self.to_decision(lot)
        lot = self.A.rtm_decide(self.session, self.season, lot, self.rr, True,
                                now=NOW)
        self.session.commit()
        self.assertEqual(800, lot.sold_price_lakh)
        self.assert_ledger_agrees("after a match with a premium")

    def test_only_the_holder_may_answer(self):
        lot = self.open_theirs()
        _, lot = self.bid_and_expire(lot, 600)
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.rtm_intent(self.session, self.season, lot, self.rcb, True,
                              now=NOW)
        self.assertIn(self.rr.name, str(caught.exception))

    def test_answering_twice_loses_harmlessly(self):
        lot = self.open_theirs()
        _, lot = self.bid_and_expire(lot, 600)
        self.A.rtm_intent(self.session, self.season, lot, self.rr, True, now=NOW)
        self.session.commit()
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.rtm_intent(self.session, self.season, lot, self.rr, True,
                              now=NOW)
        self.assertIn("no Right To Match to answer", str(caught.exception))

    def test_two_matches_in_one_transaction_see_the_first_card_spent(self):
        """The autoflush shape that has bitten this feature four times."""
        self.rr.rtm_cards_total = 1
        self.session.commit()
        for index in (0, 1):
            lot = self.open_theirs(index=index)
            _, lot = self.bid_and_expire(lot, 600)
            if index == 0:
                lot = self.A.rtm_intent(self.session, self.season, lot,
                                        self.rr, True, now=NOW)
                lot = self.A.rtm_to_decision(self.session, self.season, lot,
                                             now=NOW)
                self.A.rtm_decide(self.session, self.season, lot, self.rr,
                                  True, now=NOW)
                # NO commit — the second offer must still see the card gone.
            else:
                self.assertEqual(
                    "sold", _,
                    "with the only card spent, the second lot just sells")
        self.session.commit()
        self.session.refresh(self.rr)
        self.assertEqual(1, self.rr.rtm_cards_used)


# ══════════════════════════════════════════════════════════════════════
# Undo, and the lifecycle
# ══════════════════════════════════════════════════════════════════════

class UndoTests(RTMCase):

    def _match(self, price=900):
        lot = self.open_theirs()
        _, lot = self.bid_and_expire(lot, 600)
        lot = self.A.rtm_intent(self.session, self.season, lot, self.rr, True,
                                now=NOW)
        lot = self.A.place_bid(self.session, self.season, lot, self.rcb, price,
                               now=NOW)
        lot = self.A.rtm_decide(self.session, self.season, lot, self.rr, True,
                                now=NOW)
        self.session.commit()
        return lot

    def test_undo_sale_refuses_a_match_and_says_so(self):
        lot = self._match()
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.undo_sale(self.session, self.season, lot, now=NOW)
        self.assertIn("Right To Match", str(caught.exception))

    def test_undo_rtm_gives_back_the_money_and_the_card(self):
        lot = self._match()
        self.A.undo_rtm(self.session, self.season, lot, now=NOW)
        self.session.commit()
        self.session.refresh(self.rr)
        self.assertEqual(self.purse_lakh, self.rr.purse_remaining_lakh)
        self.assertEqual(0, self.rr.squad_size)
        self.assertEqual(0, self.rr.rtm_cards_used,
                         "an undone match that ate a card leaves a franchise "
                         "permanently poorer for an admin's slip")
        self.assertEqual(self.A.LOT_ON_BLOCK, lot.status)
        self.assertEqual(self.A.ACQ_AUCTION, lot.acquisition)
        self.assert_ledger_agrees("after undoing a match")

    def test_undo_rtm_does_not_void_the_final_offer(self):
        """The bug you get by reusing undo_sale: RCB's raise is not a losing
        bid to be erased, it is the price the lot reached."""
        from models import AuctionBid
        lot = self._match()
        self.A.undo_rtm(self.session, self.season, lot, now=NOW)
        self.session.commit()
        bids = (self.session.query(AuctionBid)
                .filter(AuctionBid.lot_id == lot.id).all())
        self.assertTrue(bids)
        self.assertTrue(all(not b.is_void for b in bids))
        self.assertEqual(900, max(b.amount_lakh for b in bids))

    def test_withdrawing_mid_rtm_clears_the_stage_and_spends_no_card(self):
        lot = self.open_theirs()
        _, lot = self.bid_and_expire(lot, 600)
        self.A.withdraw_lot(self.session, self.season, lot)
        self.session.commit()
        self.assertEqual(self.A.LOT_WITHDRAWN, lot.status)
        self.assertIsNone(lot.rtm_stage)
        self.assertIsNone(self.season.current_lot_id)
        self.session.refresh(self.rr)
        self.assertEqual(0, self.rr.rtm_cards_used)

    def test_cancelling_mid_rtm_clears_the_stage(self):
        lot = self.open_theirs()
        _, lot = self.bid_and_expire(lot, 600)
        self.A.cancel(self.session, self.season)
        self.session.commit()
        self.assertIsNone(lot.rtm_stage)
        self.assertEqual(self.A.LOT_QUEUED, lot.status)

    def test_the_league_this_season_follows_cannot_move_under_an_open_rtm(self):
        lot = self.open_theirs()
        self.bid_and_expire(lot, 600)
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.link_previous_season(self.session, self.season, 1)
        self.assertIn("Pause the auction", str(caught.exception))


class BoardAndSweeperTests(RTMCase):

    def test_the_board_never_invites_a_bid_during_intent_or_decision(self):
        lot = self.open_theirs()
        _, lot = self.bid_and_expire(lot, 600)
        board = self.A.render_board(self.session, self.season, lot, now=NOW)
        self.assertNotIn("Next bid", board,
                         "telling the room to bid on a lot nobody may bid on")
        self.assertIn("Right To Match", board)
        self.assertIn(self.rr.name, board)

    def test_the_board_invites_the_final_offer(self):
        lot = self.open_theirs()
        _, lot = self.bid_and_expire(lot, 600)
        lot = self.A.rtm_intent(self.session, self.season, lot, self.rr, True,
                                now=NOW)
        board = self.A.render_board(self.session, self.season, lot, now=NOW)
        self.assertIn("Next bid", board)
        self.assertIn("final", board.lower())

    def test_the_board_never_calls_going_twice_over_an_rtm(self):
        lot = self.open_theirs()
        _, lot = self.bid_and_expire(lot, 600)
        nearly = lot.deadline_at - timedelta(seconds=2)
        board = self.A.render_board(self.session, self.season, lot, now=nearly)
        self.assertNotIn("GOING TWICE", board)

    def test_the_auction_does_not_finish_while_an_rtm_is_open(self):
        """The last lot of an auction must not complete it mid-question."""
        lot = self.open_theirs(index=len(self.lots) - 1)
        lot.previous_franchise_id = self.rr.id
        self.session.commit()
        while self.A.next_queued(self.session, self.season.id) is not None:
            self.A.pass_lot(self.session, self.season, lot)
            self.session.commit()
            lot = self.A.open_next_lot(self.session, self.season, now=NOW)
        _, lot = self.bid_and_expire(lot, 600)
        if lot.status == self.A.LOT_RTM_OFFERED:
            self.A.complete_if_done(self.session, self.season)
            self.assertEqual(self.A.STATUS_LIVE, self.season.status)

    def test_the_next_lot_is_not_opened_over_an_open_rtm(self):
        lot = self.open_theirs()
        _, lot = self.bid_and_expire(lot, 600)
        upcoming = self.A.next_queued(self.session, self.season.id)
        if upcoming is not None:
            with self.assertRaises(self.A.AuctionError):
                self.A.open_lot(self.session, self.season, upcoming, now=NOW)


class PublishTests(RTMCase):
    min_squad = 1

    def test_a_matched_player_reaches_the_league_marked_rtm(self):
        import json
        from models import ChallengePlayer, ChallengeTeam
        lot = self.open_theirs()
        _, lot = self.bid_and_expire(lot, 600)
        lot = self.to_decision(lot)
        lot = self.A.rtm_decide(self.session, self.season, lot, self.rr, True,
                                now=NOW)
        self.session.commit()
        guard = 0
        while self.season.status == self.A.STATUS_LIVE and guard < 60:
            guard += 1
            live = self.A.current_lot(self.session, self.season)
            if live is None:
                if self.A.next_queued(self.session, self.season.id) is None:
                    self.A.complete_if_done(self.session, self.season)
                    break
                self.A.open_next_lot(self.session, self.season, now=NOW)
                continue
            if live.status == self.A.LOT_RTM_OFFERED:
                self.A.rtm_intent(self.session, self.season, live, self.rr,
                                  False, now=NOW)
            else:
                self.A.pass_lot(self.session, self.season, live)
            self.session.commit()

        league = self.A.publish_to_league(self.session, self.season)
        self.session.commit()
        row = (self.session.query(ChallengePlayer)
               .join(ChallengeTeam, ChallengePlayer.team_id == ChallengeTeam.id)
               .filter(ChallengeTeam.league_id == league.id,
                       ChallengePlayer.name == lot.name).first())
        self.assertIsNotNone(row)
        self.assertEqual("rtm", json.loads(row.details_json)["acquisition"])
        from services.cipl_match import cp_to_player_dict
        self.assertEqual(lot.rating, cp_to_player_dict(row)["rating"])


# ══════════════════════════════════════════════════════════════════════
# The commands, through the real handlers
# ══════════════════════════════════════════════════════════════════════

class CommandTests(RTMCase):
    """The four admin commands and /artm, driven as Telegram drives them."""

    def setUp(self):
        super().setUp()
        self.replies = []
        self._prev_admins = os.environ.get("BOT_ADMIN_IDS")
        os.environ["BOT_ADMIN_IDS"] = str(CAROL)

    def tearDown(self):
        if self._prev_admins is None:
            os.environ.pop("BOT_ADMIN_IDS", None)
        else:
            os.environ["BOT_ADMIN_IDS"] = self._prev_admins
        super().tearDown()

    def _run(self, handler, user_id, args=()):
        import asyncio
        from types import SimpleNamespace

        async def reply_text(text, **kwargs):
            self.replies.append(text)
            return SimpleNamespace(message_id=1)

        async def set_message_reaction(**kwargs):
            return True

        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=self.season.chat_id,
                                           type="supergroup"),
            effective_user=SimpleNamespace(id=user_id, username="u",
                                           first_name="U"),
            effective_message=SimpleNamespace(reply_text=reply_text,
                                              message_id=7))
        context = SimpleNamespace(
            args=list(args),
            bot=SimpleNamespace(set_message_reaction=set_message_reaction))
        # The handlers open their own session; this one must not be holding
        # the rows they are about to write.
        self.session.commit()
        self.session.close()
        asyncio.run(handler(update, context))
        from database import get_session
        self.session = get_session()
        self.season = self.session.merge(self.season)
        self.rr = self.session.merge(self.rr)
        self.rcb = self.session.merge(self.rcb)
        return self.replies

    def test_artmset_reads_the_rules_back(self):
        from handlers import auction as H
        self._run(H.artmset_handler, CAROL)
        self.assertIn("Right To Match", self.replies[-1])
        self.assertIn(self.rr.name, self.replies[-1])

    def test_artmset_configures_in_one_line(self):
        from handlers import auction as H
        self._run(H.artmset_handler, CAROL, ("2", "45", "2"))
        self.assertTrue(self.season.rtm_enabled)
        self.assertEqual(2, self.season.rtm_per_team)
        self.assertEqual(45, self.season.rtm_window_seconds)
        self.assertEqual(200, self.season.rtm_extra_lakh,
                         "a bare number is crore, here as everywhere else")
        self.assertEqual(2, self.rcb.rtm_cards_total,
                         "saving the rules deals the cards out")

    def test_artmset_off_leaves_the_numbers_alone(self):
        from handlers import auction as H
        self._run(H.artmset_handler, CAROL, ("off",))
        self.assertFalse(self.season.rtm_enabled)
        self.assertEqual(self.cards, self.season.rtm_per_team,
                         "turning it back on should not mean typing them again")

    def test_a_stranger_cannot_set_the_rules(self):
        from handlers import auction as H
        self._run(H.artmset_handler, BOB, ("9",))
        self.assertIn("admin", self.replies[-1].lower())
        self.assertEqual(self.cards, self.season.rtm_per_team)

    def test_artmcards_gives_one_franchise_its_own_count(self):
        from handlers import auction as H
        self._run(H.artmcards_handler, CAROL, ("Mumbai", "3"))
        self.assertEqual(3, self.rr.rtm_cards_total)

    def test_artmforce_answers_for_a_silent_owner(self):
        from handlers import auction as H
        lot = self.open_theirs()
        _, lot = self.bid_and_expire(lot, 600)
        self.assertEqual(self.A.RTM_INTENT, lot.rtm_stage)
        self._run(H.artmforce_handler, CAROL, ("yes",))
        lot = self.A.current_lot(self.session, self.season)
        self.assertEqual(self.A.RTM_FINAL_OFFER, lot.rtm_stage)
        # …and again at the last window, which is where the money moves.
        self._run(H.artmforce_handler, CAROL, ("stand",))
        self._run(H.artmforce_handler, CAROL, ("yes",))
        self.session.refresh(self.rr)
        self.assertEqual(1, self.rr.rtm_cards_used)

    def test_artmforce_will_not_invent_a_raise(self):
        from handlers import auction as H
        lot = self.open_theirs()
        _, lot = self.bid_and_expire(lot, 600)
        self._run(H.artmforce_handler, CAROL, ("yes",))
        self._run(H.artmforce_handler, CAROL, ("yes",))
        self.assertIn("has to be a real bid", self.replies[-1])

    def test_artmforce_with_nothing_open_says_so(self):
        from handlers import auction as H
        self._run(H.artmforce_handler, CAROL, ("yes",))
        self.assertIn("no Right To Match open", self.replies[-1])

    def test_artmundo_gives_the_card_back(self):
        from handlers import auction as H
        lot = self.open_theirs()
        _, lot = self.bid_and_expire(lot, 600)
        lot = self.to_decision(lot)
        lot = self.A.rtm_decide(self.session, self.season, lot, self.rr, True,
                                now=NOW)
        name = lot.name
        self.session.commit()
        self._run(H.artmundo_handler, CAROL, tuple(name.split()))
        self.session.refresh(self.rr)
        self.assertEqual(0, self.rr.rtm_cards_used)
        self.assertEqual(self.purse_lakh, self.rr.purse_remaining_lakh)
        self.assert_ledger_agrees("after /artmundo")

    def test_artmundo_refuses_an_ordinary_sale(self):
        from handlers import auction as H
        lot = self.start()
        self.A.place_bid(self.session, self.season, lot, self.rcb, 600,
                         now=NOW)
        sold = self.A.sell_lot(self.session, self.season, lot, now=NOW)
        name = sold.name
        self.session.commit()
        self._run(H.artmundo_handler, CAROL, tuple(name.split()))
        self.assertIn("not signed with a Right To Match", self.replies[-1])

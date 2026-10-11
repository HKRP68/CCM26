"""IPL-style trades between auction franchises — every window, and the money.

What is pinned here, and why:

  • **The contract price travels with the player.** The franchise that takes a
    player pays his auction price, the one that lets him go is paid it back,
    and cash moves on top. The ledger has to agree with the purse after every
    step — the invariant the rest of the auction stands on.
  • **Four windows.** Pre-auction (retained players), mid-auction (never under
    a live bid), post-auction, and mid-season (closed by the deadline and by
    the playoffs). A traded player keeps his tournament stats.
  • **Only the bot owner / a bot admin approves.** An auction admin or an
    owner pressing Approve is refused.
  • **Every squad rule still holds** — relative, so a squad already short may
    trade, it just may not get worse.
"""

import itertools
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

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
                 "services.auction_service", "services.retention_negotiation",
                 "services.auction_scheduler", "services.auction_trade_service",
                 "handlers.auction", "handlers.auction_trade")

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

ALICE, BOB, CAROL, BOSS = 111, 222, 333, 999
NOW = datetime(2026, 3, 1, 12, 0, 0)


class TradeCase(unittest.TestCase):
    min_squad = 1
    max_squad = 6
    max_overseas = 8
    purse_lakh = 10_000

    def setUp(self):
        from database import get_session
        from models import Player
        from services import auction_service as A
        from services import auction_trade_service as T

        self.session = get_session()
        self.A, self.T = A, T
        tag = next(_PID)
        self.players = []
        for name, rating, category, country in CATALOGUE:
            player = Player(name=f"{name} {tag}", rating=rating,
                            category=category, country=country,
                            version="Base", bat_hand="Right",
                            bowl_hand="Right", bowl_style="Medium Pacer",
                            bat_rating=rating, bowl_rating=max(0, rating - 10),
                            is_active=True)
            self.session.add(player)
            self.players.append(player)
        self.session.flush()
        self.season = A.create_season(
            self.session, f"Trade Season {tag}", min_squad_size=self.min_squad,
            max_squad_size=self.max_squad, max_overseas=self.max_overseas,
            opening_purse_lakh=self.purse_lakh, bid_gap_seconds=0)
        A.bind_chat(self.session, self.season, -5000 - tag)
        self.mi = A.create_franchise(self.session, self.season, "Mumbai",
                                     owner_tg_id=ALICE, owner_name="Alice")
        self.csk = A.create_franchise(self.session, self.season, "Chennai",
                                      owner_tg_id=BOB, owner_name="Bob")
        self.session.commit()
        # Only BOSS is the bot admin in these tests.
        self._patch = mock.patch.object(T, "is_bot_admin",
                                        lambda tg_id: tg_id == BOSS)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.session.rollback()
        self.session.close()

    # ── helpers ──

    def build_pool(self):
        self.A.add_players_to_pool(self.session, self.season, self.players)
        self.session.commit()

    def lot_named(self, prefix):
        from models import AuctionLot
        return (self.session.query(AuctionLot)
                .filter(AuctionLot.season_id == self.season.id,
                        AuctionLot.name.like(prefix + "%")).one())

    def run_auction(self, buyers, prices=None):
        """Sell the pool in lot order; ``buyers`` is a list of franchises."""
        A = self.A
        lot = A.start(self.session, self.season, now=NOW)
        self.session.commit()
        index = 0
        while lot is not None:
            team = buyers[index % len(buyers)]
            price = (prices or {}).get(index) or A.next_min_bid(self.season, lot)
            A.place_bid(self.session, self.season, lot, team, price, now=NOW)
            A.sell_lot(self.session, self.season, lot, now=NOW)
            self.session.commit()
            index += 1
            if self.season.status != A.STATUS_LIVE:
                break
            lot = A.open_next_lot(self.session, self.season, now=NOW)
            self.session.commit()

    def assert_ledger_agrees(self):
        for team in self.A.franchises(self.session, self.season.id):
            self.session.refresh(team)
            self.assertEqual(self.A.ledger_total(self.session, team.id),
                             int(team.purse_remaining_lakh or 0),
                             f"{team.name}'s ledger and purse disagree")
            self.assertEqual(len(self.A.squad(self.session, team.id)),
                             int(team.squad_size or 0),
                             f"{team.name}'s squad cache is wrong")

    def offer(self, a, b, a_lots=(), b_lots=(), cash=0, now=None):
        T = self.T
        trade = T.open_trade(self.session, self.season, a, b, by_tg_id=ALICE,
                             now=now)
        try:
            for lot in list(a_lots) + list(b_lots):
                T.toggle_lot(self.session, trade, lot.id)
            if cash:
                T.set_cash(self.session, trade, cash)
            T.send_offer(self.session, trade, by_tg_id=ALICE, now=now)
        except Exception:
            # A refused send leaves the builder open for the owner to fix;
            # a test moving on to its next offer calls it off instead.
            T.cancel(self.session, trade)
            self.session.commit()
            raise
        self.session.commit()
        return trade

    def make_auction_admin(self, tg_id):
        if not self.A.is_auction_admin(self.session, tg_id):
            self.A.add_auction_admin(self.session, tg_id)
            self.session.commit()

    def set_purse(self, team, lakh):
        """Move a purse honestly, through a ledgered correction."""
        delta = lakh - int(team.purse_remaining_lakh or 0)
        if delta:
            self.A.correct_purse(self.session, self.season, team, delta)
        self.session.commit()

    def complete(self, trade, now=None):
        T = self.T
        T.accept(self.session, trade, by_tg_id=BOB, now=now)
        if trade.status == T.STATUS_PENDING:
            T.approve(self.session, trade, by_tg_id=BOSS, now=now)
        self.session.commit()
        return trade


class PostAuctionMoney(TradeCase):
    def setUp(self):
        super().setUp()
        self.build_pool()
        # Mumbai buys the even lots, Chennai the odd ones.
        self.run_auction([self.mi, self.csk], prices={0: 1200, 1: 900})
        self.assertEqual(self.season.status, self.A.STATUS_COMPLETED)

    def test_phase_is_post_auction(self):
        self.assertEqual(self.T.trade_phase(self.session, self.season)[0],
                         self.T.PHASE_POST)

    def test_a_swap_settles_the_price_difference(self):
        kohli = self.lot_named("Virat")      # Mumbai, ₹12 Cr
        bumrah = self.lot_named("Jasprit")   # Chennai, ₹9 Cr
        mi0, csk0 = self.mi.purse_remaining_lakh, self.csk.purse_remaining_lakh
        trade = self.complete(self.offer(self.mi, self.csk, [kohli], [bumrah]))
        self.assertEqual(trade.status, self.T.STATUS_COMPLETED)
        self.session.refresh(kohli)
        self.session.refresh(bumrah)
        self.assertEqual(kohli.sold_to_id, self.csk.id)
        self.assertEqual(bumrah.sold_to_id, self.mi.id)
        # Mumbai let a ₹12 Cr player go and took a ₹9 Cr one: ₹3 Cr better off.
        self.assertEqual(self.mi.purse_remaining_lakh, mi0 + 300)
        self.assertEqual(self.csk.purse_remaining_lakh, csk0 - 300)
        self.assert_ledger_agrees()

    def test_all_cash_deal_and_a_sweetener(self):
        rinku = self.lot_named("Rinku")
        owner = rinku.sold_to_id
        buyer = self.mi if owner == self.csk.id else self.csk
        seller = self.csk if buyer is self.mi else self.mi
        buyer0 = buyer.purse_remaining_lakh
        seller0 = seller.purse_remaining_lakh
        price = rinku.sold_price_lakh
        # Buyer takes Rinku outright and pays ₹1 Cr on top.
        trade = self.offer(buyer, seller, [], [rinku], cash=100)
        self.complete(trade)
        self.assertEqual(buyer.purse_remaining_lakh, buyer0 - price - 100)
        self.assertEqual(seller.purse_remaining_lakh, seller0 + price + 100)
        self.assertEqual(self.mi.squad_size + self.csk.squad_size, 8)
        self.assert_ledger_agrees()

    def test_a_trade_the_buyer_cannot_afford_is_refused(self):
        kohli = self.lot_named("Virat")
        self.set_purse(self.csk, 100)
        with self.assertRaises(self.A.AuctionError):
            self.offer(self.csk, self.mi, [], [kohli])

    def test_squad_maximum_and_overseas_cap(self):
        self.season.max_squad_size = 4
        self.session.commit()
        kohli = self.lot_named("Virat")  # Mumbai has 4 → Chennai has 4
        with self.assertRaises(self.A.AuctionError) as err:
            self.offer(self.csk, self.mi, [], [kohli])
        self.assertIn("squad limit", str(err.exception))

        self.season.max_squad_size = 6
        self.season.max_overseas = 1
        self.session.commit()
        # Chennai already hold one overseas player (lot 3/odd); taking another
        # pushes them over the cap of 1.
        overseas_mi = [l for l in self.A.squad(self.session, self.mi.id)
                       if l.is_overseas]
        home_csk = [l for l in self.A.squad(self.session, self.csk.id)
                    if not l.is_overseas]
        if overseas_mi and home_csk:
            with self.assertRaises(self.A.AuctionError) as err:
                self.offer(self.csk, self.mi, [home_csk[0]], [overseas_mi[0]])
            self.assertIn("overseas", str(err.exception))

    def test_a_player_moves_once_per_window(self):
        kohli = self.lot_named("Virat")
        bumrah = self.lot_named("Jasprit")
        self.complete(self.offer(self.mi, self.csk, [kohli], [bumrah]))
        with self.assertRaises(self.A.AuctionError) as err:
            self.offer(self.csk, self.mi, [kohli], [])
        self.assertIn("once per window", str(err.exception))

    def test_ownership_is_re_checked(self):
        kohli = self.lot_named("Virat")
        bumrah = self.lot_named("Jasprit")
        trade = self.offer(self.mi, self.csk, [kohli], [bumrah])
        kohli.sold_to_id = self.csk.id  # moved under the offer
        self.session.commit()
        with self.assertRaises(self.A.AuctionError):
            self.T.accept(self.session, trade, by_tg_id=BOB)

    def test_reject_counter_and_expiry(self):
        T = self.T
        kohli = self.lot_named("Virat")
        bumrah = self.lot_named("Jasprit")
        trade = self.offer(self.mi, self.csk, [kohli], [bumrah], cash=50)
        new = T.counter(self.session, trade, by_tg_id=BOB)
        self.session.commit()
        self.assertEqual(trade.status, T.STATUS_COUNTERED)
        self.assertEqual(new.team_a_id, self.csk.id)
        self.assertEqual(T.selection(new, "a"), [bumrah.id])
        self.assertEqual(int(new.cash_lakh), -50)
        T.send_offer(self.session, new, by_tg_id=BOB)
        T.reject(self.session, new, by_tg_id=ALICE)
        self.session.commit()
        self.assertEqual(new.status, T.STATUS_REJECTED)

        later = self.offer(self.mi, self.csk, [kohli], [])
        T.expire_stale(self.session, self.season,
                       now=datetime.utcnow() + timedelta(hours=3))
        self.assertEqual(later.status, T.STATUS_EXPIRED)

    def test_only_the_bot_admin_approves(self):
        T = self.T
        kohli = self.lot_named("Virat")
        bumrah = self.lot_named("Jasprit")
        trade = self.offer(self.mi, self.csk, [kohli], [bumrah])
        T.accept(self.session, trade, by_tg_id=BOB)
        self.session.commit()
        self.assertEqual(trade.status, T.STATUS_PENDING)
        # An owner, and an auction admin, are both refused.
        self.make_auction_admin(CAROL)
        for who in (ALICE, BOB, CAROL):
            with self.assertRaises(self.A.AuctionError):
                T.approve(self.session, trade, by_tg_id=who)
            with self.assertRaises(self.A.AuctionError):
                T.veto(self.session, trade, by_tg_id=who)
        T.approve(self.session, trade, by_tg_id=BOSS)
        self.session.commit()
        self.assertEqual(trade.status, T.STATUS_COMPLETED)

    def test_only_a_bot_admin_switches_approval_off(self):
        with self.assertRaises(self.A.AuctionError):
            self.T.set_trade_rules(self.session, self.season,
                                   {"approval": "off"}, by_tg_id=CAROL)
        rules = self.T.set_trade_rules(self.session, self.season,
                                       {"approval": "off"}, by_tg_id=BOSS)
        self.assertFalse(rules["require_approval"])
        kohli = self.lot_named("Virat")
        trade = self.offer(self.mi, self.csk, [kohli], [])
        self.T.accept(self.session, trade, by_tg_id=BOB)
        self.assertEqual(trade.status, self.T.STATUS_COMPLETED)

    def test_an_agreed_trade_survives_the_window_closing(self):
        T = self.T
        trade = self.offer(self.mi, self.csk, [self.lot_named("Virat")], [])
        T.accept(self.session, trade, by_tg_id=BOB)
        T.set_window(self.session, self.season, False)
        self.session.commit()
        self.assertEqual(trade.status, T.STATUS_PENDING)   # kept, not cancelled
        T.approve(self.session, trade, by_tg_id=BOSS)
        self.assertEqual(trade.status, T.STATUS_COMPLETED)
        # A NEW offer still cannot be made while the window is shut.
        with self.assertRaises(self.A.AuctionError):
            self.offer(self.csk, self.mi, [self.lot_named("Jasprit")], [])

    def test_squad_views_mark_traded_players(self):
        from services import auction_rich as AR
        kohli = self.lot_named("Virat")
        self.complete(self.offer(self.mi, self.csk, [kohli], [], cash=100))
        squad = self.A.render_squad(self.session, self.season, self.csk)
        line = [l for l in squad.splitlines() if "Virat" in l][0]
        self.assertTrue(line.endswith("🔁"))
        self.assertIn("🔁 Trades:", squad)
        self.assertIn("net to the purse", squad)
        _blocks, sold = AR.sold_view(self.session, self.season)
        self.assertIn("🔁", [l for l in sold.splitlines() if "Virat" in l][0])
        # Undone, the mark goes.
        trade = self.T.completed_trades(self.session, self.season)[0]
        self.T.reverse(self.session, trade, by_tg_id=BOSS)
        squad = self.A.render_squad(self.session, self.season, self.mi)
        self.assertFalse([l for l in squad.splitlines() if "Virat" in l][0]
                         .endswith("🔁"))

    def test_veto_and_undo(self):
        T = self.T
        kohli = self.lot_named("Virat")
        bumrah = self.lot_named("Jasprit")
        trade = self.offer(self.mi, self.csk, [kohli], [bumrah], cash=200)
        T.accept(self.session, trade, by_tg_id=BOB)
        T.veto(self.session, trade, by_tg_id=BOSS, reason="lopsided")
        self.session.commit()
        self.assertEqual(trade.status, T.STATUS_VETOED)
        self.session.refresh(kohli)
        self.assertEqual(kohli.sold_to_id, self.mi.id)

        mi0, csk0 = self.mi.purse_remaining_lakh, self.csk.purse_remaining_lakh
        # Undo is a different window's trade: open a fresh one first.
        trade = self.offer(self.mi, self.csk, [kohli], [bumrah], cash=200)
        self.complete(trade)
        with self.assertRaises(self.A.AuctionError):
            T.reverse(self.session, trade, by_tg_id=CAROL)
        T.reverse(self.session, trade, by_tg_id=BOSS)
        self.session.commit()
        self.session.refresh(kohli)
        self.session.refresh(bumrah)
        self.assertEqual(kohli.sold_to_id, self.mi.id)
        self.assertEqual(bumrah.sold_to_id, self.csk.id)
        self.assertEqual(self.mi.purse_remaining_lakh, mi0)
        self.assertEqual(self.csk.purse_remaining_lakh, csk0)
        self.assert_ledger_agrees()

    def test_trade_limit_per_window(self):
        self.T.set_trade_rules(self.session, self.season, {"trades": 1})
        self.complete(self.offer(self.mi, self.csk, [self.lot_named("Virat")], []))
        with self.assertRaises(self.A.AuctionError) as err:
            self.offer(self.mi, self.csk, [self.lot_named("Sanju")], [])
        self.assertIn("used all 1", str(err.exception))

    def test_the_window_switch(self):
        self.T.set_window(self.session, self.season, False)
        with self.assertRaises(self.A.AuctionError):
            self.offer(self.mi, self.csk, [self.lot_named("Virat")], [])
        self.T.set_window(self.session, self.season, True)
        self.offer(self.mi, self.csk, [self.lot_named("Virat")], [])

    def test_trade_block(self):
        T = self.T
        kohli = self.lot_named("Virat")
        T.block_add(self.session, self.season, self.mi, kohli, note="for a keeper")
        self.assertEqual(len(T.block_list(self.session, self.season)), 1)
        self.assertIn("for a keeper", T.render_block(self.session, self.season))
        with self.assertRaises(self.A.AuctionError):
            T.block_add(self.session, self.season, self.csk, kohli)
        # A trade takes him off the block.
        self.complete(self.offer(self.mi, self.csk, [kohli], []))
        self.assertEqual(T.block_list(self.session, self.season), [])

    def test_renderers_say_what_happened(self):
        T = self.T
        trade = self.complete(self.offer(self.mi, self.csk,
                                         [self.lot_named("Virat")],
                                         [self.lot_named("Jasprit")], cash=100))
        card = T.render_done(self.session, self.season, trade)
        self.assertIn("TRADE COMPLETED", card)
        self.assertIn("Virat", card)
        self.assertIn("cash", card)
        self.assertIn("#%d" % trade.id, T.render_log(self.session, self.season))
        self.assertIn("Bot admin approval", T.render_rules(self.session, self.season))

    def test_cards_say_trade_out_and_trade_in(self):
        T = self.T
        kohli, bumrah = self.lot_named("Virat"), self.lot_named("Jasprit")
        trade = self.offer(self.mi, self.csk, [kohli], [bumrah])
        for card in (T.render_offer(self.session, self.season, trade),):
            mumbai = card[card.index("🏏 <b>Mumbai</b>"):card.index("🏏 <b>Chennai</b>")]
            self.assertIn("📤 Trade out: Virat", mumbai)
            self.assertIn("📥 Trade in: Jasprit", mumbai)
            self.assertNotIn("send:", card)
        self.complete(trade)
        done = T.render_done(self.session, self.season, trade)
        chennai = done[done.index("🏏 <b>Chennai</b>"):]
        self.assertIn("📤 Trade out: Jasprit", chennai)
        self.assertIn("📥 Trade in: Virat", chennai)
        self.assertNotIn("receive:", done)
        self.assertIn("📤", T.render_log(self.session, self.season))

    def test_the_help_guide(self):
        parts = self.T.render_help(self.session, self.season)
        text = "\n".join(parts)
        self.assertTrue(all(len(p) <= 4096 for p in parts))
        for heading in ("1. What a trade is", "2. The money", "3. The four windows",
                        "4. What a trade must not break", "5. Step by step",
                        "6. Who approves", "7. Commands", "8. Quick answers"):
            self.assertIn(heading, text)
        for command in ("/atrade ", "/atrades", "/atradecash", "/atradecancel",
                        "/atradeblock", "/atraderules", "/atradewindow",
                        "/atradedeadline", "/atradeapprove", "/atradeveto",
                        "/atradeundo", "/atradehelp"):
            self.assertIn(command.replace("<", "&lt;"), text)
        self.assertIn("Post-auction", parts[0])     # this auction's window
        generic = "\n".join(self.T.render_help())
        self.assertIn("7. Commands", generic)

    def test_publish_after_a_trade_leaves_no_duplicate(self):
        from models import ChallengePlayer, ChallengeTeam
        self.complete(self.offer(self.mi, self.csk, [self.lot_named("Virat")], []))
        league = self.A.publish_to_league(self.session, self.season)
        self.A.publish_to_league(self.session, self.season)
        self.session.commit()
        rows = (self.session.query(ChallengePlayer)
                .join(ChallengeTeam, ChallengeTeam.id == ChallengePlayer.team_id)
                .filter(ChallengeTeam.league_id == league.id,
                        ChallengePlayer.name.like("Virat%")).all())
        self.assertEqual(len(rows), 1)
        team = self.session.query(ChallengeTeam).get(rows[0].team_id)
        self.assertEqual(team.name, "Chennai")
        self.assertIn('"trade"', rows[0].details_json)


class Sweeper(TradeCase):
    """The trade sweep says each trade once, and DMs the bot admins once."""

    def setUp(self):
        super().setUp()
        self.build_pool()
        self.run_auction([self.mi, self.csk])
        self.sent = []
        # Other tests' trades share this database; they have been "said".
        from models import AuctionTrade
        now = datetime.utcnow()
        (self.session.query(AuctionTrade)
         .update({"announced_at": now, "admin_notified_at": now},
                 synchronize_session=False))
        self.session.commit()

    def tick(self):
        import asyncio
        from types import SimpleNamespace
        from services import auction_scheduler as S

        async def send_message(chat_id=None, text="", **kwargs):
            self.sent.append((chat_id, text, kwargs.get("reply_markup")))
            return SimpleNamespace(message_id=len(self.sent))

        bot = SimpleNamespace(send_message=send_message)
        import time
        S._last_trade_notice[0] = time.monotonic()   # notices: own tests
        with mock.patch("services.admin_ids.configured_admin_ids",
                        return_value={BOSS}):
            asyncio.run(S._trade_tick(SimpleNamespace(bot=bot), self.session,
                                      datetime.utcnow()))

    def test_pending_goes_to_the_group_and_the_bot_admin_then_done_once(self):
        trade = self.offer(self.mi, self.csk, [self.lot_named("Virat")], [])
        self.T.accept(self.session, trade, by_tg_id=BOB)
        self.session.commit()
        self.tick()
        chats = [chat for chat, _text, _markup in self.sent]
        self.assertIn(self.season.chat_id, chats)
        self.assertIn(BOSS, chats)
        self.assertTrue(all(markup is not None for _c, _t, markup in self.sent))
        self.sent.clear()
        self.tick()
        self.assertEqual(self.sent, [])

        self.T.approve(self.session, trade, by_tg_id=BOSS)
        self.session.commit()
        self.tick()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("TRADE COMPLETED", self.sent[0][1])
        self.sent.clear()
        self.tick()
        self.assertEqual(self.sent, [])

    def test_stale_offers_expire_in_the_sweep(self):
        trade = self.offer(self.mi, self.csk, [self.lot_named("Virat")], [])
        trade.expires_at = datetime.utcnow() - timedelta(seconds=1)
        self.session.commit()
        self.tick()
        self.session.refresh(trade)
        self.assertEqual(trade.status, self.T.STATUS_EXPIRED)


class MidSeason(TradeCase):
    def setUp(self):
        super().setUp()
        from models import Tournament, TournamentMatch, TournamentTeam, ChallengeTeam
        self.build_pool()
        self.run_auction([self.mi, self.csk])
        self.league = self.A.publish_to_league(self.session, self.season)
        self.session.commit()
        self.tour = Tournament(name="IPL", league_id=self.league.id,
                               status="active", is_active=True)
        self.session.add(self.tour)
        self.session.flush()
        teams = {}
        for ct in (self.session.query(ChallengeTeam)
                   .filter(ChallengeTeam.league_id == self.league.id).all()):
            tt = TournamentTeam(tournament_id=self.tour.id, name=ct.name,
                                challenge_team_id=ct.id)
            self.session.add(tt)
            self.session.flush()
            teams[ct.name] = tt
        self.tt = teams
        self.match = TournamentMatch(tournament_id=self.tour.id,
                                     team1_id=teams["Mumbai"].id,
                                     team2_id=teams["Chennai"].id,
                                     status="completed", stage="league")
        self.session.add(self.match)
        self.session.add(TournamentMatch(tournament_id=self.tour.id,
                                         team1_id=teams["Mumbai"].id,
                                         team2_id=teams["Chennai"].id,
                                         status="scheduled", stage="league"))
        self.session.commit()

    def test_phase_and_stats_follow_the_player(self):
        from models import ChallengePlayer, TournamentPlayerStats
        self.assertEqual(self.T.trade_phase(self.session, self.season)[0],
                         self.T.PHASE_MID_SEASON)
        kohli = self.lot_named("Virat")
        cp = (self.session.query(ChallengePlayer)
              .filter(ChallengePlayer.name == kohli.name).one())
        self.session.add(TournamentPlayerStats(tournament_id=self.tour.id,
                                               user_id=1, roster_id=cp.id,
                                               name=cp.name, bat_runs=77))
        old_team = cp.team_id
        self.complete(self.offer(self.mi, self.csk, [kohli], []))
        self.session.refresh(cp)
        self.assertNotEqual(cp.team_id, old_team)
        same = (self.session.query(ChallengePlayer)
                .filter(ChallengePlayer.name == kohli.name).all())
        self.assertEqual([r.id for r in same], [cp.id])

    def test_a_live_match_blocks_the_two_teams(self):
        self.match.status = "live"
        self.session.commit()
        with self.assertRaises(self.A.AuctionError) as err:
            self.offer(self.mi, self.csk, [self.lot_named("Virat")], [])
        self.assertIn("playing a match", str(err.exception))

    def test_deadline_by_matches_by_date_and_the_playoffs(self):
        from models import TournamentMatch
        T = self.T
        self.season.trade_deadline_matches = 1
        self.assertIsNone(T.trade_phase(self.session, self.season)[0])
        self.season.trade_deadline_matches = None
        self.season.trade_deadline_at = datetime.utcnow() - timedelta(minutes=1)
        self.assertIsNone(T.trade_phase(self.session, self.season)[0])
        self.season.trade_deadline_at = None
        self.assertEqual(T.trade_phase(self.session, self.season)[0],
                         T.PHASE_MID_SEASON)
        self.session.add(TournamentMatch(tournament_id=self.tour.id,
                                         team1_id=self.tt["Mumbai"].id,
                                         team2_id=self.tt["Chennai"].id,
                                         status="completed", stage="final"))
        self.session.flush()
        phase, why = T.trade_phase(self.session, self.season)
        self.assertIsNone(phase)
        self.assertIn("playoffs", why)

    def test_window_notices_open_remind_and_close_once_each(self):
        from models import TournamentMatch
        T = self.T
        now = datetime.utcnow()
        kinds = lambda: [k for k, _key, _t in T.due_notices(self.session, self.season, now)]
        say = lambda: [T.record_notice(self.session, self.season, k, key, t)
                       for k, key, t in T.due_notices(self.session, self.season, now)]

        self.assertEqual(kinds(), [T.NOTICE_OPEN])
        say()
        self.assertEqual(kinds(), [])

        # A dated deadline is a new window: open again, then the reminders.
        self.season.trade_deadline_at = now + timedelta(hours=20)
        self.assertEqual(kinds(), [T.NOTICE_OPEN, T.NOTICE_DL_DAY])
        say()
        self.assertEqual(kinds(), [])
        self.season.trade_deadline_at = now + timedelta(minutes=30)
        say()   # (the new key opens and reminds within the hour)
        self.assertEqual(kinds(), [])

        # One match left before a match-count deadline.
        self.season.trade_deadline_at = None
        self.season.trade_deadline_matches = 2
        self.assertIn(T.NOTICE_DL_MATCH, kinds())
        say()

        # An agreed trade, then the playoffs: closed, with the wait noted.
        trade = self.offer(self.mi, self.csk, [self.lot_named("Virat")], [])
        T.accept(self.session, trade, by_tg_id=BOB)
        self.session.add(TournamentMatch(tournament_id=self.tour.id,
                                         team1_id=self.tt["Mumbai"].id,
                                         team2_id=self.tt["Chennai"].id,
                                         status="completed", stage="qualifier"))
        self.session.flush()
        notices = T.due_notices(self.session, self.season, now)
        self.assertEqual([k for k, _key, _t in notices], [T.NOTICE_CLOSED])
        self.assertIn("still with the bot admin", notices[0][2])
        say()
        self.assertEqual(kinds(), [])
        # The playoffs close it even for an agreed trade.
        with self.assertRaises(self.A.AuctionError):
            T.approve(self.session, trade, by_tg_id=BOSS)

    def test_a_trade_agreed_before_the_deadline_can_be_approved_after(self):
        T = self.T
        trade = self.offer(self.mi, self.csk, [self.lot_named("Virat")], [])
        T.accept(self.session, trade, by_tg_id=BOB)
        self.season.trade_deadline_at = datetime.utcnow() - timedelta(minutes=5)
        self.session.commit()
        T.approve(self.session, trade, by_tg_id=BOSS)
        self.assertEqual(trade.status, T.STATUS_COMPLETED)

    def test_the_sweeper_says_the_notices(self):
        import asyncio
        from types import SimpleNamespace
        from services import auction_scheduler as S
        sent = []

        async def send_message(chat_id=None, text="", **kwargs):
            sent.append((chat_id, text))
            return SimpleNamespace(message_id=1)
        context = SimpleNamespace(bot=SimpleNamespace(send_message=send_message))
        asyncio.run(S._trade_notices(context, self.session, datetime.utcnow(),
                                     force=True))
        mine = [t for chat, t in sent if chat == self.season.chat_id]
        self.assertTrue(any("mid-season trade window is open" in t for t in mine))
        sent.clear()
        asyncio.run(S._trade_notices(context, self.session, datetime.utcnow(),
                                     force=True))
        self.assertEqual([t for chat, t in sent if chat == self.season.chat_id], [])

    def test_mid_season_can_be_switched_off(self):
        self.T.set_trade_rules(self.session, self.season, {"midseason": "off"})
        with self.assertRaises(self.A.AuctionError):
            self.offer(self.mi, self.csk, [self.lot_named("Virat")], [])


class MidAuction(TradeCase):
    def setUp(self):
        super().setUp()
        self.build_pool()
        A = self.A
        # Sell the first two lots, then leave the third on the block.
        lot = A.start(self.session, self.season, now=NOW)
        for team in (self.mi, self.csk):
            A.place_bid(self.session, self.season, lot, team,
                        A.next_min_bid(self.season, lot), now=NOW)
            A.sell_lot(self.session, self.season, lot, now=NOW)
            lot = A.open_next_lot(self.session, self.season, now=NOW)
        self.session.commit()
        self.on_block = lot

    def test_phase(self):
        self.assertEqual(self.T.trade_phase(self.session, self.season)[0],
                         self.T.PHASE_MID_AUCTION)

    def test_trade_between_lots_and_the_standing_bidder_refusal(self):
        A = self.A
        kohli = self.lot_named("Virat")
        bumrah = self.lot_named("Jasprit")
        trade = self.offer(self.mi, self.csk, [kohli], [bumrah])
        # Mumbai now bids on the lot on the block: the trade must wait.
        A.place_bid(self.session, self.season, self.on_block, self.mi,
                    A.next_min_bid(self.season, self.on_block), now=NOW)
        self.session.commit()
        with self.assertRaises(A.AuctionError) as err:
            self.T.accept(self.session, trade, by_tg_id=BOB)
        self.assertIn("standing bid", str(err.exception))
        A.sell_lot(self.session, self.season, self.on_block, now=NOW)
        self.session.commit()
        self.complete(trade)
        self.assertEqual(trade.status, self.T.STATUS_COMPLETED)
        self.assert_ledger_agrees()

    def test_reachability_reserve(self):
        """Taking a player dearer than the base-price floor eats headroom.

        Chennai (one player) could fill its four open minimum slots at the
        floor before the trade; buying Kohli outright at his auction price
        leaves it unable to fill the three still open afterwards.
        """
        floor = 100
        self.season.min_squad_size = 5
        self.season.min_base_price_lakh = floor
        self.session.commit()
        kohli = self.lot_named("Virat")
        price = int(kohli.sold_price_lakh)
        self.assertGreater(price, floor)
        # Before: purse - 4 floors >= 0. After: purse - price - 3 floors < 0.
        self.set_purse(self.csk, 4 * floor + (price - floor) // 2)
        with self.assertRaises(self.A.AuctionError) as err:
            self.offer(self.csk, self.mi, [], [kohli])
        self.assertIn("minimum squad", str(err.exception))


class MidAuctionRestart(MidAuction):
    def test_restart_puts_every_purse_back_including_trade_cash(self):
        A = self.A
        kohli = self.lot_named("Virat")
        bumrah = self.lot_named("Jasprit")
        trade = self.complete(self.offer(self.mi, self.csk, [kohli], [bumrah],
                                         cash=150))
        self.assertEqual(trade.status, self.T.STATUS_COMPLETED)
        A.restart_auction(self.session, self.season, now=NOW)
        self.session.commit()
        for team in (self.mi, self.csk):
            self.session.refresh(team)
            self.assertEqual(team.purse_remaining_lakh, self.purse_lakh)
        self.assert_ledger_agrees()
        self.session.refresh(trade)
        self.assertEqual(trade.status, self.T.STATUS_REVERSED)


class PreAuction(TradeCase):
    def test_retained_players_can_be_traded_before_the_auction(self):
        A, T = self.A, self.T
        self.season.max_retentions = 2
        self.session.commit()
        A.retain(self.session, self.season, self.mi, self.players[0], 1500)
        A.retain(self.session, self.season, self.csk, self.players[1], 1000)
        self.session.commit()
        self.assertEqual(T.trade_phase(self.session, self.season)[0],
                         T.PHASE_PRE)
        kohli = self.lot_named("Virat")
        bumrah = self.lot_named("Jasprit")
        trade = self.complete(self.offer(self.mi, self.csk, [kohli], [bumrah]))
        self.assertEqual(trade.status, T.STATUS_COMPLETED)
        self.assertEqual(trade.phase, T.PHASE_PRE)
        self.assert_ledger_agrees()


class Commands(TradeCase):
    """The Telegram surface, end to end, with every press authorised."""

    def setUp(self):
        super().setUp()
        self.build_pool()
        self.run_auction([self.mi, self.csk])
        self.replies, self.alerts, self.edits, self.sent = [], [], [], []

    def _update(self, user_id, args=()):
        from types import SimpleNamespace

        async def reply_text(text, **kwargs):
            self.replies.append((text, kwargs.get("reply_markup")))
            return SimpleNamespace(message_id=1)

        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=self.season.chat_id,
                                           type="supergroup"),
            effective_user=SimpleNamespace(id=user_id, username="u",
                                           first_name="U"),
            effective_message=SimpleNamespace(reply_text=reply_text,
                                              message_id=7))
        context = SimpleNamespace(args=list(args), bot=SimpleNamespace())
        return update, context

    def command(self, handler, user_id, *args):
        import asyncio
        update, context = self._update(user_id, args)
        asyncio.run(handler(update, context))
        return self.replies[-1]

    def press(self, data, user_id):
        import asyncio
        from types import SimpleNamespace

        async def answer(text=None, show_alert=False):
            self.alerts.append((text, show_alert))

        async def edit_message_text(text, **kwargs):
            self.edits.append((text, kwargs.get("reply_markup")))

        query = SimpleNamespace(
            data=data, from_user=SimpleNamespace(id=user_id),
            message=SimpleNamespace(chat=SimpleNamespace(id=self.season.chat_id)),
            answer=answer, edit_message_text=edit_message_text)
        update = SimpleNamespace(callback_query=query)

        async def send_message(chat_id, text, **kwargs):
            self.sent.append((chat_id, text, kwargs.get("reply_markup")))
            return SimpleNamespace(message_id=len(self.sent))

        from handlers import auction_trade as H
        asyncio.run(H.trade_callback(update, SimpleNamespace(
            bot=SimpleNamespace(send_message=send_message))))
        return self.alerts[-1]

    def _buttons(self, markup):
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    def test_the_whole_flow_through_the_buttons(self):
        from handlers import auction_trade as H
        T = self.T
        text, markup = self.command(H.atrade_handler, ALICE, "Chennai")
        self.assertIn("TRADE BUILDER", text)
        trade = T.live_trade_for(self.session, self.season, self.mi.id)
        kohli = self.lot_named("Virat")
        bumrah = self.lot_named("Jasprit")
        toggle = f"au_tr_t_{trade.id}_{kohli.id}_a_0"
        self.assertIn(toggle, self._buttons(markup))

        # Chennai cannot touch Mumbai's builder.
        text, alert = self.press(toggle, BOB)
        self.assertTrue(alert)
        self.press(toggle, ALICE)
        self.press(f"au_tr_t_{trade.id}_{bumrah.id}_b_0", ALICE)
        self.press(f"au_tr_c_{trade.id}_100_a_0", ALICE)
        self.press(f"au_tr_s_{trade.id}", ALICE)
        # The offer goes out as a NEW message that tags Chennai's owner, with
        # the answer buttons; the builder becomes a pointer with none.
        chat, ping, buttons = self.sent[-1]
        self.assertEqual(chat, self.season.chat_id)
        self.assertIn("sent you a trade offer", ping)
        self.assertIn("Chennai", ping.split("—")[0])
        self.assertIn(f"au_tr_y_{trade.id}", self._buttons(buttons))
        self.assertIsNone(self.edits[-1][1])

        # Mumbai cannot accept its own offer; Chennai can.
        self.assertTrue(self.press(f"au_tr_y_{trade.id}", ALICE)[1])
        self.press(f"au_tr_y_{trade.id}", BOB)
        self.assertIn(f"au_tr_ap_{trade.id}", self._buttons(self.edits[-1][1]))

        # An auction admin and both owners are refused the approval.
        self.make_auction_admin(CAROL)
        self.session.commit()
        for who in (ALICE, BOB, CAROL):
            text, alert = self.press(f"au_tr_ap_{trade.id}", who)
            self.assertTrue(alert)
            self.assertIn("bot", text)
        self.press(f"au_tr_ap_{trade.id}", BOSS)
        self.assertIn("TRADE COMPLETED", self.edits[-1][0])
        self.session.expire_all()
        self.assertEqual(T.get_trade(self.session, trade.id).status,
                         T.STATUS_COMPLETED)
        self.assertEqual(self.lot_named("Virat").sold_to_id, self.csk.id)

    def test_builder_marks_trade_out_and_trade_in(self):
        from handlers import auction_trade as H
        T = self.T
        _text, markup = self.command(H.atrade_handler, ALICE, "Chennai")
        labels = [b.text for row in markup.inline_keyboard for b in row]
        self.assertTrue(any("📤 Trade out" in l for l in labels))
        self.assertTrue(any("📥 Trade in" in l for l in labels))
        trade = T.live_trade_for(self.session, self.season, self.mi.id)
        kohli, bumrah = self.lot_named("Virat"), self.lot_named("Jasprit")
        self.press(f"au_tr_t_{trade.id}_{kohli.id}_a_0", ALICE)
        mine = [b.text for row in self.edits[-1][1].inline_keyboard for b in row]
        self.assertTrue(any(l.startswith("📤 Virat") for l in mine))
        self.press(f"au_tr_t_{trade.id}_{bumrah.id}_b_0", ALICE)
        theirs = [b.text for row in self.edits[-1][1].inline_keyboard for b in row]
        self.assertTrue(any(l.startswith("📥 Jasprit") for l in theirs))

    def test_atradehelp_answers_in_the_group(self):
        from handlers import auction_trade as H
        self.command(H.atradehelp_handler, CAROL)
        text = "\n".join(t for t, _m in self.replies)
        self.assertIn("TRADE GUIDE", text)
        self.assertIn("7. Commands", text)
        self.assertIsNotNone(self.replies[-1][1])   # ❌ Close on the last part

    def test_a_rejection_tags_the_side_that_offered(self):
        from handlers import auction_trade as H
        T = self.T
        self.command(H.atrade_handler, ALICE, "Chennai")
        trade = T.live_trade_for(self.session, self.season, self.mi.id)
        self.press(f"au_tr_t_{trade.id}_{self.lot_named('Virat').id}_a_0", ALICE)
        self.press(f"au_tr_s_{trade.id}", ALICE)
        self.press(f"au_tr_n_{trade.id}", BOB)
        self.assertIn("rejected trade", self.sent[-1][1])
        self.assertIn("Mumbai", self.sent[-1][1])

    def test_counter_offer_through_the_button(self):
        from handlers import auction_trade as H
        T = self.T
        self.command(H.atrade_handler, ALICE, "Chennai")
        trade = T.live_trade_for(self.session, self.season, self.mi.id)
        self.press(f"au_tr_t_{trade.id}_{self.lot_named('Virat').id}_a_0", ALICE)
        self.press(f"au_tr_s_{trade.id}", ALICE)
        self.press(f"au_tr_k_{trade.id}", BOB)
        self.session.expire_all()
        new = T.live_trade_for(self.session, self.season, self.csk.id)
        self.assertEqual(new.parent_trade_id, trade.id)
        self.assertEqual(new.team_a_id, self.csk.id)
        self.assertIn("counter-offer", self.edits[-1][0])

    def test_approve_command_is_bot_admin_only(self):
        from handlers import auction_trade as H
        text, _ = self.command(H.atradeapprove_handler, CAROL)
        self.assertIn("bot owner", text)
        text, _ = self.command(H.atradeapprove_handler, BOSS)
        self.assertIn("No trades are waiting", text)

    def test_rules_and_deadline_parsing(self):
        from handlers import auction_trade as H
        text, _ = self.command(H.atraderules_handler, ALICE)
        self.assertIn("Mid-auction trades", text)
        self.assertEqual(H.parse_deadline("12"), (None, 12))
        self.assertEqual(H.parse_deadline("off"), (None, None))
        at, n = H.parse_deadline("2026-05-01 18:00")
        self.assertEqual((at.month, at.hour, n), (5, 18, None))
        with self.assertRaises(self.A.AuctionError):
            H.parse_deadline("soon")


if __name__ == "__main__":
    unittest.main()

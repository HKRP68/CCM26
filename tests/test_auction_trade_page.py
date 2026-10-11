"""The 🔁 Trades fold on the auction setup page, over HTTP.

The website login is the bot owner's, so this is the one surface where
approving, vetoing and forcing a trade need no bot-admin id: what is pinned
is that the fold renders in every state, that its forms reach the service
(and so obey every squad rule), and that a completed trade can be undone.
"""

import itertools
import os
import sys
import tempfile
import unittest
from datetime import datetime

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
                 "services.auction_rich", "services.auction_trade_service",
                 "handlers.auction", "handlers.auction_trade")

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
NOW = datetime(2026, 3, 1, 12, 0, 0)


class TradePageTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            import admin
        except Exception as exc:            # pragma: no cover - env-dependent
            raise unittest.SkipTest(f"admin app unavailable: {exc}")
        admin.app.config["TESTING"] = True
        admin.app.config["WTF_CSRF_ENABLED"] = False
        cls.admin = admin

    def setUp(self):
        from database import get_session
        from models import Player
        from services import auction_service as A
        from services import auction_trade_service as T

        self.session = get_session()
        self.A, self.T = A, T
        tag = next(_PID)
        players = []
        for offset, rating in enumerate((97, 93, 88, 84)):
            player = Player(name=f"Trade {tag} {offset}", rating=rating,
                            is_active=True, **PLAYER_DEFAULTS)
            self.session.add(player)
            players.append(player)
        self.session.flush()
        self.season = A.create_season(self.session, f"Trade page {tag}",
                                      min_squad_size=1, max_squad_size=6,
                                      bid_gap_seconds=0)
        A.bind_chat(self.session, self.season, -9000 - tag)
        self.mi = A.create_franchise(self.session, self.season, "Mumbai",
                                     owner_tg_id=11)
        self.csk = A.create_franchise(self.session, self.season, "Chennai",
                                      owner_tg_id=22)
        A.add_players_to_pool(self.session, self.season, players)
        self.session.commit()
        lot = A.start(self.session, self.season, now=NOW)
        index = 0
        while lot is not None and self.season.status == A.STATUS_LIVE:
            team = (self.mi, self.csk)[index % 2]
            A.place_bid(self.session, self.season, lot, team,
                        A.next_min_bid(self.season, lot), now=NOW)
            A.sell_lot(self.session, self.season, lot, now=NOW)
            index += 1
            if self.season.status != A.STATUS_LIVE:
                break
            lot = A.open_next_lot(self.session, self.season, now=NOW)
        self.session.commit()
        self.lots = {lot.name: lot for lot in A.lots(self.session, self.season.id)}
        self.tag = tag
        self.client = self.admin.app.test_client()
        with self.client.session_transaction() as flask_session:
            flask_session["admin"] = True

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def page(self):
        response = self.client.get(f"/auctions/{self.season.id}")
        self.assertEqual(200, response.status_code)
        return response.get_data(as_text=True)

    def post(self, data):
        return self.client.post(f"/auctions/{self.season.id}/trades", data=data,
                                follow_redirects=True)

    def test_the_fold_renders(self):
        body = self.page()
        self.assertIn("🔁 Trades", body)
        self.assertIn("Post-auction window open", body)
        self.assertIn("Save trade rules", body)

    def test_force_a_trade_then_undo_it(self):
        first = f"Trade {self.tag} 0"     # Mumbai's
        body = self.post({"action": "trade_force", "team_a": self.mi.id,
                          "team_b": self.csk.id, "lots_a": first,
                          "cash_cr": "1", "cash_dir": "a"}).get_data(as_text=True)
        self.assertIn("done", body)
        self.session.expire_all()
        lot = self.session.get(type(self.lots[first]), self.lots[first].id)
        self.assertEqual(lot.sold_to_id, self.csk.id)
        trade = self.T.completed_trades(self.session, self.season)[0]
        self.assertIn(f"#{trade.id}", self.page())

        self.post({"action": "trade_undo", "trade_id": trade.id})
        self.session.expire_all()
        lot = self.session.get(type(lot), lot.id)
        self.assertEqual(lot.sold_to_id, self.mi.id)
        for team in (self.mi, self.csk):
            self.session.refresh(team)
            self.assertEqual(self.A.ledger_total(self.session, team.id),
                             team.purse_remaining_lakh)

    def test_a_forced_trade_still_obeys_the_squad_rules(self):
        self.season.max_squad_size = 2
        self.session.commit()
        body = self.post({"action": "trade_force", "team_a": self.mi.id,
                          "team_b": self.csk.id,
                          "lots_a": f"Trade {self.tag} 0"}).get_data(as_text=True)
        self.assertIn("squad limit", body)

    def test_rules_form_and_web_approval(self):
        T = self.T
        self.post({"action": "trade_rules", "trades_open": "1",
                   "post_auction": "1", "mid_season": "1",
                   "require_approval": "1", "allow_cash": "1",
                   "max_players_per_side": "2", "max_trades_per_team": "3",
                   "max_cash_cr": "5", "deadline_matches": "10"})
        self.session.expire_all()
        rules = T.trade_rules(self.season)
        self.assertFalse(rules["pre_auction"])
        self.assertEqual(rules["max_players_per_side"], 2)
        self.assertEqual(rules["max_cash_lakh"], 500)
        self.assertEqual(self.season.trade_deadline_matches, 10)

        trade = T.open_trade(self.session, self.season, self.mi, self.csk)
        T.toggle_lot(self.session, trade, self.lots[f"Trade {self.tag} 0"].id)
        T.send_offer(self.session, trade)
        T.accept(self.session, trade, by_tg_id=22)
        self.session.commit()
        self.assertEqual(trade.status, T.STATUS_PENDING)
        self.assertIn("Waiting for your approval", self.page())
        self.post({"action": "trade_approve", "trade_id": trade.id})
        self.session.expire_all()
        self.assertEqual(T.get_trade(self.session, trade.id).status,
                         T.STATUS_COMPLETED)


    def test_console_shows_and_approves_a_pending_trade(self):
        T = self.T
        trade = T.open_trade(self.session, self.season, self.mi, self.csk)
        T.toggle_lot(self.session, trade, self.lots[f"Trade {self.tag} 0"].id)
        T.send_offer(self.session, trade)
        T.accept(self.session, trade, by_tg_id=22)
        self.session.commit()
        panel = self.client.get(f"/auctions/{self.season.id}/console/panel")
        body = panel.get_data(as_text=True)
        self.assertIn("1 to approve", body)
        self.assertIn(f"#{trade.id}", body)
        response = self.client.post(f"/auctions/{self.season.id}/trades",
                                    data={"action": "trade_approve",
                                          "trade_id": trade.id, "back": "console"})
        self.assertIn("/console", response.headers["Location"])
        self.session.expire_all()
        self.assertEqual(T.get_trade(self.session, trade.id).status,
                         T.STATUS_COMPLETED)


if __name__ == "__main__":
    unittest.main()

"""The user page's Remove button on a card that has been traded.

A traded card's roster row is still referenced by its Trade rows, and that FK
is NO ACTION — so in Postgres the admin removal failed for every traded card.
SQLite only enforces FKs with the pragma on, so the test switches it on.
"""

import itertools
import os
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
                 "services.trait_service",
                 "services.activity_service")

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

    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys = ON")

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



class AdminRemoveTradedPlayerTests(unittest.TestCase):

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
        self.session = get_session()
        self.client = self.admin.app.test_client()
        with self.client.session_transaction() as flask_session:
            flask_session["admin"] = True

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def make_user(self):
        from models import User
        user = User(telegram_id=770_000 + next(_PID), first_name="Owner",
                    roster_count=1)
        self.session.add(user)
        self.session.flush()
        return user

    def make_card(self, user):
        from models import Player, UserRoster
        n = next(_PID)
        player = Player(name=f"Traded {n}", version="Base", rating=80,
                        category="Batsman", country="India", bat_hand="Right",
                        bowl_hand="Right", bowl_style="Medium")
        self.session.add(player)
        self.session.flush()
        entry = UserRoster(user_id=user.id, player_id=player.id)
        self.session.add(entry)
        self.session.flush()
        return player, entry

    def trade(self, a, b, entry_a, entry_b, pa, pb, status):
        from datetime import datetime, timedelta
        from models import Trade
        t = Trade(initiator_id=a.id, receiver_id=b.id,
                  initiator_player_id=pa.id, receiver_player_id=pb.id,
                  initiator_roster_id=entry_a.id, receiver_roster_id=entry_b.id,
                  status=status, expires_at=datetime.utcnow() + timedelta(hours=1))
        self.session.add(t)
        self.session.flush()
        return t

    def remove(self, user, entry):
        return self.client.post(f"/users/{user.id}/remove-player/{entry.id}",
                                follow_redirects=True)

    def test_a_card_from_a_completed_trade_can_be_removed(self):
        from models import Trade, UserRoster
        a, b = self.make_user(), self.make_user()
        pa, ea = self.make_card(a)
        pb, eb = self.make_card(b)
        trade = self.trade(a, b, ea, eb, pa, pb, "completed")
        a.captain_roster_id = ea.id
        self.session.commit()
        entry_id, trade_id = ea.id, trade.id

        body = self.remove(a, ea).get_data(as_text=True)

        self.assertIn(f"Removed {pa.name}", body)
        self.session.expire_all()
        self.assertIsNone(self.session.get(UserRoster, entry_id))
        trade = self.session.get(Trade, trade_id)
        self.assertEqual("completed", trade.status)
        self.assertIsNone(trade.initiator_roster_id)
        self.assertEqual(eb.id, trade.receiver_roster_id)
        self.assertIsNone(self.session.get(type(a), a.id).captain_roster_id)

    def test_removing_a_card_cancels_its_pending_trade(self):
        from models import Trade, UserRoster
        a, b = self.make_user(), self.make_user()
        pa, ea = self.make_card(a)
        pb, eb = self.make_card(b)
        trade = self.trade(a, b, ea, eb, pa, pb, "pending")
        self.session.commit()
        entry_id, trade_id = eb.id, trade.id

        self.remove(b, eb)

        self.session.expire_all()
        self.assertIsNone(self.session.get(UserRoster, entry_id))
        trade = self.session.get(Trade, trade_id)
        self.assertEqual("cancelled", trade.status)
        self.assertIsNone(trade.receiver_roster_id)


if __name__ == "__main__":
    unittest.main()

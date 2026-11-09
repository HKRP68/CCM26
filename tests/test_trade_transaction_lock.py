"""A pending trade must revalidate after concurrent wallet/squad changes."""

import itertools
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from services import trading_service as ts


class TradeTransactionLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.engine = create_engine(
            f"sqlite:///{cls.directory.name}/trades.db",
            connect_args={"check_same_thread": False, "timeout": 10})
        ts.User.metadata.create_all(cls.engine)
        cls.sessions = sessionmaker(bind=cls.engine, autoflush=False,
                                   expire_on_commit=False)
        cls.identities = itertools.count(850_000_000)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        cls.directory.cleanup()

    def setUp(self):
        self.session = self.sessions()
        self.fee = ts.trade_fee_for(85)
        self.users = [ts.User(telegram_id=next(self.identities), first_name="Captain",
                              total_coins=self.fee * 5, roster_count=1)
                      for _ in range(2)]
        self.players = [ts.Player(name=f"Card {next(self.identities)}", rating=85,
                                  category="Batsman", country="India",
                                  bat_hand="Right", bowl_hand="Right",
                                  bowl_style="Medium") for _ in range(2)]
        self.session.add_all(self.users + self.players)
        self.session.flush()
        self.entries = [ts.UserRoster(user_id=u.id, player_id=p.id,
                                      order_position=1)
                        for u, p in zip(self.users, self.players)]
        self.session.add_all(self.entries)
        self.session.flush()
        self.trade = ts.Trade(
            initiator_id=self.users[0].id, receiver_id=self.users[1].id,
            initiator_player_id=self.players[0].id,
            receiver_player_id=self.players[1].id,
            initiator_roster_id=self.entries[0].id,
            receiver_roster_id=self.entries[1].id, status="pending",
            expires_at=datetime.utcnow() + timedelta(minutes=2))
        self.session.add(self.trade)
        self.session.commit()
        # Scoring and equipped traits are independent from these wallet races.
        self.log_patch = patch.object(ts, "log_activity", return_value=None)
        self.trait_patch = patch(
            "services.trait_service.return_traits_to_inventory", return_value=0)
        self.log_patch.start()
        self.trait_patch.start()

    def tearDown(self):
        self.trait_patch.stop()
        self.log_patch.stop()
        self.session.rollback()
        self.session.close()

    def test_stale_wallet_cannot_spend_coins_already_spent_by_a_purchase(self):
        with self.sessions() as other:
            buyer = other.get(ts.User, self.users[0].id)
            buyer.total_coins = self.fee - 1
            other.commit()
        self.assertEqual(self.users[0].total_coins, self.fee * 5)
        result = ts.complete_trade(self.session, self.trade.id)
        self.session.commit()
        self.assertFalse(result["success"])
        self.assertEqual(self.users[0].total_coins, self.fee - 1)
        self.assertEqual(self.users[1].total_coins, self.fee * 5)
        self.assertEqual(self.entries[0].user_id, self.users[0].id)

    def test_trade_cancelled_by_undo_cannot_be_completed_from_cached_status(self):
        with self.sessions() as other:
            other.get(ts.Trade, self.trade.id).status = "cancelled"
            other.commit()
        self.assertEqual(self.trade.status, "pending")
        result = ts.complete_trade(self.session, self.trade.id)
        self.session.commit()
        self.assertFalse(result["success"])
        self.assertEqual(self.trade.status, "cancelled")
        self.assertEqual([u.total_coins for u in self.users], [self.fee * 5] * 2)
        self.assertEqual(self.entries[0].user_id, self.users[0].id)

    def test_roster_cached_before_a_replacement_cannot_trade_the_old_card(self):
        with self.sessions() as other:
            replacement = ts.Player(name=f"Replacement {next(self.identities)}",
                                    rating=90, category="Batsman", country="India",
                                    bat_hand="Right", bowl_hand="Right",
                                    bowl_style="Medium")
            other.add(replacement)
            other.flush()
            other.get(ts.UserRoster, self.entries[0].id).player_id = replacement.id
            other.commit()
        result = ts.complete_trade(self.session, self.trade.id)
        self.session.commit()
        self.assertFalse(result["success"])
        self.assertEqual(self.trade.status, "cancelled")
        self.assertEqual([u.total_coins for u in self.users], [self.fee * 5] * 2)

    def test_two_workers_complete_and_charge_a_trade_only_once(self):
        barrier = threading.Barrier(2)
        outcomes, errors = [], []
        trade_id = self.trade.id

        def worker():
            try:
                with self.sessions() as session:
                    trade = session.get(ts.Trade, trade_id)
                    session.get(ts.User, trade.initiator_id)
                    session.get(ts.User, trade.receiver_id)
                    barrier.wait(timeout=5)
                    result = ts.complete_trade(session, trade_id)
                    session.commit()
                    outcomes.append(result["success"])
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(outcomes), [False, True])
        self.session.expire_all()
        self.assertEqual([u.total_coins for u in self.users], [self.fee * 4] * 2)
        self.assertEqual(self.entries[0].user_id, self.users[1].id)
        self.assertEqual(self.entries[1].user_id, self.users[0].id)


if __name__ == "__main__":
    unittest.main()

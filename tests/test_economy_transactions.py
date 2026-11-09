"""Wallet safety against real stale sessions and concurrent database writers."""

import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap

_MODULES = ("database", "models", "config", "services.economy_transactions",
            "services.activity_service", "services.season_activity_service")


def setUpModule():
    global _saved, _previous_url, _path, _engine
    _saved = _module_swap.save(_MODULES)
    _module_swap.unload(_MODULES)
    _previous_url = os.environ.get("DATABASE_URL")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        _path = tmp.name
    os.environ["DATABASE_URL"] = f"sqlite:///{_path}"
    from database import Base, engine
    import models
    _engine = engine
    Base.metadata.create_all(engine)


def tearDownModule():
    _engine.dispose()
    if _previous_url is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _previous_url
    _module_swap.restore(_saved)
    os.unlink(_path)


class WalletTransactionTests(unittest.TestCase):
    _telegram_id = 930_000_000

    def setUp(self):
        from database import get_session
        self.session = get_session()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def user(self, coins=1000):
        from models import User
        type(self)._telegram_id += 1
        user = User(telegram_id=self._telegram_id, total_coins=coins,
                    total_gems=123)
        self.session.add(user)
        self.session.commit()
        return user

    def test_joe_root_cannot_be_bought_with_screenshot_balance(self):
        from services.economy_transactions import debit_coins, lock_user_for_update
        user = self.user(2_011_842)
        lock_user_for_update(self.session, user)
        self.assertFalse(debit_coins(self.session, user, 4_450_000))
        self.assertEqual(user.total_coins, 2_011_842)

    def test_stale_high_balance_cannot_pass_database_debit(self):
        from database import get_session
        from models import User
        from services.economy_transactions import debit_coins
        user = self.user(6_461_842)
        user_id = user.id
        with get_session() as other:
            fresh = other.get(User, user_id)
            self.assertTrue(debit_coins(other, fresh, 4_450_000))
            other.commit()
        self.assertEqual(user.total_coins, 6_461_842)
        self.assertFalse(debit_coins(self.session, user, 4_450_000))
        self.assertEqual(user.total_coins, 2_011_842)

    def test_concurrent_requests_cannot_spend_same_balance(self):
        from database import get_session
        from models import User
        from services.economy_transactions import debit_coins, lock_user_for_update
        user_id = self.user().id
        self.session.rollback()
        barrier = threading.Barrier(2)
        outcomes, errors = [], []

        def buy():
            try:
                with get_session() as session:
                    user = session.get(User, user_id)
                    barrier.wait(timeout=5)
                    lock_user_for_update(session, user)
                    outcomes.append(debit_coins(session, user, 650))
                    session.commit()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=buy) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertCountEqual(outcomes, [True, False])
        self.assertEqual(self.session.get(User, user_id).total_coins, 350)

    def test_debit_and_log_snapshot_roll_back_together(self):
        from models import ActivityLog
        from services.activity_service import log_activity
        from services.economy_transactions import debit_coins
        user = self.user()
        self.assertTrue(debit_coins(self.session, user, 650))
        log = log_activity(self.session, user.id, "buy", coins_change=-650)
        self.assertEqual((log.coins_balance_after, log.gems_balance_after), (350, 123))
        self.session.flush()
        log_id = log.id
        self.session.rollback()
        self.assertEqual(user.total_coins, 1000)
        self.assertIsNone(self.session.get(ActivityLog, log_id))

    def test_negative_price_cannot_credit_wallet(self):
        from services.economy_transactions import debit_coins
        user = self.user()
        for amount in (-1, 1.5, True, "650"):
            with self.assertRaises(ValueError):
                debit_coins(self.session, user, amount)
        self.assertEqual(user.total_coins, 1000)


class HistoryBalanceTests(unittest.TestCase):
    def row(self, coins, snapshot=None, gems=0):
        from types import SimpleNamespace
        return SimpleNamespace(coins_change=coins, gems_change=gems,
                               coins_balance_after=snapshot, gems_balance_after=None)

    def test_missing_old_delta_does_not_override_recorded_snapshot(self):
        from services.activity_history import annotate_balances
        newest = self.row(-4_450_000, snapshot=2_011_842)
        old = self.row(11_000)
        annotate_balances([newest, old], -2_438_158, 0)
        self.assertTrue(newest._coins_recorded)
        self.assertEqual(newest._coins_before, 6_461_842)
        self.assertEqual(newest._net_coins, 2_011_842)
        self.assertFalse(old._coins_recorded)
        self.assertEqual(old._net_coins, 6_461_842)

    def test_zero_recorded_balance_is_preserved(self):
        from services.activity_history import annotate_balances
        row = self.row(-100, snapshot=0)
        annotate_balances([row], 999, 0)
        self.assertTrue(row._coins_recorded)
        self.assertEqual((row._coins_before, row._net_coins), (100, 0))

    def test_buy_undo_cycles_restore_original_balance(self):
        from services.activity_history import annotate_balances
        rows = [self.row(880_000), self.row(-880_000),
                self.row(880_000), self.row(-880_000)]
        annotate_balances(rows, 1_445_587, 0)
        self.assertEqual([row._net_coins for row in rows],
                         [1_445_587, 565_587, 1_445_587, 565_587])
        self.assertTrue(all(not row._coins_recorded for row in rows))

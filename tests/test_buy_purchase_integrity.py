"""Real database regressions for /buypl affordability and callback replays."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import handlers.buy as buy
from config import MAX_ROSTER, get_buy_value
from models import ActivityLog, BuyConfirmation, PendingUndo, Player, User, UserRoster
from services.buy_bonus import Offer
from utils.idempotency import _GUARD


class PurchaseIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            f"sqlite:///{self.tmp.name}/purchase.db",
            connect_args={"check_same_thread": False, "timeout": 10})
        for model in (User, Player, UserRoster, ActivityLog, PendingUndo, BuyConfirmation):
            model.__table__.create(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        with self.Session() as session:
            session.add(User(id=1, telegram_id=99, total_coins=6_000_000,
                             total_gems=0, roster_count=0))
            session.add_all([
                Player(id=7, name="Joe Root", rating=97, category="Batsman", country="England",
                       bat_hand="Right", bowl_hand="Right", bowl_style="Spin"),
                Player(id=8, name="Charlie Dean", rating=85, category="Bowler", country="England",
                       bat_hand="Right", bowl_hand="Right", bowl_style="Spin"),
                Player(id=9, name="Lungi Ngidi", rating=85, category="Bowler", country="England",
                       bat_hand="Right", bowl_hand="Right", bowl_style="Spin"),
            ])
            session.commit()
        _GUARD.clear()
        self.patches = [
            patch.object(buy, "get_session", self.Session),
            patch.object(buy, "match_lock_message", return_value=None),
            patch.object(buy, "_is_buy_restricted", return_value=(False, None)),
            patch("services.buy_bonus.current_offer", return_value=Offer(
                active=True, min_rating=96, bps=10)),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for item in reversed(self.patches):
            item.stop()
        _GUARD.clear()
        self.engine.dispose()
        self.tmp.cleanup()

    def _set_wallet(self, coins, roster_count=0):
        with self.Session() as session:
            user = session.get(User, 1)
            user.total_coins, user.roster_count = coins, roster_count
            session.commit()

    def _query(self, *, player_id=7, message_id=77):
        message = NS(chat_id=-5, message_id=message_id, reply_text=AsyncMock(),
                     date=datetime.now(timezone.utc))
        return NS(data=f"buypl_{player_id}_1_99", from_user=NS(
            id=99, username="captain", first_name="Captain"), message=message,
            answer=AsyncMock(), edit_message_reply_markup=AsyncMock())

    def _run(self, query):
        asyncio.run(buy.buypl_confirm_callback(NS(callback_query=query), NS()))

    def _assert_no_purchase(self, expected_coins):
        with self.Session() as session:
            self.assertEqual(session.get(User, 1).total_coins, expected_coins)
            for model in (UserRoster, ActivityLog, PendingUndo, BuyConfirmation):
                self.assertEqual(session.query(model).count(), 0, model.__name__)

    def test_screenshot_balance_cannot_buy_joe_root(self):
        self._set_wallet(2_011_842)
        query = self._query()
        self._run(query)
        self._assert_no_purchase(2_011_842)
        text = query.message.reply_text.call_args.args[0]
        self.assertIn("2,438,158 more coins", text)
        self.assertIn("Price: 4,450,000", text)

    def test_exact_price_succeeds_with_zero_balance(self):
        self._set_wallet(get_buy_value(97))
        self._run(self._query())
        with self.Session() as session:
            user = session.get(User, 1)
            self.assertEqual((user.total_coins, user.total_gems, user.roster_count),
                             (0, 4_450, 1))
            self.assertEqual(session.query(UserRoster).count(), 1)
            self.assertEqual(session.query(BuyConfirmation).count(), 1)
            undo = session.query(PendingUndo).one()
            purchase = session.query(ActivityLog).filter_by(action="buy").one()
            self.assertEqual(json.loads(undo.payload)["activity_id"], purchase.id)
            self.assertEqual(purchase.coins_balance_after, 0)

    def test_wallet_changed_during_telegram_await_is_rechecked(self):
        query = self._query()
        query.edit_message_reply_markup.side_effect = lambda **kw: self._set_wallet(2_011_842)
        self._run(query)
        self._assert_no_purchase(2_011_842)
        self.assertIn("needs 2,438,158", query.message.reply_text.call_args.args[0])

    def test_roster_filled_during_telegram_await_is_rechecked(self):
        query = self._query()
        query.edit_message_reply_markup.side_effect = lambda **kw: self._set_wallet(
            6_000_000, MAX_ROSTER)
        self._run(query)
        self._assert_no_purchase(6_000_000)
        self.assertIn("Roster full", query.message.reply_text.call_args.args[0])

    def test_replay_after_undo_and_process_guard_loss_does_not_repurchase(self):
        query = self._query()
        self._run(query)
        # The essential state after a valid undo: coins restored, player removed,
        # PendingUndo consumed. The durable callback receipt must survive it.
        with self.Session() as session:
            session.query(UserRoster).delete()
            session.query(PendingUndo).delete()
            user = session.get(User, 1)
            user.total_coins, user.total_gems, user.roster_count = 6_000_000, 0, 0
            session.commit()
        _GUARD.clear()
        self._run(query)
        with self.Session() as session:
            self.assertEqual(session.get(User, 1).total_coins, 6_000_000)
            self.assertEqual(session.get(User, 1).total_gems, 0)
            self.assertEqual(session.query(UserRoster).count(), 0)
            self.assertEqual(session.query(BuyConfirmation).count(), 1)
        self.assertIn("already processed", query.message.reply_text.call_args.args[0])

    def test_undo_record_failure_rolls_back_every_part_of_purchase(self):
        with patch("services.undo_service.record_buy", side_effect=RuntimeError("write failed")):
            self._run(self._query())
        self._assert_no_purchase(6_000_000)

    def test_receipt_survives_failed_telegram_success_notification(self):
        query = self._query()
        query.message.reply_text.side_effect = RuntimeError("Telegram unavailable")
        self._run(query)
        _GUARD.clear()
        query.message.reply_text.side_effect = None
        self._run(query)
        with self.Session() as session:
            self.assertEqual(session.get(User, 1).total_coins, 1_550_000)
            self.assertEqual(session.query(UserRoster).count(), 1)
            self.assertEqual(session.query(BuyConfirmation).count(), 1)

    def test_different_buttons_cannot_spend_same_balance_concurrently(self):
        self._set_wallet(1_000_000)
        barrier = threading.Barrier(2)
        first = self._query(player_id=8, message_id=81)
        second = self._query(player_id=9, message_id=82)
        for query in (first, second):
            query.edit_message_reply_markup.side_effect = lambda **kw: barrier.wait(timeout=5)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(self._run, (first, second)))
        with self.Session() as session:
            user = session.get(User, 1)
            self.assertEqual((user.total_coins, user.roster_count), (120_000, 1))
            self.assertEqual(session.query(UserRoster).count(), 1)
            self.assertEqual(session.query(BuyConfirmation).count(), 1)
            self.assertEqual(session.query(ActivityLog).filter_by(action="buy").count(), 1)

    def test_different_buttons_cannot_purchase_same_version_concurrently(self):
        self._set_wallet(2_000_000)
        barrier = threading.Barrier(2)
        queries = [self._query(player_id=8, message_id=message_id)
                   for message_id in (83, 84)]
        for query in queries:
            query.edit_message_reply_markup.side_effect = lambda **kw: barrier.wait(timeout=5)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(self._run, queries))
        with self.Session() as session:
            user = session.get(User, 1)
            self.assertEqual((user.total_coins, user.roster_count), (1_120_000, 1))
            self.assertEqual(session.query(UserRoster).count(), 1)
            self.assertEqual(session.query(BuyConfirmation).count(), 1)

    def test_failed_database_debit_grants_nothing_and_rolls_back_receipt(self):
        with patch.object(buy, "debit_coins", return_value=False):
            self._run(self._query())
        self._assert_no_purchase(6_000_000)

    def test_expired_or_malformed_callbacks_do_not_open_database(self):
        expired = self._query()
        expired.message.date -= timedelta(minutes=3)
        malformed = self._query(message_id=78)
        malformed.data = "buypl_bad_1_99"
        wrong_owner = self._query(message_id=79)
        wrong_owner.data = "buypl_7_1_100"
        with patch.object(buy, "get_session", side_effect=AssertionError("DB touched")):
            for query in (expired, malformed, wrong_owner):
                self._run(query)
                query.answer.assert_awaited_once()
                self.assertTrue(query.answer.call_args.kwargs["show_alert"])


if __name__ == "__main__":
    unittest.main()

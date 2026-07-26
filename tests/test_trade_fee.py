"""Every completed trade charges both captains 1% of the card's buy value.

Both sides of a trade hold the same OVR — that is the rule the whole flow is
built on — so one fee figure applies to both, and it leaves the economy rather
than moving between them. A captain who can't cover it doesn't get a half-done
trade: the swap is refused before any card changes hands.
"""

import unittest
from datetime import datetime, timedelta

from config import TRADE_FEE_PERCENT, get_buy_value
from services import trading_service as ts


class FeeMathTests(unittest.TestCase):
    def test_one_percent_of_the_buy_value(self):
        for rating in (75, 80, 85, 90, 95):
            buy = get_buy_value(rating)
            self.assertEqual(ts.trade_fee_for(rating),
                             max(1, -(-buy * TRADE_FEE_PERCENT // 100)))

    def test_the_configured_percent_is_one(self):
        self.assertEqual(TRADE_FEE_PERCENT, 1)

    def test_a_cheap_card_still_costs_something(self):
        self.assertGreaterEqual(ts.trade_fee_for(40), 1)

    def test_junk_ratings_are_free_not_a_crash(self):
        self.assertEqual(ts.trade_fee_for(None), 0)
        self.assertEqual(ts.trade_fee_for("high"), 0)


class FakeUser:
    def __init__(self, uid, coins, username="cap"):
        self.id = uid
        self.telegram_id = 1000 + uid
        self.total_coins = coins
        self.username = username
        self.first_name = username


class FakePlayer:
    def __init__(self, pid, rating):
        self.id = pid
        self.name = f"Player{pid}"
        self.rating = rating


class FakeEntry:
    def __init__(self, rid, user_id, player_id):
        self.id = rid
        self.user_id = user_id
        self.player_id = player_id


class FakeTrade:
    def __init__(self, initiator, receiver, init_entry, recv_entry):
        self.id = 1
        self.initiator_id = initiator.id
        self.receiver_id = receiver.id
        self.initiator_roster_id = init_entry.id
        self.receiver_roster_id = recv_entry.id
        self.status = "pending"
        self.trade_fee = 0
        self.expires_at = datetime.utcnow() + timedelta(seconds=60)
        self.updated_at = None
        self.completed_at = None


class FakeSession:
    """Serves the handful of lookups ``complete_trade`` performs."""

    def __init__(self, objects):
        self._objects = objects
        self.flushed = 0

    def query(self, model):
        session = self

        class _Q:
            def get(self, key):
                return session._objects.get((model.__name__, key))

            def filter(self, *a, **k):
                return self

            def all(self):
                return []

            def first(self):
                return None

        return _Q()

    def flush(self):
        self.flushed += 1

    def add(self, obj):
        pass


class CompleteTradeFeeTests(unittest.TestCase):
    RATING = 85

    def setUp(self):
        self.fee = ts.trade_fee_for(self.RATING)
        self.init_user = FakeUser(1, self.fee * 10, "alpha")
        self.recv_user = FakeUser(2, self.fee * 10, "beta")
        self.init_player = FakePlayer(11, self.RATING)
        self.recv_player = FakePlayer(22, self.RATING)
        self.init_entry = FakeEntry(101, 1, 11)
        self.recv_entry = FakeEntry(202, 2, 22)
        self.trade = FakeTrade(self.init_user, self.recv_user,
                               self.init_entry, self.recv_entry)
        self.session = FakeSession({
            ("Trade", 1): self.trade,
            ("User", 1): self.init_user,
            ("User", 2): self.recv_user,
        })

        # Stub the validation + trait plumbing complete_trade leans on.
        self._saved = {
            "expire": ts.expire_stale_trades,
            "validate": ts._validate_same_ovr_swap,
            "log": ts.log_activity,
        }
        ts.expire_stale_trades = lambda session: None
        ts._validate_same_ovr_swap = lambda *a, **k: (
            self.init_entry, self.init_player,
            self.recv_entry, self.recv_player, None)
        ts.log_activity = lambda *a, **k: None

        import services.trait_service as trait_service
        self._real_return = trait_service.return_traits_to_inventory
        trait_service.return_traits_to_inventory = lambda session, ids: 0

    def tearDown(self):
        ts.expire_stale_trades = self._saved["expire"]
        ts._validate_same_ovr_swap = self._saved["validate"]
        ts.log_activity = self._saved["log"]
        import services.trait_service as trait_service
        trait_service.return_traits_to_inventory = self._real_return

    def test_both_captains_are_charged_the_same_fee(self):
        before_init = self.init_user.total_coins
        before_recv = self.recv_user.total_coins
        result = ts.complete_trade(self.session, 1)

        self.assertTrue(result["success"])
        self.assertEqual(result["fee"], self.fee)
        self.assertEqual(self.init_user.total_coins, before_init - self.fee)
        self.assertEqual(self.recv_user.total_coins, before_recv - self.fee)
        # And the amount is recorded on the trade row.
        self.assertEqual(self.trade.trade_fee, self.fee)

    def test_the_cards_still_change_hands(self):
        ts.complete_trade(self.session, 1)
        self.assertEqual(self.init_entry.user_id, self.recv_user.id)
        self.assertEqual(self.recv_entry.user_id, self.init_user.id)
        self.assertEqual(self.trade.status, "completed")

    def test_a_captain_who_cannot_pay_gets_no_trade_at_all(self):
        self.recv_user.total_coins = self.fee - 1
        result = ts.complete_trade(self.session, 1)

        self.assertFalse(result["success"])
        self.assertIn("trade fee", result["message"])
        self.assertIn("beta", result["message"])
        # Nothing moved, and the trade is closed rather than left pending.
        self.assertEqual(self.init_entry.user_id, 1)
        self.assertEqual(self.recv_entry.user_id, 2)
        self.assertEqual(self.init_user.total_coins, self.fee * 10)
        self.assertEqual(self.trade.status, "cancelled")

    def test_the_fee_is_not_a_transfer_between_the_two(self):
        total_before = self.init_user.total_coins + self.recv_user.total_coins
        ts.complete_trade(self.session, 1)
        total_after = self.init_user.total_coins + self.recv_user.total_coins
        self.assertEqual(total_before - total_after, self.fee * 2)


if __name__ == "__main__":
    unittest.main()

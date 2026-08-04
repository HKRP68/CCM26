"""Tests for /claim paying no coins.

The card you pull is the whole reward. ``config.CLAIM_COINS`` has been 0 since
the economy rebalance and a fresh install seeds the same, but databases seeded
before it kept the original 500 and went on printing "+500 Coin Added" on every
pull. A guarded one-shot migration clears exactly that legacy value.
"""

import re
import sqlite3
import sys
import types
import unittest

# The statement database.py ships. Asserted to be present verbatim below, so
# widening the shipped WHERE clause without revisiting these tests fails.
CLAIM_COINS_SQL = (
    "UPDATE command_rewards SET coin_amount = 0 "
    "WHERE command_key = 'claim' AND coin_amount = 500"
)


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class ClaimCoinMigrationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE command_rewards ("
                        "command_key TEXT PRIMARY KEY, coin_amount INTEGER)")

    def tearDown(self):
        self.db.close()

    def _coins(self, key):
        row = self.db.execute(
            "SELECT coin_amount FROM command_rewards WHERE command_key = ?",
            (key,)).fetchone()
        return row[0] if row else None

    def _rows(self, pairs):
        self.db.executemany(
            "INSERT INTO command_rewards (command_key, coin_amount) VALUES (?, ?)",
            pairs)

    def test_the_shipped_statement_is_the_one_under_test(self):
        # database.py writes the statement across adjacent string literals, so
        # compare against the source with that concatenation collapsed out.
        source = re.sub(r'"\s*\n\s*"', "", _read("database.py"))
        self.assertIn(CLAIM_COINS_SQL, source)

    def test_legacy_500_is_cleared(self):
        self._rows([("claim", 500)])
        self.db.execute(CLAIM_COINS_SQL)
        self.assertEqual(self._coins("claim"), 0)

    def test_other_commands_keep_their_coins(self):
        # /daily and /debut pay coins on purpose — only /claim is touched.
        self._rows([("claim", 500), ("daily", 500), ("debut", 500)])
        self.db.execute(CLAIM_COINS_SQL)
        self.assertEqual(self._coins("claim"), 0)
        self.assertEqual(self._coins("daily"), 500)
        self.assertEqual(self._coins("debut"), 500)

    def test_an_admin_chosen_value_is_left_alone(self):
        # Narrow by design: only a row still sitting on the shipped 500 moves,
        # so an admin who deliberately set claim coins keeps them.
        self._rows([("claim", 250)])
        self.db.execute(CLAIM_COINS_SQL)
        self.assertEqual(self._coins("claim"), 250)

    def test_already_zero_stays_zero(self):
        self._rows([("claim", 0)])
        self.db.execute(CLAIM_COINS_SQL)
        self.assertEqual(self._coins("claim"), 0)

    def test_running_twice_changes_nothing_further(self):
        self._rows([("claim", 500)])
        self.db.execute(CLAIM_COINS_SQL)
        self.db.execute(CLAIM_COINS_SQL)
        self.assertEqual(self._coins("claim"), 0)


class Reward:
    def __init__(self, coin_amount):
        self.coin_amount = coin_amount


class _Query:
    def __init__(self, row):
        self._row = row

    def filter(self, *a, **k):
        return self

    def first(self):
        return self._row


class FakeSession:
    def __init__(self, row):
        self._row = row

    def query(self, *a, **k):
        return _Query(self._row)


class ClaimCoinReadPathTests(unittest.TestCase):
    """What /claim actually credits, given a CommandReward row."""

    def setUp(self):
        self._saved = sys.modules.get("models")
        models = types.ModuleType("models")
        models.CommandReward = type("CommandReward", (), {"command_key": ""})
        sys.modules["models"] = models
        from services.command_config_service import get_coin_amount
        self.get_coin_amount = get_coin_amount

    def tearDown(self):
        if self._saved is None:
            sys.modules.pop("models", None)
        else:
            sys.modules["models"] = self._saved

    def test_zero_on_the_row_pays_nothing(self):
        session = FakeSession(Reward(0))
        self.assertEqual(self.get_coin_amount(session, "claim", 0), 0)

    def test_missing_row_falls_back_to_the_config_default(self):
        session = FakeSession(None)
        self.assertEqual(self.get_coin_amount(session, "claim", 0), 0)

    def test_an_admin_can_still_turn_claim_coins_back_on(self):
        session = FakeSession(Reward(750))
        self.assertEqual(self.get_coin_amount(session, "claim", 0), 750)


class ClaimRewardDefaultTests(unittest.TestCase):
    def test_config_default_is_zero(self):
        # The fallback used when no CommandReward row exists at all.
        self.assertIn("CLAIM_COINS = 0", _read("config.py"))

    def test_fresh_install_seeds_zero_claim_coins(self):
        seed = _read("database.py").split('("claim", "/claim", "c",')[1][:220]
        self.assertIn("coin_amount=0", seed)

    def test_card_hides_the_coin_line_when_nothing_is_paid(self):
        # handlers/claim.py builds the line only `if coin_reward:`, so a zero
        # reward prints no "+N Coin Added" row at all.
        source = _read("handlers/claim.py")
        self.assertIn("if coin_reward:", source)
        self.assertIn("Coin Added", source)


if __name__ == "__main__":
    unittest.main()

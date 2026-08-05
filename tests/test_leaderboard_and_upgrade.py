"""Two small rules that had visible consequences in-game.

1. The AI opponent is a real ``users`` row (telegram_id -1) so a bot match can be
   stored like any other. It plays constantly and wins constantly, so it sat at
   the top of /cmuleaderboard's Matches, Wins and Runs boards. Nothing should
   credit it, and no board should list it.

2. Upgrading a card to a higher version charged the difference in buy value
   *plus a 10% markup*. The buy-value curve is steep at the top, so that surcharge
   landed hardest on exactly the upgrades players wanted. The price is now the
   plain difference.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class UpgradeCostTest(unittest.TestCase):
    def setUp(self):
        from services import upgrade_service
        self.upgrade_service = upgrade_service

    def test_cost_is_exactly_the_difference_in_buy_value(self):
        from config import get_buy_value
        for current, target in ((80, 85), (85, 90), (90, 93), (93, 96)):
            with self.subTest(upgrade=f"{current}->{target}"):
                expected = get_buy_value(target) - get_buy_value(current)
                self.assertEqual(
                    self.upgrade_service._upgrade_cost(current, target),
                    max(self.upgrade_service.MIN_UPGRADE_COST, expected))

    def test_no_markup_remains(self):
        # Guards against the surcharge creeping back in under another name.
        self.assertFalse(hasattr(self.upgrade_service, "UPGRADE_MARKUP"))

    def test_a_tiny_step_up_still_costs_the_floor(self):
        cost = self.upgrade_service._upgrade_cost(80, 81)
        self.assertGreaterEqual(cost, self.upgrade_service.MIN_UPGRADE_COST)

    def test_a_downgrade_never_costs_a_negative_amount(self):
        cost = self.upgrade_service._upgrade_cost(95, 80)
        self.assertEqual(cost, self.upgrade_service.MIN_UPGRADE_COST)


class AiUserIsNotAPlayerTest(unittest.TestCase):
    def setUp(self):
        from services import match_rewards
        self.match_rewards = match_rewards

    def test_the_bot_sentinel_is_recognised(self):
        from types import SimpleNamespace
        from services.bot_ai import BOT_TG_ID
        self.assertTrue(self.match_rewards.is_ai_user(
            SimpleNamespace(telegram_id=BOT_TG_ID)))
        self.assertTrue(self.match_rewards.is_ai_user(
            SimpleNamespace(telegram_id=0)))
        self.assertTrue(self.match_rewards.is_ai_user(
            SimpleNamespace(telegram_id=None)))
        self.assertTrue(self.match_rewards.is_ai_user(None))

    def test_a_real_account_is_not_ai(self):
        from types import SimpleNamespace
        self.assertFalse(self.match_rewards.is_ai_user(
            SimpleNamespace(telegram_id=123456789)))

    def test_streaks_and_active_days_skip_the_ai(self):
        from types import SimpleNamespace

        class _Session:
            def __init__(self, rows):
                self.rows = rows

            def query(self, _model):
                return self

            def get(self, uid):
                return self.rows.get(uid)

        human = SimpleNamespace(telegram_id=555, win_streak=2, best_streak=2,
                                active_days=4, last_match_date=None)
        bot = SimpleNamespace(telegram_id=-1, win_streak=99, best_streak=99,
                              active_days=99, last_match_date=None)
        session = _Session({1: human, 2: bot})
        self.match_rewards.record_match_result_stats(session, 2, 1)

        self.assertEqual(bot.win_streak, 99)      # untouched
        self.assertEqual(bot.active_days, 99)
        self.assertEqual(human.win_streak, 0)     # the human lost
        self.assertEqual(human.active_days, 5)


class LeaderboardFiltersTheAiTest(unittest.TestCase):
    """Both boards must filter on the same rule, in source."""

    def _read(self, path):
        with open(os.path.join(os.path.dirname(__file__), "..", path)) as fh:
            return fh.read()

    def test_cmuleaderboard_filters_every_metric(self):
        # Every branch that builds a query must go through the filter — a new
        # metric added without it would put the bot back on the board.
        source = self._read("handlers/leaderboard.py")
        body = source.split("def _get_leaderboard_data(")[1].split("\ndef ")[0]
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("q = ") or stripped.startswith("rows = ("):
                self.assertTrue(
                    "_humans_only(" in stripped or "User.telegram_id > 0" in body,
                    f"unfiltered leaderboard query: {stripped}")
        self.assertEqual(body.count("_humans_only("), 5)   # the five user metrics
        self.assertIn("User.telegram_id > 0", body)        # the runs board's join

    def test_the_miniapp_board_filters_every_metric(self):
        source = self._read("admin.py")
        block = source.split("def webapp_leaderboard()")[1].split("\n@app.route")[0]
        self.assertIn("humans = User.telegram_id > 0", block)
        # One filter(humans …) per metric: wins, streak, active_days, win_rate,
        # coins and roster_rating.
        self.assertEqual(block.count("filter(humans"), 6)

    def test_the_miniapp_board_offers_streak_and_active_days(self):
        source = self._read("admin.py")
        block = source.split("def webapp_leaderboard()")[1].split("\n@app.route")[0]
        self.assertIn('metric == "streak"', block)
        self.assertIn('metric == "active_days"', block)
        self.assertIn("User.best_streak", block)
        self.assertIn("User.active_days", block)

    def test_the_miniapp_ui_offers_the_two_new_chips(self):
        source = self._read("templates/webapp.html")
        self.assertIn("setLeaderboardMetric('streak')", source)
        self.assertIn("setLeaderboardMetric('active_days')", source)


if __name__ == "__main__":
    unittest.main()

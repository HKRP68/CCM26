"""Regression coverage for the alt-farming payout channel.

Ending / forfeiting a match used to move the quitter's fine into the
opponent's wallet. Two players who are the same person turn that into an
income stream: run a match against your own alt, quit on the alt, and the
alt's balance (starter coins, its own daily claims, its own free packs)
lands on the main account. Every fresh alt is another payout.

The Mini App path was worse than a transfer. ``compensation`` was the *full*
penalty while ``applied_penalty`` was capped at the quitter's balance, so a
quitter with 0 coins paid nothing and the opponent was still credited the
whole amount — coins minted from nothing.

The rule now, on all three paths: the fine is charged to the quitter and
leaves the economy. The opponent's reward for being quit on is the forfeit
win, the streak and the stats — never currency.

``handle_match_termination`` is exercised for real. The two Telegram
handlers are asserted against source text: importing them needs
python-telegram-bot, which the lean test env doesn't have.
"""

import os
import unittest
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


# ════════════════════════════════════════════════════════════════════
# 1. Mini App / timeout path — handle_match_termination
# ════════════════════════════════════════════════════════════════════

class MatchTerminationEconomyTests(unittest.TestCase):
    """The quitter pays, the opponent's balance never moves."""

    def setUp(self):
        from database import Base
        from models import User, Match
        self.User, self.Match = User, Match
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()

        self.quitter = User(telegram_id=1001, first_name="Quitter",
                            total_coins=50_000, total_gems=100)
        self.opponent = User(telegram_id=1002, first_name="Opponent",
                             total_coins=7_000, total_gems=9)
        self.db.add_all([self.quitter, self.opponent])
        self.db.flush()
        self.match = Match(user1_id=self.quitter.id, user2_id=self.opponent.id,
                           status="playing", overs=10,
                           created_at=datetime.utcnow())
        self.db.add(self.match)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _terminate(self, state):
        """Run handle_match_termination with `state` standing in for the match
        state store (which is Redis/DB-backed and not part of this test)."""
        from services import match_webapp_service as svc
        saved = {}
        orig_get, orig_save = svc.mwa.get_state, svc.mwa.save_state
        orig_scorecard = svc.save_final_scorecard
        svc.mwa.get_state = lambda mid: state
        svc.mwa.save_state = lambda mid, s: saved.update(s or {})
        svc.save_final_scorecard = lambda *a, **k: None
        try:
            return svc.handle_match_termination(
                self.db, self.match.id, self.quitter.id, reason="quit"), saved
        finally:
            svc.mwa.get_state = orig_get
            svc.mwa.save_state = orig_save
            svc.save_final_scorecard = orig_scorecard

    @staticmethod
    def _state(**over):
        """A part-played 10-over match: 30 balls bowled."""
        base = {"overs": 10, "played_via": "webapp", "current_over": 6,
                "current_ball": 0, "innings": 1, "total_runs": 44,
                "total_wickets": 1}
        base.update(over)
        return base

    def test_opponent_balance_is_untouched(self):
        before_coins = self.opponent.total_coins
        before_gems = self.opponent.total_gems
        (ok, info), _saved = self._terminate(self._state())
        self.assertTrue(ok, info)
        self.assertGreater(info["penalty"], 0,
                           "a part-played match must still fine the quitter")
        self.assertEqual(self.opponent.total_coins, before_coins,
                         "the fine must not be paid to the opponent")
        self.assertEqual(self.opponent.total_gems, before_gems)

    def test_quitter_still_pays_the_penalty(self):
        (ok, info), _saved = self._terminate(self._state())
        self.assertTrue(ok, info)
        self.assertEqual(self.quitter.total_coins, 50_000 - info["penalty"])

    def test_no_coins_are_minted_when_the_quitter_cannot_pay(self):
        """The old bug: penalty capped at the balance, compensation was not."""
        self.quitter.total_coins = 0
        self.db.commit()
        total_before = (self.quitter.total_coins or 0) + (self.opponent.total_coins or 0)
        (ok, info), _saved = self._terminate(self._state())
        self.assertTrue(ok, info)
        self.assertEqual(info["penalty"], 0, "nothing could be deducted")
        total_after = (self.quitter.total_coins or 0) + (self.opponent.total_coins or 0)
        self.assertEqual(total_after, total_before,
                         "a broke quitter must not create coins for anyone")

    def test_compensation_is_reported_as_zero(self):
        """The key stays in the payload (callers read it); the value is 0."""
        (ok, info), saved = self._terminate(self._state())
        self.assertTrue(ok, info)
        self.assertEqual(info["compensation"], 0)
        self.assertEqual(saved["match_result"]["compensation"], 0)

    def test_the_opponent_still_wins_by_forfeit(self):
        """Removing the payout must not remove the reward that isn't currency."""
        (ok, info), _saved = self._terminate(self._state())
        self.assertTrue(ok, info)
        self.assertEqual(info["winner_id"], self.opponent.id)
        self.assertEqual(info["loser_id"], self.quitter.id)
        self.assertEqual(self.opponent.matches_won or 0, 1)
        self.assertEqual(self.quitter.matches_lost or 0, 1)
        self.assertEqual(self.match.margin_type, "forfeit")

    def test_a_match_with_no_balls_bowled_is_still_a_free_cancel(self):
        (ok, info), _saved = self._terminate(
            self._state(current_over=1, current_ball=0, total_runs=0,
                        total_wickets=0))
        self.assertTrue(ok, info)
        self.assertTrue(info["cancelled"])
        self.assertEqual(self.quitter.total_coins, 50_000)
        self.assertEqual(self.opponent.total_coins, 7_000)


# ════════════════════════════════════════════════════════════════════
# 2. /endmatch (bot) — handlers/match.py
# ════════════════════════════════════════════════════════════════════

class EndmatchHandlerSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read("handlers/match.py")

    def test_no_credit_to_the_opponent(self):
        self.assertNotIn("opponent.total_coins = (opponent.total_coins or 0) + charged_coins",
                         self.src)
        self.assertNotIn("opponent.total_gems = (opponent.total_gems or 0) + charged_gems",
                         self.src)

    def test_the_compensation_activity_row_is_gone(self):
        self.assertNotIn("endmatch_compensation", self.src)

    def test_the_quitter_is_still_fined(self):
        self.assertIn("charged_coins = min(u.total_coins or 0, fine_coins)", self.src)
        self.assertIn("u.total_coins = (u.total_coins or 0) - charged_coins", self.src)

    def test_the_prompt_no_longer_promises_the_opponent_a_payout(self):
        self.assertNotIn("Your opponent gets the same as compensation.", self.src)
        self.assertIn("not paid to your opponent", self.src)


# ════════════════════════════════════════════════════════════════════
# 3. Challenge League inactivity forfeit — handlers/cipl_play.py
# ════════════════════════════════════════════════════════════════════

class CiplForfeitSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read("handlers/cipl_play.py")

    def test_no_credit_to_the_winner(self):
        self.assertNotIn("win_user.total_coins = (win_user.total_coins or 0) + charged_coins",
                         self.src)
        self.assertNotIn("win_user.total_gems = (win_user.total_gems or 0) + charged_gems",
                         self.src)

    def test_the_idle_player_is_still_fined(self):
        self.assertIn("charged_coins = min(idle_user.total_coins or 0, CIPL_FORFEIT_COINS)",
                      self.src)
        self.assertIn("idle_user.total_coins = (idle_user.total_coins or 0) - charged_coins",
                      self.src)

    def test_the_announcement_drops_the_compensation_line(self):
        self.assertNotIn("🎁 Compensation:", self.src)
        self.assertIn("⚠️ Fine:", self.src)


if __name__ == "__main__":
    unittest.main()

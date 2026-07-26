"""A /lpbot or /ciplbot match must cost and count for nothing.

The promise to the player is blunt: practice against the bot records no career
stats, pays no coins or gems, and never touches Win/Loss, the streak or the
active-day counter. That promise rests on a single flag — ``stats_disabled`` —
which ``handlers.cipl_play.mark_bot_match`` sets and which the stat and reward
paths already honour. These tests pin both halves of that contract.
"""

import unittest
from unittest.mock import MagicMock

from services import player_stats_service, match_rewards


class MarkBotMatchTests(unittest.TestCase):
    def _marked(self, **extra):
        from handlers.cipl_play import mark_bot_match
        state = {"bat_team_id": 7, "bowl_team_id": 99}
        state.update(extra)
        return mark_bot_match(state, bot_user_id=99)

    def test_sets_the_unranked_gate(self):
        state = self._marked()
        self.assertTrue(state["is_bot_match"])
        self.assertTrue(state["stats_disabled"])
        self.assertTrue(state["unranked"])
        self.assertEqual(state["bot_user_id"], 99)

    def test_clears_every_competition_binding(self):
        # A practice match must never land in a tournament table or a tour
        # series, even if the draft it came from carried those ids.
        state = self._marked(tournament_id=5, cl_tour_id=3, cl_tour_match_id=4,
                             reserved_fixture_id=8,
                             tournament_team_by_user={1: 2})
        self.assertIsNone(state["tournament_id"])
        self.assertIsNone(state["cl_tour_id"])
        self.assertIsNone(state["cl_tour_match_id"])
        self.assertIsNone(state["reserved_fixture_id"])
        self.assertEqual(state["tournament_team_by_user"], {})

    def test_identifies_which_side_the_bot_is_on(self):
        from handlers.cipl_play import _bot_bats, _bot_bowls
        state = self._marked()
        self.assertTrue(_bot_bowls(state))
        self.assertFalse(_bot_bats(state))
        # At the innings break the sides swap, and so must the bot's role.
        state["bat_team_id"], state["bowl_team_id"] = (
            state["bowl_team_id"], state["bat_team_id"])
        self.assertTrue(_bot_bats(state))
        self.assertFalse(_bot_bowls(state))

    def test_a_normal_match_is_never_treated_as_a_bot_match(self):
        from handlers.cipl_play import _bot_bats, _bot_bowls
        state = {"bat_team_id": 7, "bowl_team_id": 8}
        self.assertFalse(_bot_bats(state))
        self.assertFalse(_bot_bowls(state))


class UnrankedGateTests(unittest.TestCase):
    def test_no_career_stats_are_persisted(self):
        # A bare MagicMock session would happily absorb any DB call, so the
        # assertion is that nothing is attempted at all.
        session = MagicMock()
        counts = player_stats_service.persist_player_game_stats(
            session, {"stats_disabled": True, "is_bot_match": True})
        self.assertEqual(counts.get("skipped"), "stats_disabled")
        self.assertEqual(counts.get("batting"), 0)
        self.assertEqual(counts.get("bowling"), 0)
        session.query.assert_not_called()

    def test_no_coins_gems_or_win_loss(self):
        session = MagicMock()
        payout = match_rewards.award_match_rewards_core(
            session, winner_user_id=1, loser_user_id=2, overs=20,
            count_result=False)
        self.assertEqual(payout, (0, 0, 0, 0))
        session.query.assert_not_called()

    def test_no_streak_or_active_day(self):
        session = MagicMock()
        match_rewards.record_match_result_stats(
            session, winner_user_id=1, loser_user_id=2, count_result=False)
        session.query.assert_not_called()


if __name__ == "__main__":
    unittest.main()

"""The bot's turns must drive themselves and then hand the board back.

This is the riskiest wiring in /lpbot and /ciplbot: the per-over prompt chain
(bowler → bowling approach → batting approach → simulate) now auto-answers
whichever links belong to the AI captain. Two things must hold every time:

  1. the chain always stops at the human's prompt — never loops, never runs the
     over before the human has picked;
  2. the bot never gets offered a keyboard, and no inactivity timer is armed on
     a turn the bot is going to answer instantly.

The real ``handlers.cipl_play`` functions are exercised against a fake state
store and a fake Telegram bot, so the wiring under test is the shipped code.
"""

import asyncio
import random
import unittest
from unittest.mock import AsyncMock, MagicMock

import handlers.cipl_play as cp
from services import cipl_match
from services.bot_xi_builder import (
    LPBOT_SHAPE, _assign_lpbot_traits, _lpbot_player_dict)


class FakePlayer:
    """The handful of ``Player`` attributes the engine dict builder reads."""

    _n = 0

    def __init__(self, category, rating):
        FakePlayer._n += 1
        self.id = FakePlayer._n
        self.name = f"{category[:3]}{FakePlayer._n}"
        self.rating = rating
        self.category = category
        self.bat_rating = rating if category != "Bowler" else rating - 25
        self.bowl_rating = (rating if category in ("Bowler", "All-rounder")
                            else rating - 40)
        self.bowl_style = "Fast"
        self.bowl_hand = "Right"
        self.bat_hand = "Right"


def _make_xi(seed_rating, rid_offset):
    rows = [FakePlayer(c, seed_rating - i) for c, n in LPBOT_SHAPE for i in range(n)]
    xi = [_lpbot_player_dict(p) for p in rows]
    _assign_lpbot_traits(xi)
    for i, p in enumerate(xi):
        p["roster_id"] = rid_offset + i
    return xi


class BotTurnFlowTests(unittest.TestCase):
    """Each test installs its own fakes and restores cipl_play afterwards."""

    BOT_UID = 99
    HUMAN_UID = 7

    def setUp(self):
        random.seed(11)
        self._saved = {name: getattr(cp, name) for name in
                       ("_gs", "_ss", "_get_next_action", "_miniapp_row",
                        "_arm_timer", "_cancel_timer", "BOT_THINK_DELAY")}
        self.store = {"state": None, "next": None}
        self.armed = []

        async def _gs(ctx, mid):
            return self.store["state"]

        async def _ss(ctx, mid, s, next_action=None, last_prompt_msg_id=None):
            self.store["state"] = s
            if next_action:
                self.store["next"] = next_action

        async def _get_next_action(ctx, mid):
            return self.store["next"]

        cp._gs, cp._ss, cp._get_next_action = _gs, _ss, _get_next_action
        cp._miniapp_row = lambda s: None
        cp._arm_timer = lambda ctx, mid, expected: self.armed.append(expected)
        cp._cancel_timer = lambda ctx, mid: None
        cp.BOT_THINK_DELAY = 0

        msg = MagicMock()
        msg.message_id = 1
        self.ctx = MagicMock()
        self.ctx.bot.send_message = AsyncMock(return_value=msg)
        self.ctx.bot.edit_message_text = AsyncMock(return_value=msg)
        self.ctx.bot.delete_message = AsyncMock()
        self.ctx.job_queue = None

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(cp, name, value)

    def _state(self, bot_bowls):
        """A live match state with the bot on one side."""
        bot_xi = _make_xi(80, 0)
        human_xi = _make_xi(78, 5000)
        if bot_bowls:
            bat_uid, bowl_uid = self.HUMAN_UID, self.BOT_UID
            bat_xi, bowl_xi = human_xi, bot_xi
            bat_tg, bowl_tg = self.HUMAN_UID, -1
        else:
            bat_uid, bowl_uid = self.BOT_UID, self.HUMAN_UID
            bat_xi, bowl_xi = bot_xi, human_xi
            bat_tg, bowl_tg = -1, self.HUMAN_UID
        state = cipl_match.build_cipl_state(
            match_id=42, overs=20, bat_user_id=bat_uid, bowl_user_id=bowl_uid,
            bat_user_tg=bat_tg, bowl_user_tg=bowl_tg,
            bat_xi=bat_xi, bowl_xi=bowl_xi,
            bat_team_name="Bat side", bowl_team_name="Bowl side",
            chat_id=1, pitch_type="Hard")
        cp.mark_bot_match(state, self.BOT_UID)
        state["user_names"] = {str(self.HUMAN_UID): "You", "-1": "🤖 Bot"}
        self.store["state"] = state
        return state

    def test_bot_bowling_turn_stops_at_the_humans_batting_prompt(self):
        state = self._state(bot_bowls=True)
        asyncio.run(cp._prompt_bowler(self.ctx, 42, state, first=True))
        result = self.store["state"]

        self.assertEqual(self.store["next"], cp.A_PICK_BAT_APPROACH)
        self.assertIsNotNone(result["current_bowler"])
        self.assertIn(result["bowling_approach"],
                      {k for k, _e, _l in cp.BOWLING_APPROACHES})
        # The over must NOT have been bowled — the human hasn't picked yet.
        self.assertEqual(result["current_over"], 1)
        self.assertEqual(cipl_match.balls_bowled(result), 0)
        # Only the human's turn is on the clock.
        self.assertEqual(self.armed, [cp.A_PICK_BAT_APPROACH])

    def test_bot_batting_turn_runs_the_over_and_returns_to_the_human(self):
        from services import bot_captain

        state = self._state(bot_bowls=False)
        # The human has already picked bowler + bowling approach.
        state["current_bowler"] = cipl_match.eligible_bowlers(state)[0]
        state["bowling_approach"] = "balanced"

        # ``simulate_over`` clears both approaches once the over completes, so
        # the bot's choice is captured as it is made rather than read afterwards.
        chosen = []
        real_pick = bot_captain.pick_batting_approach

        def spy(s):
            key = real_pick(s)
            chosen.append(key)
            return key

        bot_captain.pick_batting_approach = spy
        try:
            asyncio.run(cp._prompt_bat_approach(self.ctx, 42, state))
        finally:
            bot_captain.pick_batting_approach = real_pick
        result = self.store["state"]

        self.assertEqual(len(chosen), 1)
        self.assertIn(chosen[0], {k for k, _e, _l in cp.BATTING_APPROACHES})
        # The bot answered instantly, so the over was bowled and the board is
        # back with the human for the next over's bowler.
        self.assertEqual(cipl_match.balls_bowled(result), 6)
        self.assertEqual(self.store["next"], cp.A_PICK_CIPL_BOWLER)
        self.assertEqual(self.armed, [cp.A_PICK_CIPL_BOWLER])

    def test_the_bot_is_never_shown_a_keyboard(self):
        state = self._state(bot_bowls=True)
        asyncio.run(cp._prompt_bowler(self.ctx, 42, state, first=True))
        # Its "hands the ball to X" note carries no buttons; the only keyboard
        # in the exchange belongs to the human's batting-approach prompt.
        first_send = self.ctx.bot.send_message.call_args_list[0]
        self.assertIsNone(first_send.kwargs.get("reply_markup"))

    def test_over_summary_reports_the_plan_the_bot_actually_used(self):
        """A completed over clears both approaches on the state, so the summary
        must be built from the plan captured before the over was simulated —
        otherwise it always reports the neutral default."""
        from services import bot_captain

        state = self._state(bot_bowls=True)
        state["current_bowler"] = cipl_match.eligible_bowlers(state)[0]
        state["bowling_approach"] = "variation"
        state["batting_approach"] = "balanced"

        plan = cp._bot_plan_label(state)
        self.assertEqual(plan, bot_captain.bowling_label("variation"))

        summary = cipl_match.simulate_over(state)
        self.assertIsNone(state["bowling_approach"])   # cleared for the next over
        text = cp._render_over_summary(state, summary, bot_plan=plan)
        self.assertIn(bot_captain.bowling_label("variation"), text)
        self.assertNotIn(bot_captain.bowling_label("balanced"), text)

    def test_a_human_match_summary_has_no_bot_plan_line(self):
        state = self._state(bot_bowls=True)
        state["is_bot_match"] = False
        state["bowling_approach"] = "variation"
        self.assertIsNone(cp._bot_plan_label(state))

    def test_a_failed_bot_toss_launch_still_reaches_the_player(self):
        """On the bot-elects route the callback query was already answered during
        the coin flip, so `q.answer(...)` is rejected. Launch failures must fall
        back to a chat message instead of leaving a dead toss on screen."""
        session = MagicMock()
        session.query.return_value.get.return_value = None   # "Players no longer exist"
        q = MagicMock()
        # Telegram rejects a second answer on an already-answered query.
        q.answer = AsyncMock(side_effect=RuntimeError("query already answered"))
        q.edit_message_text = AsyncMock()
        draft = {"chat_id": 4242, "vs_bot": True,
                 "host_user_id": 1, "target_user_id": 2}

        real_get_session = cp.get_session
        cp.get_session = lambda: session
        try:
            asyncio.run(cp._launch_after_toss(self.ctx, q, draft, 7, "bat", "host"))
        finally:
            cp.get_session = real_get_session

        self.ctx.bot.send_message.assert_called_once()
        chat_id, text = self.ctx.bot.send_message.call_args.args[:2]
        self.assertEqual(chat_id, 4242)
        self.assertIn("Players no longer exist", text)
        # A failed launch must stay retryable.
        self.assertFalse(draft.get("launch_in_progress"))
        self.assertFalse(draft.get("match_launched"))

    def test_a_full_innings_never_breaks_a_bowling_rule(self):
        """End to end: the AI captain bowls out an innings legally."""
        state = self._state(bot_bowls=True)
        quota_breaches = back_to_back = 0
        while not cipl_match.is_innings_over(state):
            previous = state.get("prev_bowler_rid")
            asyncio.run(cp._prompt_bowler(self.ctx, 42, state, first=True))
            state = self.store["state"]
            bowler = state["current_bowler"]
            if previous is not None and bowler["roster_id"] == previous:
                back_to_back += 1
            if (cipl_match._overs_bowled(state, bowler["roster_id"])
                    >= cipl_match.quota_for(state, bowler)):
                quota_breaches += 1
            # Stand in for the human's batting-approach tap.
            state["batting_approach"] = "balanced"
            cipl_match.simulate_over(state)

        self.assertEqual(back_to_back, 0)
        self.assertEqual(quota_breaches, 0)
        self.assertEqual(cipl_match.balls_bowled(state), 120)


if __name__ == "__main__":
    unittest.main()

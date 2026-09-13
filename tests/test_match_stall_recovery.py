"""A match must never need a captain to type /rcl to keep going.

A Challenge League (/cipl) or Lets Play match moves through three picks per over,
each shown by a Telegram send and guarded by an in-memory inactivity timer. Two
things used to stop the match dead:

  • a send that raised — the callback had already cancelled the old clock, and
    the new one was only armed *after* the send, so the match was left with no
    buttons in the chat and nothing to move it on;
  • a process restart or an exception mid-over — the timer jobs live in memory,
    and the heartbeat deliberately skipped these matches, so nothing noticed.

Either way the match sat still until someone typed /rcl. These tests pin the
recovery: the clock is armed even when the prompt fails, a timeout on a prompt
nobody ever saw re-sends it instead of forfeiting, and the heartbeat resumes a
match that has lost its clock.
"""

import asyncio
import unittest
from datetime import datetime, timedelta
from unittest import mock

import handlers.cipl_play as cp
import services.match_heartbeat as hb
from services.match_state_store import (
    A_PICK_CIPL_BOWLER, A_PICK_BOWL_APPROACH, A_PICK_BAT_APPROACH, A_COMPLETED,
)


class _FakeJobQueue:
    """Records run_once jobs by name, the way PTB's queue would hold them."""

    def __init__(self):
        self.jobs = {}

    def run_once(self, callback, when, name=None, data=None):
        self.jobs.setdefault(name, []).append({"callback": callback,
                                               "when": when, "data": data})

    def get_jobs_by_name(self, name):
        return tuple(self.jobs.get(name, ()))

    def schedule_removal_all(self, name):
        self.jobs.pop(name, None)


class _Ctx:
    def __init__(self, job_queue=None):
        self.bot = mock.MagicMock()
        self.bot.send_message = mock.AsyncMock()
        self.bot.delete_message = mock.AsyncMock()
        self.bot.edit_message_text = mock.AsyncMock()
        self.bot_data = {}
        self.job_queue = job_queue


def _state(**over):
    s = {
        "match_id": 1,
        "chat_id": -100,
        "mode": "cipl_approach",
        "innings": 1,
        "current_over": 5,
        "bat_user_tg": 111,
        "bowl_user_tg": 222,
        "bat_team_name": "Team A",
        "bowl_team_name": "Team B",
        "user_names": {"111": "Ann", "222": "Bo"},
        "over_msg_ids": [],
        "action_msg_id": None,
        "current_bowler": {"roster_id": 9, "name": "Bowler", "bowl_rating": 80},
    }
    s.update(over)
    return s


class PromptFailureKeepsTheClockTests(unittest.TestCase):
    """A prompt whose send fails still leaves the match a way forward."""

    def _prompt(self, prompt, action, *, sender):
        ctx = _Ctx(job_queue=_FakeJobQueue())
        state = _state()
        saved = {}

        async def _ss(c, mid, s, next_action=None, **kw):
            saved["next_action"] = next_action
            saved["state"] = s

        async def _boom(*a, **k):
            raise RuntimeError("Telegram is having a moment")

        scheduled = []
        with mock.patch.object(cp, "_ss", _ss), \
                mock.patch.object(cp, sender, _boom), \
                mock.patch.object(cp, "_approach_card", lambda s: "card"), \
                mock.patch.object(cp, "_schedule_cipl_recovery",
                                  lambda c, mid, **k: scheduled.append(mid)), \
                mock.patch.object(cp.cipl_match, "eligible_bowlers",
                                  lambda s: [dict(s["current_bowler"])]), \
                mock.patch.object(cp.cipl_match, "is_part_time_bowler",
                                  lambda p: False), \
                mock.patch.object(cp.cipl_match, "overs_left", lambda s, p: 3), \
                mock.patch.object(cp.cipl_match, "display_rating",
                                  lambda p, k: 80):
            asyncio.run(prompt(ctx, 1, state))

        return ctx, state, saved, scheduled, action

    def _assert_recovered(self, ctx, state, saved, scheduled, action):
        # The state machine still advanced to the pick that is outstanding …
        self.assertEqual(saved["next_action"], action)
        # … the clock is armed even though the send failed …
        self.assertTrue(ctx.job_queue.get_jobs_by_name("cipl_to_1"))
        # … the failure is recorded, so the timeout re-prompts instead of
        # forfeiting a turn nobody ever saw …
        self.assertFalse(state["prompt_delivered"])
        # … and a retry is queued so nobody has to type /rcl.
        self.assertEqual(scheduled, [1])

    def test_bowler_prompt(self):
        self._assert_recovered(*self._prompt(
            cp._prompt_bowler, A_PICK_CIPL_BOWLER, sender="_new_action_message"))

    def test_bowling_approach_prompt(self):
        self._assert_recovered(*self._prompt(
            cp._prompt_bowl_approach, A_PICK_BOWL_APPROACH,
            sender="_edit_action_message"))

    def test_batting_approach_prompt(self):
        self._assert_recovered(*self._prompt(
            cp._prompt_bat_approach, A_PICK_BAT_APPROACH,
            sender="_edit_action_message"))

    def test_a_delivered_prompt_is_marked_delivered(self):
        ctx = _Ctx(job_queue=_FakeJobQueue())
        state = _state()

        async def _ss(c, mid, s, next_action=None, **kw):
            pass

        async def _ok(*a, **k):
            return None

        with mock.patch.object(cp, "_ss", _ss), \
                mock.patch.object(cp, "_edit_action_message", _ok), \
                mock.patch.object(cp, "_approach_card", lambda s: "card"):
            asyncio.run(cp._prompt_bat_approach(ctx, 1, state))

        self.assertTrue(state["prompt_delivered"])
        self.assertTrue(ctx.job_queue.get_jobs_by_name("cipl_to_1"))


class TimeoutOnAnUnseenPromptTests(unittest.TestCase):
    """The forfeit clock must only punish a turn the player could actually play."""

    def _timeout(self, state):
        ctx = _Ctx(job_queue=_FakeJobQueue())
        ctx.job = mock.MagicMock()
        ctx.job.data = {"mid": 1, "expected": A_PICK_BAT_APPROACH}
        calls = {"forfeit": 0, "resume": 0}

        async def _gs(c, mid):
            return state

        async def _next_action(c, mid):
            return A_PICK_BAT_APPROACH

        async def _forfeit(c, mid, s, expected):
            calls["forfeit"] += 1

        async def _resume(c, mid):
            calls["resume"] += 1
            return True

        with mock.patch.object(cp, "_gs", _gs), \
                mock.patch.object(cp, "_get_next_action", _next_action), \
                mock.patch.object(cp, "_forfeit_live_match", _forfeit), \
                mock.patch.object(cp, "_resume_locked", _resume), \
                mock.patch.object(cp, "_is_bot_match", lambda s: False):
            asyncio.run(cp._on_timeout(ctx))
        return calls

    def test_an_undelivered_prompt_is_re_sent_not_forfeited(self):
        calls = self._timeout(_state(prompt_delivered=False))
        self.assertEqual(calls["forfeit"], 0)
        self.assertEqual(calls["resume"], 1)

    def test_a_delivered_prompt_still_forfeits(self):
        calls = self._timeout(_state(prompt_delivered=True))
        self.assertEqual(calls["forfeit"], 1)
        self.assertEqual(calls["resume"], 0)

    def test_an_old_state_without_the_flag_still_forfeits(self):
        # States written before this flag existed must keep the old behaviour —
        # missing means "the prompt went out", not "never sent".
        calls = self._timeout(_state())
        self.assertEqual(calls["forfeit"], 1)

    def test_the_reminder_stays_quiet_when_no_picker_was_sent(self):
        ctx = _Ctx()
        ctx.job = mock.MagicMock()
        ctx.job.data = {"mid": 1, "expected": A_PICK_BAT_APPROACH}
        state = _state(prompt_delivered=False)

        async def _gs(c, mid):
            return state

        async def _next_action(c, mid):
            return A_PICK_BAT_APPROACH

        with mock.patch.object(cp, "_gs", _gs), \
                mock.patch.object(cp, "_get_next_action", _next_action):
            asyncio.run(cp._on_remind(ctx))

        ctx.bot.send_message.assert_not_awaited()


class FlowErrorRecoveryTests(unittest.TestCase):
    """An exception mid-over must not leave the match without a clock."""

    def test_recovery_rearms_the_clock_and_queues_a_retry(self):
        ctx = _Ctx(job_queue=_FakeJobQueue())
        state = _state()
        scheduled = []

        async def _gs(c, mid):
            return state

        async def _ss(c, mid, s, next_action=None, **kw):
            pass

        async def _next_action(c, mid):
            return A_PICK_BOWL_APPROACH

        with mock.patch.object(cp, "_gs", _gs), \
                mock.patch.object(cp, "_ss", _ss), \
                mock.patch.object(cp, "_get_next_action", _next_action), \
                mock.patch.object(cp, "_schedule_cipl_recovery",
                                  lambda c, mid, **k: scheduled.append(mid)):
            asyncio.run(cp._recover_from_error(ctx, 1, "over simulation"))

        self.assertFalse(state["prompt_delivered"])
        self.assertTrue(ctx.job_queue.get_jobs_by_name("cipl_to_1"))
        self.assertEqual(scheduled, [1])

    def test_a_finished_match_is_not_given_a_new_clock(self):
        ctx = _Ctx(job_queue=_FakeJobQueue())

        async def _gs(c, mid):
            return None

        async def _ss(c, mid, s, next_action=None, **kw):
            pass

        async def _next_action(c, mid):
            return A_COMPLETED

        with mock.patch.object(cp, "_gs", _gs), \
                mock.patch.object(cp, "_ss", _ss), \
                mock.patch.object(cp, "_get_next_action", _next_action), \
                mock.patch.object(cp, "_schedule_cipl_recovery",
                                  lambda c, mid, **k: None):
            asyncio.run(cp._recover_from_error(ctx, 1, "over simulation"))

        self.assertFalse(ctx.job_queue.get_jobs_by_name("cipl_to_1"))


class TimerArmedTests(unittest.TestCase):
    def test_reports_an_armed_clock(self):
        ctx = _Ctx(job_queue=_FakeJobQueue())
        cp._arm_timer(ctx, 7, A_PICK_CIPL_BOWLER)
        self.assertTrue(cp.timer_armed(ctx, 7))
        self.assertFalse(cp.timer_armed(ctx, 8))

    def test_no_job_queue_means_no_clock(self):
        self.assertFalse(cp.timer_armed(_Ctx(), 7))


class HeartbeatCiplRecoveryTests(unittest.TestCase):
    """The heartbeat does what the captains were typing /rcl for."""

    def _recover(self, ctx, armed, resumed=True):
        calls = {"resume": 0}

        async def _resume(c, mid):
            calls["resume"] += 1
            return resumed

        fake = mock.MagicMock()
        fake.timer_armed = lambda c, mid: armed
        fake.cipl_resume = _resume
        with mock.patch.dict("sys.modules", {"handlers.cipl_play": fake}):
            asyncio.run(hb._recover_cipl(ctx, 1))
        return calls

    def test_a_match_with_no_clock_is_resumed(self):
        calls = self._recover(_Ctx(), armed=False)
        self.assertEqual(calls["resume"], 1)

    def test_a_match_still_on_the_clock_is_left_alone(self):
        calls = self._recover(_Ctx(), armed=True)
        self.assertEqual(calls["resume"], 0)

    def test_resumes_are_spaced_out_when_there_is_no_clock_to_re_arm(self):
        ctx = _Ctx()
        self.assertEqual(self._recover(ctx, armed=False)["resume"], 1)
        # A second tick moments later must not re-prompt the same chat again.
        self.assertEqual(self._recover(ctx, armed=False)["resume"], 0)
        # Once the cooldown has passed it tries again.
        ctx.bot_data[hb._CIPL_RESUME_KEY.format(mid=1)] = (
            datetime.utcnow() - timedelta(seconds=hb.CIPL_RESUME_COOLDOWN + 1))
        self.assertEqual(self._recover(ctx, armed=False)["resume"], 1)


class HeartbeatTickRoutingTests(unittest.TestCase):
    """A stalled Challenge League match reaches the CIPL recovery, not the
    ball-by-ball renderer (which doesn't understand its actions)."""

    def test_cipl_state_routes_to_cipl_recovery(self):
        ctx = _Ctx()
        ctx.bot_data["ms_1"] = {"mode": "cipl_approach", "chat_id": -100}
        row = mock.MagicMock()
        row.match_id = 1
        row.next_action = A_PICK_BAT_APPROACH
        row.last_modified = datetime.utcnow() - timedelta(seconds=600)

        seen, rerendered, decided = [], [], []

        async def _recover(c, mid):
            seen.append(mid)

        async def _rerender(c, mid):
            rerendered.append(mid)

        async def _decide(c, mid, state, action):
            decided.append(mid)

        session = mock.MagicMock()
        session.query.return_value.all.return_value = [row]

        with mock.patch.object(hb, "_recover_cipl", _recover), \
                mock.patch.object(hb, "_try_rerender", _rerender), \
                mock.patch.object(hb, "_auto_decide", _decide), \
                mock.patch("services.match_heartbeat_flags.has_active_matches",
                           lambda c: True), \
                mock.patch("services.match_heartbeat_flags.has_due_notifications",
                           lambda: False), \
                mock.patch("services.match_heartbeat_flags.set_active_match_count",
                           lambda c, n: None), \
                mock.patch("database.get_session", lambda: session):
            asyncio.run(hb._heartbeat_tick(ctx))

        self.assertEqual(seen, [1])
        self.assertEqual(rerendered, [])
        self.assertEqual(decided, [])


if __name__ == "__main__":
    unittest.main()

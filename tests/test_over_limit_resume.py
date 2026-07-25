"""No over may be played past the format's limit.

Once 20 overs — or 100 balls (20 sets) in The Hundred — have been bowled, the
innings is done. Resuming (/rcl, /resume), a stale approach button, or an
end-of-innings handler that failed part-way must never bowl a 21st over: the
match moves to the innings break or the result instead.
"""

import asyncio
import contextlib
import importlib
import sys
import unittest
from unittest import mock

from services import cipl_match as cm
import handlers.cipl_play as cp


def _mk(rid, name, cat, bat, bowl, style="Fast"):
    return {"roster_id": rid, "player_id": rid, "name": name,
            "rating": max(bat, bowl), "category": cat, "bat_rating": bat,
            "bowl_rating": bowl, "bowl_style": style, "bowl_hand": "Right",
            "bat_hand": "Right"}


def _state(overs=20, ball_format="T20"):
    bat_xi = ([_mk(i, "Bat%d" % i, "Batsman", 80 - i, 30) for i in range(1, 8)]
              + [_mk(i, "AR%d" % i, "All-rounder", 60, 70, "Off spin")
                 for i in range(8, 12)])
    bowl_xi = ([_mk(100 + i, "Bwl%d" % i, "Bowler", 30, 80 - i, "Fast")
                for i in range(1, 7)]
               + [_mk(100 + i, "BAR%d" % i, "All-rounder", 60, 70, "Leg spin")
                  for i in range(7, 12)])
    s = cm.build_cipl_state(1, overs, 10, 20, 111, 222, bat_xi, bowl_xi,
                            "Team A", "Team B", -100, "Hard", False,
                            ball_format=ball_format)
    s["chat_id"] = -100
    s["mode"] = "cipl_approach"
    return s


class InningsQuotaTests(unittest.TestCase):
    def test_live_innings_is_not_flagged_used(self):
        s = _state()
        s["current_over"], s["current_ball"] = 12, 3
        self.assertFalse(cp._innings_quota_used(s))

    def test_twentieth_over_completed_uses_the_quota(self):
        s = _state()
        s["current_over"], s["current_ball"] = 21, 0  # 120 balls bowled
        self.assertTrue(cp._innings_quota_used(s))

    def test_hundred_stops_at_100_balls_not_120(self):
        s = _state(overs=20, ball_format="The100")
        s["current_over"], s["current_ball"] = 21, 0  # 20 sets = 100 balls
        self.assertTrue(cp._innings_quota_used(s))
        s["current_over"], s["current_ball"] = 20, 4  # 99 balls — still live
        self.assertFalse(cp._innings_quota_used(s))

    def test_all_out_uses_the_quota(self):
        s = _state()
        s["current_over"], s["current_ball"] = 8, 2
        s["total_wickets"] = 10
        self.assertTrue(cp._innings_quota_used(s))

    def test_malformed_state_is_treated_as_live(self):
        self.assertFalse(cp._innings_quota_used(None))
        self.assertFalse(cp._innings_quota_used({}))


class RunOverGuardTests(unittest.TestCase):
    """_run_over must refuse to simulate once the quota is used."""

    def _run(self, state):
        calls = {"simulated": 0, "break": 0, "complete": 0}

        def _sim(_s):
            calls["simulated"] += 1
            return {}

        async def _brk(ctx, mid, s):
            calls["break"] += 1

        async def _cmp(ctx, mid, s):
            calls["complete"] += 1

        with mock.patch.object(cm, "simulate_over", _sim), \
                mock.patch.object(cp, "_innings_break", _brk), \
                mock.patch.object(cp, "_complete_match", _cmp), \
                mock.patch.object(cp, "_cancel_timer", lambda *a, **k: None):
            asyncio.run(cp._run_over(mock.MagicMock(), 1, state))
        return calls

    def test_no_21st_over_after_a_full_first_innings(self):
        s = _state()
        s["current_over"], s["current_ball"] = 21, 0
        calls = self._run(s)
        self.assertEqual(calls["simulated"], 0)   # nothing bowled
        self.assertEqual(calls["break"], 1)       # innings break instead
        self.assertEqual(calls["complete"], 0)

    def test_no_extra_over_after_a_finished_chase(self):
        s = _state()
        s["innings"] = 2
        s["target"] = 100
        s["total_runs"] = 120
        s["current_over"], s["current_ball"] = 18, 2
        calls = self._run(s)
        self.assertEqual(calls["simulated"], 0)
        self.assertEqual(calls["complete"], 1)    # result posted instead
        self.assertEqual(calls["break"], 0)

    def test_no_21st_set_in_the_hundred(self):
        s = _state(ball_format="The100")
        s["current_over"], s["current_ball"] = 21, 0  # 100 balls bowled
        calls = self._run(s)
        self.assertEqual(calls["simulated"], 0)
        self.assertEqual(calls["break"], 1)


class ResumeGuardTests(unittest.TestCase):
    """/rcl must never re-prompt a pick for an innings that is already over."""

    def _resume(self, state, match_status=None):
        calls = {"bowler": 0, "bowl_app": 0, "bat_app": 0,
                 "break": 0, "complete": 0, "cleanup": 0, "messages": []}

        async def _gs(ctx, mid):
            return state

        async def _next_action(ctx, mid):
            # The pointer is stale: it still names the pick that was consumed by
            # the over that ended the innings.
            return cp.A_PICK_BAT_APPROACH

        async def _prompt_bowler(ctx, mid, s=None, first=False):
            calls["bowler"] += 1

        async def _prompt_bowl_app(ctx, mid, s, auto=False):
            calls["bowl_app"] += 1

        async def _prompt_bat_app(ctx, mid, s, auto=False):
            calls["bat_app"] += 1

        async def _brk(ctx, mid, s):
            calls["break"] += 1

        async def _cmp(ctx, mid, s):
            calls["complete"] += 1

        ctx = mock.MagicMock()
        ctx.bot_data = {}  # no live Super Over

        async def _send(chat_id, text, **kwargs):
            calls["messages"].append(text)

        ctx.bot.send_message = _send

        with mock.patch.object(cp, "_gs", _gs), \
                mock.patch.object(cp, "_get_next_action", _next_action), \
                mock.patch.object(cp, "_prompt_bowler", _prompt_bowler), \
                mock.patch.object(cp, "_prompt_bowl_approach", _prompt_bowl_app), \
                mock.patch.object(cp, "_prompt_bat_approach", _prompt_bat_app), \
                mock.patch.object(cp, "_innings_break", _brk), \
                mock.patch.object(cp, "_complete_match", _cmp), \
                mock.patch.object(cp, "_match_row_status", lambda mid: match_status), \
                mock.patch.object(cp, "cleanup_state",
                                  lambda *a, **k: calls.__setitem__(
                                      "cleanup", calls["cleanup"] + 1)), \
                mock.patch.object(cp, "release_match_lock", lambda mid: None):
            calls["result"] = asyncio.run(cp.cipl_resume(ctx, 1))
        return calls

    def test_resume_after_full_first_innings_runs_the_break(self):
        s = _state()
        s["current_over"], s["current_ball"] = 21, 0
        calls = self._resume(s)
        self.assertTrue(calls["result"])
        self.assertEqual(calls["break"], 1)
        # No pick is re-prompted — that is what used to start over 21.
        self.assertEqual(calls["bowler"] + calls["bowl_app"] + calls["bat_app"], 0)

    def test_resume_after_full_second_innings_posts_the_result(self):
        s = _state()
        s["innings"] = 2
        s["target"] = 200
        s["total_runs"] = 150
        s["current_over"], s["current_ball"] = 21, 0
        calls = self._resume(s, match_status="active")
        self.assertTrue(calls["result"])
        self.assertEqual(calls["complete"], 1)
        self.assertEqual(calls["bowler"] + calls["bowl_app"] + calls["bat_app"], 0)

    def test_resume_of_an_already_completed_match_only_clears_state(self):
        s = _state()
        s["innings"] = 2
        s["target"] = 200
        s["total_runs"] = 150
        s["current_over"], s["current_ball"] = 21, 0
        calls = self._resume(s, match_status="completed")
        self.assertTrue(calls["result"])
        # Never re-finalize: that would pay the match rewards a second time.
        self.assertEqual(calls["complete"], 0)
        self.assertEqual(calls["cleanup"], 1)
        self.assertTrue(any("already over" in m for m in calls["messages"]))

    def test_live_innings_still_resumes_its_outstanding_pick(self):
        s = _state()
        s["current_over"], s["current_ball"] = 14, 0
        s["current_bowler"] = s["bowl_xi"][0]
        calls = self._resume(s)
        self.assertTrue(calls["result"])
        self.assertEqual(calls["bat_app"], 1)     # the stalled pick reappears
        self.assertEqual(calls["break"] + calls["complete"], 0)


# Some sibling test modules install stub `telegram` / `database` / `models` /
# `handlers.match` modules in sys.modules and leave them there. Import the real
# handlers.match for the tests below regardless of the order tests run in, then
# put whatever was there back so nothing else changes behaviour.
_STUBBABLE = ("telegram", "telegram.ext", "database", "models", "handlers.match")


@contextlib.contextmanager
def real_handlers_match():
    existing = sys.modules.get("handlers.match")
    if existing is not None and hasattr(existing, "render_screen"):
        yield existing
        return
    saved = {name: sys.modules.get(name) for name in _STUBBABLE}
    for name in _STUBBABLE:
        sys.modules.pop(name, None)
    try:
        yield importlib.import_module("handlers.match")
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class BallByBallResumeGuardTests(unittest.TestCase):
    """/resume on a finished ball-by-ball match must not re-run the innings end.

    Re-running it would pay the match rewards (and re-announce the result) a
    second time, so a finished-but-uncleaned state is simply cleared.
    """

    def test_render_screen_clears_a_finalized_match(self):
        with real_handlers_match() as hm:
            self._assert_render_screen_clears(hm)

    def _assert_render_screen_clears(self, hm):
        state = {"chat_id": -100, "innings": 2, "overs": 20,
                 "current_over": 21, "current_ball": 0, "total_runs": 150,
                 "total_wickets": 4, "target": 200, "result_finalized": True}
        calls = {"end_innings": 0, "cleanup": 0}

        async def _end(ctx, mid):
            calls["end_innings"] += 1

        ctx = mock.MagicMock()
        ctx.bot_data = {}

        with mock.patch.object(hm, "_gs", lambda ctx, mid: state), \
                mock.patch.object(hm, "_end_innings", _end), \
                mock.patch.object(hm, "cleanup_state",
                                  lambda *a, **k: calls.__setitem__(
                                      "cleanup", calls["cleanup"] + 1)), \
                mock.patch.object(hm, "release_match_lock", lambda mid: None):
            rendered = asyncio.run(hm.render_screen(ctx, 1))

        self.assertFalse(rendered)
        self.assertEqual(calls["end_innings"], 0)
        self.assertEqual(calls["cleanup"], 1)

    def test_end_innings_is_not_run_twice(self):
        with real_handlers_match() as hm:
            self._assert_end_innings_is_a_no_op(hm)

    def _assert_end_innings_is_a_no_op(self, hm):
        state = {"chat_id": -100, "innings": 2, "overs": 20,
                 "current_over": 21, "current_ball": 0, "total_runs": 150,
                 "total_wickets": 4, "target": 200, "result_finalized": True}
        calls = {"cleanup": 0}

        ctx = mock.MagicMock()
        ctx.bot_data = {}

        with mock.patch.object(hm, "_gs", lambda ctx, mid: state), \
                mock.patch.object(hm, "_cancel_action_timer", lambda *a, **k: None), \
                mock.patch.object(hm, "cleanup_state",
                                  lambda *a, **k: calls.__setitem__(
                                      "cleanup", calls["cleanup"] + 1)), \
                mock.patch.object(hm, "release_match_lock", lambda mid: None):
            # Returns immediately without touching rewards / result messages,
            # which would raise on this stripped-down state if it went further.
            asyncio.run(hm._end_innings(ctx, 1))

        self.assertEqual(calls["cleanup"], 1)


if __name__ == "__main__":
    unittest.main()

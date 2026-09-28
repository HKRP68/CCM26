"""Over-by-over bot matches played from the Mini App, and the mid-over
new-batsman pick.

Covers:
  * services.cipl_match — an over that pauses on a wicket and resumes;
  * handlers.cipl_play.submit_pick — the one entry point the chat buttons and
    the Mini App share, the new-batsman prompt and its timeout;
  * services.bot_bridge — handing a pick from a request thread to the bot loop;
  * services.crickidex_arena / match_webapp_service — who may play from the
    Mini App and what the Arena is told about the over.
"""

import asyncio
import json
import random
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock

import handlers.cipl_play as cp
from services import cipl_match as cm
from services.match_state_store import (
    A_PICK_BAT_APPROACH, A_PICK_BOWL_APPROACH, A_PICK_CIPL_BOWLER,
    A_PICK_CIPL_NEW_BATSMAN)


def _mk(rid, name, cat, bat, bowl, style="Fast"):
    return {"roster_id": rid, "player_id": rid, "name": name,
            "rating": max(bat, bowl), "category": cat, "bat_rating": bat,
            "bowl_rating": bowl, "bowl_style": style, "bowl_hand": "Right",
            "bat_hand": "Right"}


def _xi(offset):
    return ([_mk(offset + i, "Bat%d" % i, "Batsman", 80 - i, 30) for i in range(1, 7)]
            + [_mk(offset + i, "Bwl%d" % i, "Bowler", 30, 82 - i) for i in range(7, 12)])


HUMAN_UID, BOT_UID = 7, 99
HUMAN_TG, BOT_TG = 7007, -1


def _state(human_bats=True, bot_match=True):
    if human_bats:
        bat_uid, bowl_uid, bat_tg, bowl_tg = HUMAN_UID, BOT_UID, HUMAN_TG, BOT_TG
    else:
        bat_uid, bowl_uid, bat_tg, bowl_tg = BOT_UID, HUMAN_UID, BOT_TG, HUMAN_TG
    s = cm.build_cipl_state(
        match_id=42, overs=20, bat_user_id=bat_uid, bowl_user_id=bowl_uid,
        bat_user_tg=bat_tg, bowl_user_tg=bowl_tg, bat_xi=_xi(100),
        bat_team_name="Bat side", bowl_team_name="Bowl side", bowl_xi=_xi(500),
        chat_id=1, pitch_type="Hard")
    if bot_match:
        cp.mark_bot_match(s, BOT_UID)
    return s


class _Script:
    """Replaces the ball engine with a fixed sequence of outcomes."""

    def __init__(self, marks):
        self.marks = list(marks)
        self._saved = None

    def __enter__(self):
        self._saved = cm.calculate_outcome

        def fake(**_kw):
            mark = self.marks.pop(0) if self.marks else "1"
            if mark == "W":
                return {"type": "wicket", "runs": 0, "how": "Bowled"}
            return {"type": "runs", "runs": int(mark)}

        cm.calculate_outcome = fake
        return self

    def __exit__(self, *exc):
        cm.calculate_outcome = self._saved


def _ready_to_bowl(s):
    s["current_bowler"] = cm.eligible_bowlers(s)[0]
    s["bowling_approach"] = "balanced"
    s["batting_approach"] = "balanced"
    return s


def _legal(timeline):
    return [t for t in timeline if t not in ("WD", "NB")]


# ════════════════════════════════════════════════════════════════════
# Engine
# ════════════════════════════════════════════════════════════════════

class PausableOverTests(unittest.TestCase):
    def test_a_wicket_parks_the_over_and_the_resume_finishes_it(self):
        s = _ready_to_bowl(_state())
        with _Script(["1", "W", "4", "0", "6", "1"]):
            first = cm.simulate_over(s, pause_on_wicket=True)
            self.assertTrue(first["paused"])
            self.assertEqual(first["over_timeline"], ["1", "W"])
            self.assertTrue(first["out_batsman"]["dismissal"])
            self.assertTrue(cm.needs_new_batsman(s))

            # The state survives the JSON round-trip the store puts it through.
            s = json.loads(json.dumps(s))
            pick = cm.available_batsmen(s)[-1]
            cm.bring_in_batsman(s, pick["roster_id"])
            self.assertFalse(cm.needs_new_batsman(s))
            self.assertEqual(s["batting_order"][s["striker_idx"]]["roster_id"],
                             pick["roster_id"])

            summary = cm.simulate_over(s, pause_on_wicket=True)
        self.assertNotIn("paused", summary)
        self.assertEqual(summary["over_timeline"], ["1", "W", "4", "0", "6", "1"])
        self.assertEqual(summary["over_runs"], 12)
        self.assertEqual(summary["over_wickets"], 1)
        self.assertEqual(cm.balls_bowled(s), 6)
        self.assertEqual(s["current_over"], 2)
        self.assertNotIn("over_in_progress", s)
        # The chosen batsman took the next slot; nobody was lost or doubled.
        self.assertEqual(s["batting_order"][2]["roster_id"], pick["roster_id"])
        ids = [p["roster_id"] for p in s["batting_order"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(s["next_batsman_idx"], 3)

    def test_without_the_flag_the_order_walks_in_as_before(self):
        s = _ready_to_bowl(_state())
        third = s["batting_order"][2]["roster_id"]
        with _Script(["1", "W", "4", "0", "6", "1"]):
            summary = cm.simulate_over(s)
        self.assertNotIn("paused", summary)
        self.assertEqual(len(_legal(summary["over_timeline"])), 6)
        self.assertIn(third, (s["batting_order"][s["striker_idx"]]["roster_id"],
                              s["batting_order"][s["non_striker_idx"]]["roster_id"]))

    def test_a_wicket_on_the_last_ball_still_lets_the_captain_pick(self):
        s = _ready_to_bowl(_state())
        with _Script(["1", "1", "1", "1", "1", "W"]):
            first = cm.simulate_over(s, pause_on_wicket=True)
            self.assertTrue(first["paused"])
            self.assertEqual(first["balls_left"], 0)
            cm.bring_in_batsman(s, cm.available_batsmen(s)[1]["roster_id"])
            summary = cm.simulate_over(s, pause_on_wicket=True)
        self.assertEqual(len(_legal(summary["over_timeline"])), 6)
        self.assertEqual(s["current_over"], 2)

    def test_no_pause_when_there_is_no_real_choice(self):
        s = _ready_to_bowl(_state())
        # Only one batsman left to come in: nothing to choose between.
        s["next_batsman_idx"] = len(s["batting_order"]) - 1
        with _Script(["W", "1", "1", "1", "1", "1"]):
            summary = cm.simulate_over(s, pause_on_wicket=True)
        self.assertNotIn("paused", summary)

    def test_a_resume_without_a_pick_never_lets_the_out_batsman_bat(self):
        s = _ready_to_bowl(_state())
        with _Script(["W", "1", "1", "1", "1", "1"]):
            cm.simulate_over(s, pause_on_wicket=True)
            out_rid = s["batting_order"][s["striker_idx"]]["roster_id"]
            cm.simulate_over(s, pause_on_wicket=True)   # no bring_in_batsman
        out_stats = s["bat_stats"][str(out_rid)]
        self.assertEqual(out_stats["balls"], 1)
        self.assertEqual(cm.balls_bowled(s), 6)

    def test_bring_in_batsman_refuses_someone_not_waiting(self):
        s = _ready_to_bowl(_state())
        with _Script(["W"]):
            cm.simulate_over(s, pause_on_wicket=True)
        opener = s["batting_order"][0]["roster_id"]
        self.assertIsNone(cm.bring_in_batsman(s, opener))

    def test_full_innings_with_picks_keeps_every_over_whole(self):
        for seed in range(6):
            random.seed(seed)
            s = _state()
            guard = 0
            while not cm.is_innings_over(s) and guard < 80:
                guard += 1
                s["current_bowler"] = cm.eligible_bowlers(s)[0]
                s["bowling_approach"] = "aggressive"
                s["batting_approach"] = "ultra"
                res = cm.simulate_over(s, pause_on_wicket=True)
                while res.get("paused"):
                    cm.bring_in_batsman(s, cm.available_batsmen(s)[-1]["roster_id"])
                    res = cm.simulate_over(s, pause_on_wicket=True)
                self.assertTrue(len(_legal(res["over_timeline"])) == 6
                                or cm.is_innings_over(s))


# ════════════════════════════════════════════════════════════════════
# Chat / shared pick flow
# ════════════════════════════════════════════════════════════════════

class SubmitPickFlowTests(unittest.TestCase):
    def setUp(self):
        self._saved = {name: getattr(cp, name) for name in
                       ("_gs", "_ss", "_get_next_action", "_miniapp_row",
                        "_arm_timer", "_cancel_timer", "BOT_THINK_DELAY",
                        "_super_over_active", "_flush_dirty_locked")}
        self.store = {"state": None, "next": None}
        self.armed = []
        cp._super_over_active = lambda ctx, mid: False
        cp._flush_dirty_locked = AsyncMock()

        async def _gs(ctx, mid):
            return self.store["state"]

        async def _ss(ctx, mid, s, next_action=None, last_prompt_msg_id=None,
                      **_kw):
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

    def _install(self, state, next_action):
        self.store["state"] = state
        self.store["next"] = next_action
        return state

    def _bat_ready(self, human_bats=True, bot_match=True):
        s = _state(human_bats=human_bats, bot_match=bot_match)
        s["current_bowler"] = cm.eligible_bowlers(s)[0]
        s["bowling_approach"] = "balanced"
        return self._install(s, A_PICK_BAT_APPROACH)

    def _bat_idx(self, key="balanced"):
        return [k for k, _e, _l in cp.BATTING_APPROACHES].index(key)

    def _sent_texts(self):
        calls = (self.ctx.bot.send_message.call_args_list
                 + self.ctx.bot.edit_message_text.call_args_list)
        out = []
        for c in calls:
            text = c.args[1] if len(c.args) > 1 else (c.args[0] if c.args else c.kwargs.get("text"))
            out.append((str(text), c.kwargs.get("reply_markup")))
        return out

    def test_a_human_wicket_pauses_for_the_pick_then_the_over_resumes(self):
        self._bat_ready()
        with _Script(["1", "W", "4", "0", "6", "1", "1", "1", "1", "1", "1", "1"]):
            ok, why = asyncio.run(cp.submit_pick(
                self.ctx, 42, HUMAN_TG, cp.PICK_BAT_APPROACH, self._bat_idx()))
            self.assertTrue(ok, why)
            s = self.store["state"]
            self.assertEqual(self.store["next"], A_PICK_CIPL_NEW_BATSMAN)
            self.assertTrue(cm.needs_new_batsman(s))
            self.assertIn(A_PICK_CIPL_NEW_BATSMAN, self.armed)
            # The chat got a "who walks in?" keyboard of the waiting batsmen.
            prompts = [(t, kb) for t, kb in self._sent_texts() if "who walks in" in t]
            self.assertTrue(prompts)
            buttons = [b.callback_data for row in prompts[-1][1].inline_keyboard
                       for b in row if b.callback_data]
            waiting = cm.available_batsmen(s)
            self.assertEqual(buttons, [f"cipl_newbat_42_{p['roster_id']}" for p in waiting])

            pick = waiting[-1]["roster_id"]
            ok, why = asyncio.run(cp.submit_pick(
                self.ctx, 42, HUMAN_TG, cp.PICK_NEW_BATSMAN, pick))
            self.assertTrue(ok, why)
        s = self.store["state"]
        self.assertFalse(cm.awaiting_new_batsman(s))
        self.assertGreaterEqual(cm.balls_bowled(s), 6)
        self.assertEqual(s["batting_order"][2]["roster_id"], pick)
        # The bot bowls the next over and hands the batting pick back.
        self.assertEqual(self.store["next"], A_PICK_BAT_APPROACH)

    def test_the_bot_batting_never_pauses(self):
        s = _state(human_bats=False)
        s["current_bowler"] = cm.eligible_bowlers(s)[0]
        s["bowling_approach"] = "balanced"
        self._install(s, A_PICK_BOWL_APPROACH)
        with _Script(["1", "W", "4", "0", "6", "1"]):
            asyncio.run(cp._prompt_bat_approach(self.ctx, 42, s))
        self.assertFalse(cm.awaiting_new_batsman(self.store["state"]))
        self.assertEqual(self.store["next"], A_PICK_CIPL_BOWLER)
        self.assertNotIn(A_PICK_CIPL_NEW_BATSMAN, self.armed)

    def test_a_human_vs_human_match_keeps_the_batting_order(self):
        self._bat_ready(bot_match=False)
        with _Script(["1", "W", "4", "0", "6", "1"]):
            ok, _ = asyncio.run(cp.submit_pick(
                self.ctx, 42, HUMAN_TG, cp.PICK_BAT_APPROACH, self._bat_idx()))
        self.assertTrue(ok)
        self.assertFalse(cm.awaiting_new_batsman(self.store["state"]))
        self.assertEqual(self.store["next"], A_PICK_CIPL_BOWLER)

    def test_picks_are_validated(self):
        self._bat_ready()
        # Wrong captain.
        ok, why = asyncio.run(cp.submit_pick(
            self.ctx, 42, 123456, cp.PICK_BAT_APPROACH, self._bat_idx()))
        self.assertFalse(ok)
        self.assertIn("batting captain", why)
        # Wrong phase.
        ok, why = asyncio.run(cp.submit_pick(
            self.ctx, 42, HUMAN_TG, cp.PICK_NEW_BATSMAN, 101))
        self.assertFalse(ok)
        # Out-of-range approach.
        ok, why = asyncio.run(cp.submit_pick(
            self.ctx, 42, HUMAN_TG, cp.PICK_BAT_APPROACH, 99))
        self.assertFalse(ok)
        self.assertEqual(why, "Invalid approach.")
        # Garbage value / kind.
        self.assertFalse(asyncio.run(cp.submit_pick(
            self.ctx, 42, HUMAN_TG, cp.PICK_BAT_APPROACH, "x"))[0])
        self.assertFalse(asyncio.run(cp.submit_pick(
            self.ctx, 42, HUMAN_TG, "nope", 1))[0])
        # Nothing moved.
        self.assertEqual(cm.balls_bowled(self.store["state"]), 0)

    def test_an_unavailable_batsman_is_refused(self):
        self._bat_ready()
        with _Script(["W"] + ["1"] * 12):
            asyncio.run(cp.submit_pick(
                self.ctx, 42, HUMAN_TG, cp.PICK_BAT_APPROACH, self._bat_idx()))
            s = self.store["state"]
            non_striker = s["batting_order"][s["non_striker_idx"]]["roster_id"]
            ok, why = asyncio.run(cp.submit_pick(
                self.ctx, 42, HUMAN_TG, cp.PICK_NEW_BATSMAN, non_striker))
        self.assertFalse(ok)
        self.assertIn("isn't available", why)
        self.assertTrue(cm.needs_new_batsman(self.store["state"]))

    def test_the_accept_hook_runs_before_the_over(self):
        self._bat_ready()
        seen = []

        async def accept():
            seen.append(cm.balls_bowled(self.store["state"]))

        with _Script(["1"] * 12):
            ok, _ = asyncio.run(cp.submit_pick(
                self.ctx, 42, HUMAN_TG, cp.PICK_BAT_APPROACH, self._bat_idx(),
                on_accept=accept))
        self.assertTrue(ok)
        self.assertEqual(seen, [0])

    def test_the_timeout_sends_in_the_next_batsman_and_plays_on(self):
        self._bat_ready()
        with _Script(["W"] + ["1"] * 12):
            asyncio.run(cp.submit_pick(
                self.ctx, 42, HUMAN_TG, cp.PICK_BAT_APPROACH, self._bat_idx()))
            s = self.store["state"]
            s["prompt_delivered"] = True
            expected_next = cm.available_batsmen(s)[0]["roster_id"]
            self.ctx.job = MagicMock()
            self.ctx.job.data = {"mid": 42, "expected": A_PICK_CIPL_NEW_BATSMAN}
            asyncio.run(cp._on_timeout(self.ctx))
        s = self.store["state"]
        self.assertFalse(cm.awaiting_new_batsman(s))
        self.assertEqual(s["batting_order"][2]["roster_id"], expected_next)
        self.assertGreaterEqual(cm.balls_bowled(s), 6)
        # Not closed: the match moved on to the next over's pick.
        self.assertEqual(self.store["next"], A_PICK_BAT_APPROACH)

    def test_resume_re_asks_for_the_batsman(self):
        self._bat_ready()
        with _Script(["W"] + ["1"] * 12):
            asyncio.run(cp.submit_pick(
                self.ctx, 42, HUMAN_TG, cp.PICK_BAT_APPROACH, self._bat_idx()))
        self.ctx.bot.send_message.reset_mock()
        self.ctx.bot.edit_message_text.reset_mock()
        self.assertTrue(asyncio.run(cp._resume_locked(self.ctx, 42)))
        self.assertTrue(any("who walks in" in t for t, _ in self._sent_texts()))
        self.assertTrue(cm.needs_new_batsman(self.store["state"]))

    def test_bot_matches_offer_play_in_mini_app(self):
        s = _state()
        cp._miniapp_row = self._saved["_miniapp_row"]
        import services.match_broadcast as mb
        saved = mb.play_match_keyboard
        seen = {}

        def fake_kb(mid, **kw):
            seen.update(kw)
            return None

        mb.play_match_keyboard = fake_kb
        try:
            cp._miniapp_row(s)
            self.assertEqual(seen["label"], "🎮 Play in Mini App")
            s.pop("is_bot_match")
            cp._miniapp_row(s)
            self.assertEqual(seen["label"], "📊 View Match")
        finally:
            mb.play_match_keyboard = saved


# ════════════════════════════════════════════════════════════════════
# Mini App side
# ════════════════════════════════════════════════════════════════════

class MiniAppAccessTests(unittest.TestCase):
    def test_only_bot_approach_matches_are_playable(self):
        from services.match_webapp_service import is_approach_match, is_view_only_match
        bot = _state()
        pvp = _state(bot_match=False)
        self.assertTrue(is_approach_match(bot))
        self.assertTrue(is_approach_match(pvp))
        self.assertFalse(is_view_only_match(bot))
        self.assertTrue(is_view_only_match(pvp))
        self.assertFalse(is_approach_match({"mode": "classic"}))

    def test_ball_by_ball_actions_still_refuse_approach_matches(self):
        from services import match_webapp_service as mws
        s = _state()
        saved = mws.mwa.get_state
        mws.mwa.get_state = lambda mid: s
        try:
            ok, _msg = mws.set_delivery(42, HUMAN_UID, "Yorker")
            self.assertFalse(ok)
            ok, _msg = mws.play_shot(42, HUMAN_UID, 0)
            self.assertFalse(ok)
        finally:
            mws.mwa.get_state = saved

    def test_turn_mapping_covers_the_approach_picks(self):
        from services.match_webapp_service import turn_state_name, whose_turn
        s = _state()   # human bats, bot bowls
        self.assertEqual(turn_state_name(A_PICK_BAT_APPROACH), "batting_approach")
        self.assertEqual(turn_state_name(A_PICK_BOWL_APPROACH), "bowling_approach")
        self.assertEqual(turn_state_name(A_PICK_CIPL_BOWLER), "selecting_over_bowler")
        self.assertEqual(turn_state_name(A_PICK_CIPL_NEW_BATSMAN), "selecting_wicket_batsman")
        self.assertEqual(whose_turn(s, A_PICK_BAT_APPROACH, HUMAN_UID), ("batsman", True))
        self.assertEqual(whose_turn(s, A_PICK_CIPL_NEW_BATSMAN, HUMAN_UID), ("batsman", True))
        self.assertEqual(whose_turn(s, A_PICK_BOWL_APPROACH, HUMAN_UID), ("bowler", False))

    def test_approach_payload_for_each_pick(self):
        from services.crickidex_arena import _approach_payload
        s = _state(human_bats=False)       # human bowls
        p = _approach_payload(s, A_PICK_CIPL_BOWLER, "bowling")
        self.assertEqual(len(p["battingOptions"]), 5)
        self.assertTrue(all(o["desc"] for o in p["bowlingOptions"]))
        self.assertEqual({b["roster_id"] for b in p["overBowlers"]},
                         {b["roster_id"] for b in cm.eligible_bowlers(s)})
        self.assertIn("oversLeft", p["overBowlers"][0])
        self.assertIsNone(p["lastOver"])
        self.assertTrue(p["isBotMatch"])

        # An over bowled → the playback payload reveals both plans.
        s = _ready_to_bowl(_state())
        s["bowling_approach"] = "variation"
        with _Script(["1", "4", "0", "6", "1", "1"]):
            cm.simulate_over(s)
        lo = _approach_payload(s, A_PICK_BAT_APPROACH, "batting")["lastOver"]
        self.assertEqual(lo["timeline"], ["1", "4", "0", "6", "1", "1"])
        self.assertEqual(len(lo["balls"]), 6)
        self.assertEqual(lo["runs"], 13)
        self.assertEqual(lo["bowlingApproach"]["key"], "variation")
        self.assertEqual(lo["key"], "1-1")
        self.assertFalse(lo["botBatted"])

        # A paused over → the new-batsman sheet.
        s = _ready_to_bowl(_state())
        with _Script(["1", "W"]):
            cm.simulate_over(s, pause_on_wicket=True)
        p = _approach_payload(s, A_PICK_CIPL_NEW_BATSMAN, "batting")
        self.assertEqual(p["overInProgress"]["timeline"], ["1", "W"])
        self.assertEqual(len(p["overInProgress"]["balls"]), 2)
        self.assertEqual([b["roster_id"] for b in p["incomingBatsmen"]],
                         [b["roster_id"] for b in cm.available_batsmen(s)])
        self.assertEqual(p["outBatsman"]["name"],
                         s["batting_order"][s["striker_idx"]]["name"])


class BotBridgeTests(unittest.TestCase):
    def setUp(self):
        from services import bot_bridge
        self.bridge = bot_bridge
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        app = MagicMock()
        app.bot_data = {}
        bot_bridge.configure(app, self.loop)

    def tearDown(self):
        self.bridge.configure(None, None)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)
        self.loop.close()

    def test_returns_on_accept_while_the_step_keeps_running(self):
        done = threading.Event()

        async def step(ctx, accept):
            self.assertEqual(ctx.bot_data, {})
            await accept()
            await asyncio.sleep(0.2)
            done.set()
            return True, None

        self.assertEqual(self.bridge.submit_and_wait_for_accept(step), (True, None))
        self.assertFalse(done.is_set())
        self.assertTrue(done.wait(2))

    def test_a_refusal_comes_back(self):
        async def step(ctx, accept):
            return False, "Already chosen."

        self.assertEqual(self.bridge.submit_and_wait_for_accept(step),
                         (False, "Already chosen."))

    def test_unwired_bridge_refuses(self):
        self.bridge.configure(None, None)

        async def step(ctx, accept):
            return True, None

        ok, why = self.bridge.submit_and_wait_for_accept(step)
        self.assertFalse(ok)
        self.assertTrue(why)


class ApproachPickValueTests(unittest.TestCase):
    def test_maps_keys_and_roster_ids(self):
        from services.match_webapp_service import (
            APPROACH_ACTION_TYPES, approach_pick_value as _approach_pick_value)
        self.assertEqual(set(APPROACH_ACTION_TYPES.values()),
                         {cp.PICK_BOWLER, cp.PICK_BOWL_APPROACH,
                          cp.PICK_BAT_APPROACH, cp.PICK_NEW_BATSMAN})
        self.assertEqual(_approach_pick_value("bat_approach", {"key": "ultra"}), 4)
        self.assertEqual(_approach_pick_value("bowl_approach", {"key": "Variation"}), 4)
        self.assertIsNone(_approach_pick_value("bowl_approach", {"key": "ultra"}))
        self.assertEqual(_approach_pick_value("bowler", {"rosterId": "501"}), 501)
        self.assertIsNone(_approach_pick_value("new_batsman", {}))


if __name__ == "__main__":
    unittest.main()

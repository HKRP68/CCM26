"""Regression cover for the /vsbot reliability + latency fixes.

Three problems drove these changes:

1. Players needed /resume "most of the time". Two causes: an AI turn that died
   left the match with no button to press until the 90s heartbeat noticed, and
   the innings break pointed ``next_action`` at PICK_DELIVERY while it was
   actually waiting on a human opener/bowler pick — so /resume bowled a phantom
   ball with the previous innings' bowler instead of re-showing the picker.
2. Roster-keyed stat maps lost their keys through JSON, so a reloaded match
   forgot who was out and showed every batter as 0(0).
3. Every AI pick cost its own Telegram message and every ball cost two DB
   commits, which is most of why an over felt slow.
"""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest import mock

import handlers.match as hm
import handlers.vsbot as hv
import services.bot_ai as bot_ai
import services.match_state_store as store


def _p(rid, name, bowl_rating=50, bat_rating=60):
    return {"roster_id": rid, "player_id": rid, "name": name,
            "rating": max(bat_rating, bowl_rating), "category": "Batsman",
            "bat_rating": bat_rating, "bowl_rating": bowl_rating,
            "bowl_style": "Fast", "bowl_hand": "Right", "bat_hand": "Right"}


class _Ctx:
    def __init__(self):
        self.bot_data = {}


# ════════════════════════════════════════════════════════════════════
# 1. Roster-id keys survive the JSON round-trip
# ════════════════════════════════════════════════════════════════════

class StatKeyNormalizationTests(unittest.TestCase):
    def test_int_keys_become_strings_on_deserialize(self):
        raw = json.dumps({"bat_stats": {"12": {"runs": 40, "out": True}},
                          "bowl_stats": {"7": {"overs_done": 2}}})
        state = store._deserialize(raw)
        self.assertEqual(set(state["bat_stats"]), {"12"})
        self.assertEqual(set(state["bowl_stats"]), {"7"})

    def test_int_and_str_twins_merge_keeping_the_busier_row(self):
        # A state written before keys were canonicalized can hold both forms:
        # one carries the real innings, the other is a leftover zeroed row.
        state = store._normalize_state({
            "bat_stats": {
                12: {"runs": 0, "balls": 0, "out": False},
                "12": {"runs": 41, "balls": 33, "out": True},
            },
        })
        self.assertEqual(list(state["bat_stats"]), ["12"])
        self.assertEqual(state["bat_stats"]["12"]["runs"], 41)
        self.assertTrue(state["bat_stats"]["12"]["out"])

    def test_merge_keeps_the_busier_row_whichever_key_holds_it(self):
        state = store._normalize_state({
            "bat_stats": {
                "9": {"runs": 0, "balls": 0, "out": False},
                9: {"runs": 12, "balls": 8, "out": False},
            },
        })
        self.assertEqual(state["bat_stats"]["9"]["runs"], 12)

    def test_non_numeric_keys_are_left_alone(self):
        state = store._normalize_state({"bat_stats": {"total": {"runs": 3}}})
        self.assertEqual(state["bat_stats"], {"total": {"runs": 3}})

    def test_save_state_canonicalizes_in_place(self):
        # The in-memory tier must agree with the DB, or the next cold read
        # changes key form underneath the readers.
        ctx = _Ctx()
        state = {"bat_stats": {5: {"runs": 1}}, "bowl_stats": {6: {"balls": 3}}}
        rows = []

        class _Q:
            def filter(self, *a):
                return self

            def first(self):
                return None

        session = SimpleNamespace(
            query=lambda model: _Q(), add=rows.append,
            commit=lambda: None, rollback=lambda: None, close=lambda: None)
        with mock.patch.object(store, "get_session", lambda: session):
            store.save_state(ctx, 1, state)

        self.assertEqual(list(state["bat_stats"]), ["5"])
        self.assertEqual(list(state["bowl_stats"]), ["6"])
        # And the copy handed to the DB carries the same canonical keys.
        persisted = json.loads(rows[0].state_json)
        self.assertEqual(list(persisted["bat_stats"]), ["5"])
        # The cached in-memory tier is the very dict we normalized.
        self.assertIs(ctx.bot_data["ms_1"], state)


class StatAccessorTests(unittest.TestCase):
    def test_stats_get_reads_either_key_form(self):
        self.assertEqual(hm._stats_get({"3": {"runs": 9}}, 3)["runs"], 9)
        self.assertEqual(hm._stats_get({3: {"runs": 9}}, 3)["runs"], 9)

    def test_stats_get_missing_returns_empty_dict(self):
        self.assertEqual(hm._stats_get({"3": {}}, 4), {})
        self.assertEqual(hm._stats_get(None, 4), {})

    def test_stats_row_finds_the_string_keyed_row_instead_of_re_creating_it(self):
        stats = {"3": {"runs": 25, "balls": 20}}
        row = hm._stats_row(stats, 3, {"runs": 0, "balls": 0})
        self.assertEqual(row["runs"], 25)
        self.assertEqual(list(stats), ["3"])  # no int-keyed twin

    def test_stats_row_rekeys_a_legacy_int_row_rather_than_zeroing_it(self):
        stats = {3: {"runs": 25, "balls": 20}}
        row = hm._stats_row(stats, 3, {"runs": 0, "balls": 0})
        self.assertEqual(row["runs"], 25)
        self.assertEqual(list(stats), ["3"])

    def test_stats_row_inserts_a_blank_under_the_string_key(self):
        stats = {}
        row = hm._stats_row(stats, 3, {"runs": 0})
        row["runs"] = 4
        self.assertEqual(stats, {"3": {"runs": 4}})


class BotNextBatsmanTests(unittest.TestCase):
    def test_dismissed_batter_is_not_sent_back_in(self):
        order = [_p(1, "A"), _p(2, "B"), _p(3, "C"), _p(4, "D")]
        # String keys are what a reloaded state carries.
        bat_stats = {"3": {"out": True}, "4": {"out": False}}
        idx, player = bot_ai.pick_bot_next_batsman(order, 0, 1, bat_stats)
        self.assertEqual(player["name"], "D")
        self.assertEqual(idx, 3)

    def test_int_keyed_stats_still_work(self):
        order = [_p(1, "A"), _p(2, "B"), _p(3, "C"), _p(4, "D")]
        idx, player = bot_ai.pick_bot_next_batsman(
            order, 0, 1, {3: {"out": True}})
        self.assertEqual(player["name"], "D")

    def test_all_out_returns_nothing(self):
        order = [_p(1, "A"), _p(2, "B"), _p(3, "C")]
        idx, player = bot_ai.pick_bot_next_batsman(
            order, 0, 1, {"3": {"out": True}})
        self.assertIsNone(idx)
        self.assertIsNone(player)


# ════════════════════════════════════════════════════════════════════
# 2. AI announcements ride on the next prompt (one send, not three)
# ════════════════════════════════════════════════════════════════════

class AiNoteTests(unittest.TestCase):
    def test_queued_lines_are_taken_once_then_cleared(self):
        s = {}
        hm._queue_ai_note(s, "🤖 New bowler: X")
        hm._queue_ai_note(s, "🤖 X: Yorker")
        prefix = hm._take_ai_note(s)
        self.assertEqual(prefix, "🤖 New bowler: X\n🤖 X: Yorker\n\n")
        self.assertEqual(hm._take_ai_note(s), "")

    def test_no_notes_adds_no_prefix(self):
        self.assertEqual(hm._take_ai_note({}), "")

    def test_duplicate_lines_are_not_repeated(self):
        s = {}
        hm._queue_ai_note(s, "same")
        hm._queue_ai_note(s, "same")
        self.assertEqual(hm._take_ai_note(s), "same\n\n")

    def test_note_list_is_capped(self):
        s = {}
        for i in range(hm.AI_NOTE_MAX_LINES + 5):
            hm._queue_ai_note(s, f"line {i}")
        self.assertEqual(len(s["ai_note"]), hm.AI_NOTE_MAX_LINES)
        # The most recent beats are the ones kept.
        self.assertIn(f"line {hm.AI_NOTE_MAX_LINES + 4}", s["ai_note"])

    def test_spectator_match_sends_the_line_instead_of_queueing_it(self):
        # Nothing prompts a player in bot-vs-bot, so a queued note would never
        # be seen at all.
        sent = []

        class _Bot:
            async def send_message(self, chat_id, text, parse_mode=None):
                sent.append(text)

        ctx = SimpleNamespace(bot=_Bot(), bot_data={})
        s = {"bat_user_tg": hv.BOT_TG_ID, "bowl_user_tg": hv.BOT_TG_ID,
             "chat_id": 5}
        asyncio.run(hv._ai_say(ctx, s, "🤖 New bowler: X"))
        self.assertEqual(sent, ["🤖 New bowler: X"])
        self.assertNotIn("ai_note", s)

    def test_normal_match_queues_the_line_for_the_prompt(self):
        ctx = SimpleNamespace(bot=None, bot_data={})
        s = {"bat_user_tg": 999, "bowl_user_tg": hv.BOT_TG_ID, "chat_id": 5}
        asyncio.run(hv._ai_say(ctx, s, "🤖 X: Yorker"))
        self.assertEqual(s["ai_note"], ["🤖 X: Yorker"])


# ════════════════════════════════════════════════════════════════════
# 3. AI-turn watchdog
# ════════════════════════════════════════════════════════════════════

class BotOwnershipTests(unittest.TestCase):
    def _state(self, bat_is_bot, bowl_is_bot):
        return {"bat_user_tg": hv.BOT_TG_ID if bat_is_bot else 42,
                "bowl_user_tg": hv.BOT_TG_ID if bowl_is_bot else 42}

    def test_bowling_actions_belong_to_the_bowling_side(self):
        s = self._state(bat_is_bot=False, bowl_is_bot=True)
        for act in (hm.A_PICK_DELIVERY, hm.A_PICK_LENGTH, hm.A_PICK_NEW_BOWLER):
            self.assertTrue(hv._bot_owes_action(s, act), act)
        for act in (hm.A_PICK_SHOT, hm.A_PICK_NEW_BATSMAN):
            self.assertFalse(hv._bot_owes_action(s, act), act)

    def test_batting_actions_belong_to_the_batting_side(self):
        s = self._state(bat_is_bot=True, bowl_is_bot=False)
        for act in (hm.A_PICK_SHOT, hm.A_PICK_NEW_BATSMAN):
            self.assertTrue(hv._bot_owes_action(s, act), act)
        for act in (hm.A_PICK_DELIVERY, hm.A_PICK_NEW_BOWLER):
            self.assertFalse(hv._bot_owes_action(s, act), act)

    def test_setup_and_terminal_actions_are_never_the_bots(self):
        s = self._state(bat_is_bot=True, bowl_is_bot=True)
        for act in (hm.A_PICK_OPENERS, hm.A_PICK_OPENING_BOWLER,
                    hm.A_INNINGS_BREAK, hm.A_COMPLETED, None):
            self.assertFalse(hv._bot_owes_action(s, act), act)


class BallSignatureTests(unittest.TestCase):
    def test_a_legal_ball_changes_the_signature(self):
        before = hv._ball_signature({"innings": 1, "current_over": 1,
                                     "current_ball": 0, "total_runs": 0,
                                     "total_wickets": 0})
        after = hv._ball_signature({"innings": 1, "current_over": 1,
                                    "current_ball": 1, "total_runs": 0,
                                    "total_wickets": 0})
        self.assertNotEqual(before, after)

    def test_a_wide_changes_the_signature_even_though_the_ball_count_holds(self):
        # A wide doesn't advance current_ball, so runs are what distinguishes it
        # — without that the watchdog would call a re-bowled wide a stall.
        before = hv._ball_signature({"innings": 1, "current_over": 1,
                                     "current_ball": 3, "total_runs": 20,
                                     "total_wickets": 1})
        after = hv._ball_signature({"innings": 1, "current_over": 1,
                                    "current_ball": 3, "total_runs": 21,
                                    "total_wickets": 1})
        self.assertNotEqual(before, after)


class WatchdogTests(unittest.TestCase):
    """The watchdog is what removes the need for /resume on an AI turn."""

    def setUp(self):
        # Zero delay: the point under test is the decision, not the wait.
        patcher = mock.patch.object(hv, "BOT_WATCHDOG_DELAY", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.rendered = []
        self.sent = []

        async def _render(ctx, mid):
            self.rendered.append(mid)

        class _Bot:
            outer = self

            async def send_message(self, chat_id, text, parse_mode=None):
                WatchdogTests.instance.sent.append(text)

        WatchdogTests.instance = self
        self.render = _render
        self.ctx = SimpleNamespace(bot=_Bot(), bot_data={})

    def _run(self, state_seq, next_action):
        """Arm the watchdog, then let it fire. ``state_seq`` supplies the state
        for the arming read and then for the check."""
        states = list(state_seq)

        def _gs(ctx, mid):
            return states[0] if len(states) == 1 else states.pop(0)

        with mock.patch.object(hm, "_gs", _gs), \
             mock.patch.object(hm, "render_screen", self.render), \
             mock.patch.object(store, "get_next_action",
                               lambda ctx, mid: next_action):
            async def _go():
                task = hv._arm_bot_watchdog(self.ctx, 7)
                if task:
                    await task
            asyncio.run(_go())

    def _live(self, **over):
        s = {"is_vsbot": True, "chat_id": 3, "innings": 1, "current_over": 1,
             "current_ball": 0, "total_runs": 0, "total_wickets": 0,
             "bat_user_tg": 42, "bowl_user_tg": hv.BOT_TG_ID}
        s.update(over)
        return s

    def test_stalled_ai_turn_is_re_dispatched(self):
        self._run([self._live(), self._live()], hm.A_PICK_DELIVERY)
        self.assertEqual(self.rendered, [7])
        self.assertEqual(self.ctx.bot_data["vsbot_nudges_7"], 1)

    def test_a_ball_that_went_through_is_left_alone(self):
        self._run([self._live(), self._live(current_ball=1)],
                  hm.A_PICK_DELIVERY)
        self.assertEqual(self.rendered, [])

    def test_waiting_on_the_player_is_not_a_stall(self):
        # Bot bowled, player owes the shot: sitting here is normal.
        self._run([self._live(), self._live()], hm.A_PICK_SHOT)
        self.assertEqual(self.rendered, [])

    def test_finished_match_is_left_alone(self):
        self._run([self._live(), self._live(result_finalized=True)],
                  hm.A_PICK_DELIVERY)
        self.assertEqual(self.rendered, [])

    def test_nudges_stop_and_the_player_is_told(self):
        self.ctx.bot_data["vsbot_nudges_7"] = hv.BOT_WATCHDOG_MAX_NUDGES
        self._run([self._live(), self._live()], hm.A_PICK_DELIVERY)
        self.assertEqual(self.rendered, [])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("/r", self.sent[0])

    def test_a_completed_ball_clears_the_nudge_count(self):
        self.ctx.bot_data["vsbot_nudges_7"] = 2
        self._run([self._live(), self._live(current_ball=1)],
                  hm.A_PICK_DELIVERY)
        self.assertNotIn("vsbot_nudges_7", self.ctx.bot_data)


# ════════════════════════════════════════════════════════════════════
# 4. Per-ball work moved off the critical path
# ════════════════════════════════════════════════════════════════════

class EventMediaTests(unittest.TestCase):
    def test_wicket_beats_the_run_clips(self):
        self.assertEqual(
            hm._event_media_keys({"type": "wicket", "runs": 0}, 0, 0,
                                 eoo=False, is_maiden=False),
            ["wicket"])

    def test_boundary_and_milestone_both_fire(self):
        self.assertEqual(
            hm._event_media_keys({"type": "runs", "runs": 4}, 48, 52,
                                 eoo=False, is_maiden=False),
            ["four", "fifty"])

    def test_maiden_over_is_appended_at_end_of_over(self):
        self.assertEqual(
            hm._event_media_keys({"type": "runs", "runs": 0}, 10, 10,
                                 eoo=True, is_maiden=True),
            ["maiden_over"])

    def test_an_ordinary_single_earns_nothing(self):
        self.assertEqual(
            hm._event_media_keys({"type": "runs", "runs": 1}, 10, 11,
                                 eoo=False, is_maiden=False),
            [])

    def test_nothing_to_send_schedules_no_task(self):
        self.assertIsNone(hm._fire_event_media_async(_Ctx(), 1, []))

    def test_clips_are_sent_without_the_caller_awaiting_them(self):
        fired = []

        async def _fake_fire(ctx, chat_id, key):
            fired.append(key)

        async def _go():
            with mock.patch.dict(
                "sys.modules",
                {"services.event_media_service": SimpleNamespace(
                    fire_event_media=_fake_fire)}):
                task = hm._fire_event_media_async(_Ctx(), 1, ["six", "fifty"])
                self.assertFalse(task.done())  # caller is not blocked
                await task
        asyncio.run(_go())
        self.assertEqual(fired, ["six", "fifty"])


class BallCommitTests(unittest.TestCase):
    def test_ss_folds_the_ball_counter_into_one_commit(self):
        calls = {}

        def _save(ctx, mid, s, next_action=None, last_prompt_msg_id=None,
                  bump_ball_seq=False):
            calls["bump"] = bump_ball_seq
            return 4 if bump_ball_seq else None

        with mock.patch.object(hm, "_store_save", _save):
            self.assertEqual(
                hm._ss(_Ctx(), 1, {}, next_action=hm.A_PICK_SHOT,
                       bump_ball_seq=True),
                4)
        self.assertTrue(calls["bump"])

    def test_store_advances_the_counter_in_the_same_commit(self):
        # The guarantee behind the change: state + ball_seq land together, so a
        # ball costs one DB round trip instead of two.
        commits = []
        row = SimpleNamespace(match_id=1, state_json="{}", next_action="X",
                              version=3, ball_seq=8, last_modified=None,
                              last_prompt_msg_id=None)

        class _Q:
            def filter(self, *a):
                return self

            def first(self):
                return row

        session = SimpleNamespace(
            query=lambda model: _Q(), add=lambda r: None,
            commit=lambda: commits.append(1), rollback=lambda: None,
            close=lambda: None)
        with mock.patch.object(store, "get_session", lambda: session):
            new_seq = store.save_state(_Ctx(), 1, {"total_runs": 5},
                                       next_action=store.A_PICK_SHOT,
                                       bump_ball_seq=True)
        self.assertEqual(new_seq, 9)
        self.assertEqual(row.ball_seq, 9)
        self.assertEqual(row.next_action, store.A_PICK_SHOT)
        self.assertEqual(len(commits), 1)


class InningsCardTests(unittest.TestCase):
    def test_ai_players_get_no_card_render(self):
        sent = []

        async def _bat(ctx, cid, p, owner):
            sent.append(("bat", p["name"]))

        async def _bowl(ctx, cid, p, owner):
            sent.append(("bowl", p["name"]))

        human = _p(1, "Human")
        bot_bat = dict(_p(-2, "BotBat"), is_bot_player=True)
        bot_bowl = dict(_p(-3, "BotBowl"), is_bot_player=True)

        with mock.patch.object(hm, "_send_batsman_card", _bat), \
             mock.patch.object(hm, "_send_bowler_card", _bowl):
            asyncio.run(hm._send_innings_cards(
                _Ctx(), 9, (human, bot_bat), bot_bowl, 1, 2))
        self.assertEqual(sent, [("bat", "Human")])

    def test_all_ai_sends_nothing_at_all(self):
        with mock.patch.object(hm, "_send_batsman_card", mock.AsyncMock()) as b:
            asyncio.run(hm._send_innings_cards(
                _Ctx(), 9,
                (dict(_p(-1, "A"), is_bot_player=True),), None, 1, 2))
        b.assert_not_awaited()


# ════════════════════════════════════════════════════════════════════
# 5. The innings break waits on the right thing
# ════════════════════════════════════════════════════════════════════

class RenderScreenSetupDispatchTests(unittest.TestCase):
    """/resume during an innings break must re-show the picker, not bowl."""

    def _dispatch(self, next_action):
        seen = []

        async def _openers(ctx, mid):
            seen.append("openers")

        async def _bowler(ctx, mid):
            seen.append("opening_bowler")

        async def _delivery(ctx, cid, mid):
            seen.append("delivery")

        state = {"chat_id": 1, "innings": 2, "target": 90, "overs": 5,
                 "current_over": 1, "current_ball": 0, "total_runs": 0,
                 "total_wickets": 0, "is_vsbot": True,
                 "bat_user_tg": 42, "bowl_user_tg": hv.BOT_TG_ID}

        with mock.patch.object(hm, "_gs", lambda ctx, mid: state), \
             mock.patch.object(hm, "get_next_action",
                               lambda ctx, mid: next_action), \
             mock.patch.object(hm, "_show_innings_openers", _openers), \
             mock.patch.object(hm, "_show_innings_opening_bowler", _bowler), \
             mock.patch.object(hm, "_show_delivery", _delivery), \
             mock.patch.object(hm, "_ss", lambda *a, **k: None), \
             mock.patch("handlers.vsbot.vsbot_auto_continue",
                        mock.AsyncMock(return_value=False)):
            asyncio.run(hm.render_screen(_Ctx(), 1))
        return seen

    def test_pick_openers_re_shows_the_opener_picker(self):
        self.assertEqual(self._dispatch(hm.A_PICK_OPENERS), ["openers"])

    def test_pick_opening_bowler_re_shows_the_bowler_picker(self):
        self.assertEqual(self._dispatch(hm.A_PICK_OPENING_BOWLER),
                         ["opening_bowler"])

    def test_pick_delivery_still_bowls(self):
        self.assertEqual(self._dispatch(hm.A_PICK_DELIVERY), ["delivery"])


class InningsBreakPointerTests(unittest.TestCase):
    """The break must record the selection it is waiting on.

    It used to write PICK_DELIVERY, so /resume and the heartbeat bowled a ball
    with the first innings' bowler — who by then is on the batting side.
    """

    def _end_innings(self, bot_bats_second):
        human, bot = 42, hv.BOT_TG_ID
        bat_xi = [_p(i, f"H{i}") for i in range(1, 12)]
        bowl_xi = [dict(_p(-i, f"B{i}", bowl_rating=40 + i),
                        is_bot_player=True) for i in range(1, 12)]
        if bot_bats_second:
            # Innings 1: human bats, bot bowls → bot bats second.
            first_bat, first_bowl = human, bot
            first_bat_xi, first_bowl_xi = bat_xi, bowl_xi
        else:
            first_bat, first_bowl = bot, human
            first_bat_xi, first_bowl_xi = bowl_xi, bat_xi

        s = {
            "match_id": 1, "chat_id": 5, "innings": 1, "overs": 5,
            "current_over": 6, "current_ball": 0, "total_runs": 62,
            "total_wickets": 4, "extras_total": 0, "wides": 0, "noballs": 0,
            "legbyes": 0, "timeline": [], "fow": [],
            "partnership_runs": 0, "partnership_balls": 0,
            "striker_idx": 0, "non_striker_idx": 1,
            "bat_user_tg": first_bat, "bowl_user_tg": first_bowl,
            "bat_team_id": 1, "bowl_team_id": 2,
            "bat_team_name": "Bat XI", "bowl_team_name": "Bowl XI",
            "bat_username": "bat", "bowl_username": "bowl",
            "bat_xi": first_bat_xi, "bowl_xi": first_bowl_xi,
            "batting_order": list(first_bat_xi),
            "current_bowler": first_bowl_xi[0],
            "bat_stats": {str(p["roster_id"]): {"runs": 0, "balls": 0}
                          for p in first_bat_xi},
            "bowl_stats": {str(p["roster_id"]): {"balls": 0, "runs": 0}
                           for p in first_bowl_xi},
            "is_vsbot": True,
        }

        saved = []
        shown = []

        def _ss(ctx, mid, state, next_action=None, **kw):
            saved.append(next_action)

        async def _noop_openers(ctx, mid):
            shown.append("openers")

        async def _noop_bowler(ctx, mid):
            shown.append("opening_bowler")

        async def _send_message(chat_id, text, parse_mode=None, **kw):
            return SimpleNamespace(message_id=1)

        ctx = SimpleNamespace(bot=SimpleNamespace(send_message=_send_message),
                              bot_data={}, job_queue=None)

        with mock.patch.object(hm, "_gs", lambda c, m: s), \
             mock.patch.object(hm, "_ss", _ss), \
             mock.patch.object(hm, "_save_match_stats", mock.AsyncMock()), \
             mock.patch.object(hm, "_send_innings_scorecards", mock.AsyncMock()), \
             mock.patch.object(hm, "_show_innings_openers", _noop_openers), \
             mock.patch.object(hm, "_show_innings_opening_bowler", _noop_bowler):
            asyncio.run(hm._end_innings(ctx, 1))
        return s, saved, shown

    def test_bot_batting_second_waits_on_the_players_opening_bowler(self):
        s, saved, shown = self._end_innings(bot_bats_second=True)
        self.assertEqual(saved[-1], hm.A_PICK_OPENING_BOWLER)
        self.assertEqual(shown, ["opening_bowler"])
        self.assertNotIn(hm.A_PICK_DELIVERY, saved)

    def test_player_batting_second_waits_on_their_openers(self):
        s, saved, shown = self._end_innings(bot_bats_second=False)
        self.assertEqual(saved[-1], hm.A_PICK_OPENERS)
        self.assertEqual(shown, ["openers"])
        self.assertNotIn(hm.A_PICK_DELIVERY, saved)

    def test_the_first_innings_bowler_does_not_carry_over(self):
        # After the swap he belongs to the batting side; leaving him as
        # current_bowler is how a phantom over got bowled.
        s, _, _ = self._end_innings(bot_bats_second=True)
        new_bowl_rids = {p["roster_id"] for p in s["bowl_xi"]}
        self.assertIn(s["current_bowler"]["roster_id"], new_bowl_rids)

    def test_bot_openers_are_announced_on_the_bowler_prompt_not_separately(self):
        s, _, _ = self._end_innings(bot_bats_second=True)
        self.assertTrue(any("Bot openers" in line
                            for line in s.get("ai_note", [])))


class HeartbeatInningsBreakTests(unittest.TestCase):
    """A break nobody answers must still start the chase.

    Now that the break records PICK_OPENERS / PICK_OPENING_BOWLER, the AFK
    auto-decide has to understand those pointers — otherwise the match sits
    there forever waiting on a tap that isn't coming.
    """

    def _auto_decide(self, next_act, state):
        from services import match_heartbeat as heartbeat

        saved = []
        rendered = []
        sent = []

        def _save(context, mid, s, next_action=None, **kw):
            saved.append(next_action)

        async def _render(context, mid):
            rendered.append(mid)

        async def _send_message(chat_id, text, parse_mode=None):
            sent.append(text)

        fake_match = mock.MagicMock()
        fake_match.render_screen = _render
        ctx = SimpleNamespace(bot=SimpleNamespace(send_message=_send_message))

        with mock.patch.object(store, "save_state", _save), \
             mock.patch.dict("sys.modules", {"handlers.match": fake_match}):
            asyncio.run(heartbeat._auto_decide(ctx, 9, state, next_act))
        return saved, rendered, sent

    def _state(self):
        return {
            "chat_id": 1, "overs": 5, "innings": 2, "target": 63,
            "bat_xi": [_p(i, f"Bat{i}") for i in range(1, 12)],
            "bowl_xi": [_p(-i, f"Bowl{i}", bowl_rating=40 + i)
                        for i in range(1, 12)],
            "batting_order": [], "bowl_stats": {},
        }

    def test_openers_and_opening_bowler_are_both_filled_in_one_pass(self):
        state = self._state()
        saved, rendered, sent = self._auto_decide(store.A_PICK_OPENERS, state)
        self.assertEqual(state["striker_idx"], 0)
        self.assertEqual(state["non_striker_idx"], 1)
        self.assertEqual(len(state["batting_order"]), 11)
        self.assertIsNotNone(state["current_bowler"])
        self.assertEqual(saved[-1], store.A_PICK_DELIVERY)
        self.assertEqual(rendered, [9])
        self.assertIn("Auto openers", sent[-1])
        self.assertIn("Auto opening bowler", sent[-1])

    def test_opening_bowler_only_leaves_the_chosen_openers_alone(self):
        state = self._state()
        state["striker_idx"] = 4
        state["non_striker_idx"] = 7
        state["batting_order"] = list(state["bat_xi"])
        saved, rendered, sent = self._auto_decide(
            store.A_PICK_OPENING_BOWLER, state)
        self.assertEqual(state["striker_idx"], 4)
        self.assertEqual(state["non_striker_idx"], 7)
        self.assertIsNotNone(state["current_bowler"])
        self.assertEqual(saved[-1], store.A_PICK_DELIVERY)
        self.assertNotIn("Auto openers", sent[-1])

    def test_the_opening_bowler_comes_from_the_bowling_xi(self):
        state = self._state()
        saved, _, _ = self._auto_decide(store.A_PICK_OPENERS, state)
        rids = {p["roster_id"] for p in state["bowl_xi"]}
        self.assertIn(state["current_bowler"]["roster_id"], rids)
        self.assertIsNone(state["prev_bowler_rid"])


class StaleSetupSweepTests(unittest.TestCase):
    def test_selecting_rows_are_swept_once_expired(self):
        # An ignored opener picker used to leave a "selecting" row forever, and
        # because the busy guard spans every mode it locked the player out of
        # starting anything.
        captured = {}

        class _Q:
            def filter(self, *exprs):
                captured["statuses"] = exprs[0].right.value
                return self

            def update(self, values, synchronize_session=False):
                return 0

        session = SimpleNamespace(query=lambda model: _Q(), commit=lambda: None)
        hm._expire_stale_pending_matches(session)
        self.assertEqual(set(captured["statuses"]),
                         {"pending", "toss", "selecting"})


if __name__ == "__main__":
    unittest.main()

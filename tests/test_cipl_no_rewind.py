"""A played over is never played again — /rcl continues, it never rewinds.

The report: over 6 was bowled (49/1 — Bradman out, Kohli in). A captain typed
/rcl and the bot showed "End of Over 6" again, same bowler, a different result
(60/0). /rcl had resumed from a copy of the match taken before over 6 — Dale
Steyn already chosen, the batting approach outstanding — and the over was
simulated a second time.

Two ways that copy came back, both pinned here:

  * the over's DB save failed silently, so the DB kept the pre-over row, and
    memory (the only place over 6 existed) was lost or disagreed with the pointer;
  * a second bot process (a deploy overlap) held the pre-over copy in memory,
    re-showed it, and wrote it back over the newer row.

These tests run the real store (services.match_state_store) against a fake DB
that can drop commits, and the real Challenge League resume path on top of it.
"""

import asyncio
import copy
import json
import unittest
from unittest import mock

import handlers.cipl_play as cp
import services.match_heartbeat as hb
import services.match_state_store as store
from models import MatchState
from services.match_state_store import (
    A_PICK_CIPL_BOWLER, A_PICK_BAT_APPROACH, A_COMPLETED,
)


# ── Fake DB: one table, commits that can fail (or land and still raise) ──────

class _DB:
    def __init__(self):
        self.rows = {}
        self.fail_commits = 0        # next N commits raise and apply nothing
        self.ambiguous_commits = 0   # next N commits apply, then raise


class _Query:
    def __init__(self, session, conditions=None):
        self._session = session
        self.conditions = conditions or {}

    def filter(self, *exprs):
        conditions = dict(self.conditions)
        for e in exprs:
            conditions[e.left.key] = e.right.value
        return _Query(self._session, conditions)

    def _matching(self):
        return [r for r in self._session.db.rows.values()
                if all(getattr(r, k) == v for k, v in self.conditions.items())]

    def first(self):
        rows = self._matching()
        return rows[0] if rows else None

    def update(self, values, synchronize_session=False):
        rows = self._matching()
        for row in rows:
            self._session.pending.append(
                (row, {col.key: val for col, val in values.items()}))
        return len(rows)


class _Session:
    def __init__(self, db):
        self.db = db
        self.pending = []
        self.added = []

    def query(self, model):
        assert model is MatchState
        return _Query(self)

    def add(self, row):
        self.added.append(row)

    def delete(self, row):
        self.db.rows.pop(row.match_id, None)

    def _apply(self):
        for row, values in self.pending:
            for k, v in values.items():
                setattr(row, k, v)
        for row in self.added:
            self.db.rows[row.match_id] = row
        self.pending, self.added = [], []

    def commit(self):
        if self.db.fail_commits:
            self.db.fail_commits -= 1
            self.pending, self.added = [], []
            raise RuntimeError("SSL connection has been closed unexpectedly")
        self._apply()
        if self.db.ambiguous_commits:
            self.db.ambiguous_commits -= 1
            raise RuntimeError("connection lost after COMMIT was sent")

    def rollback(self):
        self.pending, self.added = [], []

    def close(self):
        pass


class _JobQueue:
    def __init__(self):
        self.jobs = {}

    def run_once(self, callback, when, name=None, data=None):
        self.jobs.setdefault(name, []).append(data)

    def get_jobs_by_name(self, name):
        return tuple(self.jobs.get(name, ()))


class _Ctx:
    """One bot process: its own bot_data (the in-memory tier)."""

    def __init__(self, job_queue=None):
        self.bot = mock.MagicMock()
        self.bot.send_message = mock.AsyncMock()
        self.bot.delete_message = mock.AsyncMock()
        self.bot.edit_message_text = mock.AsyncMock()
        self.bot_data = {}
        self.job_queue = job_queue


MID = 42


def _over5_state():
    """End of over 5: 46/0, Steyn picked for over 6, batting approach due."""
    return {
        "match_id": MID, "chat_id": -100, "mode": "cipl_approach",
        "innings": 2, "overs": 20, "current_over": 6, "current_ball": 0,
        "total_runs": 46, "total_wickets": 0, "target": 190,
        "bat_user_tg": 111, "bowl_user_tg": 222,
        "bat_team_name": "theredhairedshankss", "bowl_team_name": "MiTU_014",
        "user_names": {"111": "Ann", "222": "Bo"},
        "current_bowler": {"roster_id": 9, "name": "Dale Steyn"},
        "bowling_approach": "attack", "batting_approach": "rotate",
        "over_msg_ids": [], "action_msg_id": None,
    }


def _play_over6(state):
    """What simulate_over does to the state: 1 1 W 0 1 0 → 49/1, over 7 due."""
    state["total_runs"] += 3
    state["total_wickets"] += 1
    state["current_over"] = 7
    state["prev_bowler_rid"] = 9
    return {"over_timeline": ["1", "1", "W", "0", "1", "0"]}


class _NoRewindCase(unittest.TestCase):
    def setUp(self):
        store.clear_cache()
        self.db = _DB()
        patches = [
            mock.patch.object(store, "get_session", lambda: _Session(self.db)),
            # No real sleeping between save retries / flushes.
            mock.patch.object(cp, "CIPL_SAVE_RETRY_DELAYS", (0,)),
            mock.patch.object(cp, "CIPL_FLUSH_FIRST_DELAY", 0.01),
            mock.patch.object(cp, "_arm_timer", lambda *a, **k: None),
            mock.patch.object(cp, "_approach_card", lambda s: "card"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(store.clear_cache)
        self.addCleanup(store._match_locks.clear)

    def saved(self):
        row = self.db.rows[MID]
        return json.loads(row.state_json), row.next_action

    def seed(self, ctx):
        """Save the over-5 snapshot through the real guarded save."""
        state = _over5_state()
        asyncio.run(cp._ss(ctx, MID, state, next_action=A_PICK_BAT_APPROACH,
                           force=True))
        return state

    def resume(self, ctx, **kwargs):
        """Run cipl_resume and report which prompt it showed, on what state."""
        shown = []

        def _rec(kind):
            async def _prompt(c, mid, state, *a, **k):
                shown.append((kind, state["current_over"], state["total_runs"],
                              state["total_wickets"]))
            return _prompt

        with mock.patch.object(cp, "_prompt_bowler", _rec("bowler")), \
                mock.patch.object(cp, "_prompt_bowl_approach", _rec("bowl_app")), \
                mock.patch.object(cp, "_prompt_bat_approach", _rec("bat_app")):
            result = asyncio.run(cp.cipl_resume(ctx, MID, **kwargs))
        return result, shown


class LostWriteTests(_NoRewindCase):
    """The over's save fails. Over 6 must still never be played twice."""

    def _play_over_with_db_down(self, ctx):
        state = self.seed(ctx)
        self.db.fail_commits = 100          # Neon drops every commit

        async def _flow():
            async def _prompt_bowler(c, mid, s, first=False):
                # the real prompt ends by saving the outstanding pick
                await cp._mark_prompt_delivered(c, mid, s, True,
                                                A_PICK_CIPL_BOWLER)
            with mock.patch.object(cp.cipl_match, "simulate_over", _play_over6), \
                    mock.patch.object(cp.cipl_match, "is_innings_over",
                                      lambda s: False), \
                    mock.patch.object(cp, "_innings_quota_used", lambda s: False), \
                    mock.patch.object(cp, "_render_over_summary",
                                      lambda s, summ: "summary"), \
                    mock.patch.object(cp, "_between_overs_row", lambda s, m: None), \
                    mock.patch.object(cp, "_prompt_bowler", _prompt_bowler), \
                    mock.patch("services.bot_captain.observe_over",
                               lambda s, summ: None), \
                    mock.patch.object(cp.milestones, "snapshot", lambda s: None), \
                    mock.patch.object(cp.milestones, "detect",
                                      lambda s, b, summ: []):
                await cp._run_over(ctx, MID, state)
        asyncio.run(_flow())
        return state

    def test_memory_keeps_the_over_and_the_pointer_moves_on(self):
        ctx = _Ctx()
        self._play_over_with_db_down(ctx)
        # The DB never got over 6 …
        saved, na = self.saved()
        self.assertEqual(saved["total_runs"], 46)
        self.assertEqual(na, A_PICK_BAT_APPROACH)
        # … so memory is flagged as the newest copy, and the pointer comes from
        # it — not the stale "batting approach" still in the DB.
        self.assertTrue(cp._is_dirty(ctx, MID))
        self.assertEqual(asyncio.run(cp._get_next_action(ctx, MID)),
                         A_PICK_CIPL_BOWLER)

    def test_rcl_while_the_db_is_down_continues_at_over_7(self):
        ctx = _Ctx()
        self._play_over_with_db_down(ctx)
        ok, shown = self.resume(ctx)
        self.assertTrue(ok)
        # The bowler for over 7 on 49/1 — never the batting approach for over 6.
        self.assertEqual(shown, [("bowler", 7, 49, 1)])

    def test_rcl_after_the_db_recovers_saves_then_continues(self):
        ctx = _Ctx()
        self._play_over_with_db_down(ctx)
        self.db.fail_commits = 0
        ok, shown = self.resume(ctx)
        self.assertEqual(shown, [("bowler", 7, 49, 1)])
        saved, na = self.saved()
        self.assertEqual((saved["current_over"], saved["total_runs"],
                          saved["total_wickets"]), (7, 49, 1))
        self.assertEqual(na, A_PICK_CIPL_BOWLER)
        self.assertFalse(cp._is_dirty(ctx, MID))

    def test_a_restart_after_the_flush_resumes_at_over_7(self):
        ctx = _Ctx()
        self._play_over_with_db_down(ctx)
        self.db.fail_commits = 0
        asyncio.run(cp.flush_unsaved_matches(ctx))    # shutdown flush
        store.clear_cache()
        ok, shown = self.resume(_Ctx())               # the new process
        self.assertEqual(shown, [("bowler", 7, 49, 1)])

    def test_the_background_flush_saves_it_without_anyone_typing(self):
        ctx = _Ctx()

        async def _go():
            state = _over5_state()
            await cp._ss(ctx, MID, state, next_action=A_PICK_BAT_APPROACH,
                         force=True)
            self.db.fail_commits = 1                 # just this one save fails
            _play_over6(state)
            await cp._ss(ctx, MID, state, next_action=A_PICK_CIPL_BOWLER)
            self.assertTrue(cp._is_dirty(ctx, MID))
            for _ in range(50):
                if not cp._is_dirty(ctx, MID):
                    break
                await asyncio.sleep(0.01)
        # Only the first attempt fails; the in-place retry is patched to one
        # immediate retry, so give it a run with retries off to force the flush.
        with mock.patch.object(cp, "CIPL_SAVE_RETRY_DELAYS", ()):
            asyncio.run(_go())
        self.assertFalse(cp._is_dirty(ctx, MID))
        saved, na = self.saved()
        self.assertEqual(saved["total_runs"], 49)
        self.assertEqual(na, A_PICK_CIPL_BOWLER)


class StaleCopyTests(_NoRewindCase):
    """A second process holding the pre-over copy can't show it or save it."""

    def _two_processes(self):
        a, b = _Ctx(), _Ctx()
        self.seed(a)
        store.clear_cache()
        # Process B loads the match while over 6 is still being decided …
        b_state = asyncio.run(cp._gs(b, MID))
        self.assertEqual(b_state["total_runs"], 46)
        # … then process A plays over 6 and saves it.
        a_state = a.bot_data[store._mem_key(MID)]
        _play_over6(a_state)
        asyncio.run(cp._ss(a, MID, a_state, next_action=A_PICK_CIPL_BOWLER))
        store.clear_cache()
        return a, b, b_state

    def test_rcl_in_the_other_process_resumes_from_the_newer_row(self):
        _, b, _ = self._two_processes()
        ok, shown = self.resume(b)
        self.assertEqual(shown, [("bowler", 7, 49, 1)])

    def test_the_old_copy_is_never_written_back(self):
        _, b, b_state = self._two_processes()
        stale = copy.deepcopy(b_state)
        with mock.patch.object(cp, "_schedule_cipl_recovery") as recover:
            with self.assertRaises(cp.StaleMatchState):
                asyncio.run(cp._ss(b, MID, stale,
                                   next_action=A_PICK_BAT_APPROACH))
        recover.assert_called_once_with(b, MID, force=True)
        saved, na = self.saved()
        self.assertEqual((saved["total_runs"], saved["current_over"]), (49, 7))
        self.assertEqual(na, A_PICK_CIPL_BOWLER)
        # B's memory now holds the saved over, not its old copy.
        self.assertEqual(b.bot_data[store._mem_key(MID)]["total_runs"], 49)

    def test_two_copies_of_one_revision_only_the_first_save_lands(self):
        ctx = _Ctx()
        self.seed(ctx)
        base = ctx.bot_data[store._mem_key(MID)]
        first, second = copy.deepcopy(base), copy.deepcopy(base)
        first["total_runs"], second["total_runs"] = 49, 60
        asyncio.run(cp._ss(_Ctx(), MID, first))
        with mock.patch.object(cp, "_schedule_cipl_recovery"):
            with self.assertRaises(cp.StaleMatchState):
                asyncio.run(cp._ss(_Ctx(), MID, second))
        self.assertEqual(self.saved()[0]["total_runs"], 49)

    def test_a_stale_bat_approach_tap_does_not_replay_the_over(self):
        """The captain taps a batting approach on the pre-over copy."""
        _, b, _ = self._two_processes()
        q = mock.MagicMock()
        q.data = f"cipl_batapp_{MID}_0"
        q.from_user.id = 111
        q.answer = mock.AsyncMock()
        update = mock.MagicMock(callback_query=q)
        with mock.patch.object(cp, "_run_over") as run_over:
            asyncio.run(cp.cipl_batapp_callback(update, b))
        run_over.assert_not_called()
        q.answer.assert_awaited_once()
        self.assertIn("Already chosen", q.answer.await_args.args[0])


class AmbiguousCommitTests(_NoRewindCase):
    def test_a_commit_that_landed_but_raised_counts_as_saved(self):
        ctx = _Ctx()
        state = self.seed(ctx)
        _play_over6(state)
        self.db.ambiguous_commits = 1
        with mock.patch.object(cp, "_schedule_cipl_recovery") as recover:
            asyncio.run(cp._ss(ctx, MID, state, next_action=A_PICK_CIPL_BOWLER))
        recover.assert_not_called()                   # not treated as stale
        self.assertFalse(cp._is_dirty(ctx, MID))
        self.assertEqual(self.saved()[0]["total_runs"], 49)
        # and the next save goes through normally
        state["total_runs"] = 55
        asyncio.run(cp._ss(ctx, MID, state))
        self.assertEqual(self.saved()[0]["total_runs"], 55)


class ClearedMatchTests(_NoRewindCase):
    """A match an admin removed (its row deleted, outside the match lock) is
    never re-created by a save that was still in flight — otherwise the startup
    sweep and heartbeat would resume it and prompt its players again."""

    def test_an_in_flight_save_after_a_clear_does_not_recreate_the_row(self):
        ctx = _Ctx()
        state = self.seed(ctx)
        # /removematch or /clearmatches — cleanup_state deletes the row (and,
        # in this process, the memory copy), but the flow still holds `state`.
        store.cleanup_state(ctx, MID)
        _play_over6(state)
        with mock.patch.object(cp, "_schedule_cipl_recovery") as recover:
            with self.assertRaises(cp.MatchCleared):
                asyncio.run(cp._ss(ctx, MID, state,
                                   next_action=A_PICK_CIPL_BOWLER))
        recover.assert_not_called()                  # nothing to resume
        self.assertNotIn(MID, self.db.rows)
        self.assertNotIn(store._mem_key(MID), ctx.bot_data)
        self.assertFalse(cp._is_dirty(ctx, MID))

    def test_the_web_panel_clear_is_not_undone_by_the_flush(self):
        """The admin web panel clears with a fresh ctx, so the bot's own memory
        copy and its unsaved flag survive the clear."""
        bot = _Ctx()
        state = self.seed(bot)
        self.db.fail_commits = 1
        _play_over6(state)
        with mock.patch.object(cp, "CIPL_SAVE_RETRY_DELAYS", ()), \
                mock.patch.object(cp, "_schedule_cipl_flush"):
            asyncio.run(cp._ss(bot, MID, state, next_action=A_PICK_CIPL_BOWLER))
        self.assertTrue(cp._is_dirty(bot, MID))
        store.cleanup_state(_Ctx(), MID)             # admin.py: fresh_ctx()
        asyncio.run(cp.flush_unsaved_matches(bot))   # or the background flush
        self.assertNotIn(MID, self.db.rows)
        self.assertFalse(cp._is_dirty(bot, MID))
        self.assertNotIn(store._mem_key(MID), bot.bot_data)
        ok, shown = self.resume(bot)
        self.assertFalse(ok)
        self.assertEqual(shown, [])

    def test_a_forced_save_still_creates_a_new_match(self):
        ctx = _Ctx()
        self.seed(ctx)                               # begin_cipl_match's save
        self.assertIn(MID, self.db.rows)
        self.assertEqual(self.saved()[1], A_PICK_BAT_APPROACH)


class TerminalWriteTests(_NoRewindCase):
    def test_force_writes_even_over_a_newer_row(self):
        _, b = _Ctx(), _Ctx()
        a = _Ctx()
        self.seed(a)
        old = copy.deepcopy(a.bot_data[store._mem_key(MID)])
        a_state = a.bot_data[store._mem_key(MID)]
        _play_over6(a_state)
        asyncio.run(cp._ss(a, MID, a_state))
        asyncio.run(cp._ss(b, MID, old, next_action=A_COMPLETED, force=True))
        self.assertEqual(self.saved()[1], A_COMPLETED)


class ResumeGuardTests(_NoRewindCase):
    def test_only_if_stalled_leaves_a_match_with_a_running_clock(self):
        ctx = _Ctx(job_queue=_JobQueue())
        self.seed(ctx)
        ctx.job_queue.run_once(None, 120, name=f"cipl_to_{MID}")
        ok, shown = self.resume(ctx, only_if_stalled=True)
        self.assertFalse(ok)
        self.assertEqual(shown, [])

    def test_only_if_stalled_resumes_a_match_with_no_clock(self):
        ctx = _Ctx(job_queue=_JobQueue())
        self.seed(ctx)
        ok, shown = self.resume(ctx, only_if_stalled=True)
        self.assertTrue(ok)
        self.assertEqual(shown, [("bat_app", 6, 46, 0)])

    def test_a_busy_lock_returns_none_instead_of_hanging(self):
        ctx = _Ctx()
        self.seed(ctx)

        async def _go():
            lock = store.get_match_lock(MID)
            await lock.acquire()
            try:
                return await cp.cipl_resume(ctx, MID, lock_timeout=0.05)
            finally:
                lock.release()
        self.assertIsNone(asyncio.run(_go()))

    def test_rcl_says_so_when_the_match_is_busy(self):
        ctx = _Ctx()
        self.seed(ctx)
        update = mock.MagicMock()
        update.effective_chat.id = -100
        update.effective_user.id = 111
        update.message.reply_text = mock.AsyncMock()

        async def _go():
            lock = store.get_match_lock(MID)
            await lock.acquire()
            try:
                with mock.patch.object(cp, "RCL_LOCK_TIMEOUT", 0.05):
                    await cp.rcl_handler(update, ctx)
            finally:
                lock.release()
        asyncio.run(_go())
        replies = [c.args[0] for c in update.message.reply_text.await_args_list]
        self.assertIn("46/0", replies[0])              # names where it resumes
        self.assertIn("still finishing", replies[-1])


class RclCommandTests(_NoRewindCase):
    """/rcl [MatchId], its 5-second cooldown, and /resume routing to it."""

    def _update(self, chat=-100, user=111):
        update = mock.MagicMock()
        update.effective_chat.id = chat
        update.effective_user.id = user
        update.message.reply_text = mock.AsyncMock()
        return update

    def _rcl(self, ctx, update, args=None, handler=None):
        ctx.args = args or []
        calls = []

        async def _resume(c, mid, *a, **k):
            calls.append(mid)
            return True
        with mock.patch.object(cp, "cipl_resume", _resume):
            asyncio.run((handler or cp.rcl_handler)(update, ctx))
        replies = [c.args[0] for c in update.message.reply_text.await_args_list]
        return calls, replies

    def test_a_second_rcl_within_five_seconds_is_refused(self):
        ctx = _Ctx()
        self.seed(ctx)
        calls, _ = self._rcl(ctx, self._update())
        self.assertEqual(calls, [MID])
        calls, replies = self._rcl(ctx, self._update())
        self.assertEqual(calls, [])
        self.assertIn("Please wait", replies[-1])

    def test_rcl_is_allowed_again_after_the_cooldown(self):
        ctx = _Ctx()
        self.seed(ctx)
        self._rcl(ctx, self._update())
        ctx.bot_data[f"rcl_cd_{MID}"] -= cp.RCL_COOLDOWN + 1
        calls, _ = self._rcl(ctx, self._update())
        self.assertEqual(calls, [MID])

    def test_rcl_by_match_id_from_another_chat(self):
        ctx = _Ctx()
        self.seed(ctx)
        calls, replies = self._rcl(ctx, self._update(chat=-999), args=[f"#{MID}"])
        self.assertEqual(calls, [MID])
        self.assertIn(f"#{MID}", replies[0])
        self.assertIn("re-sent in that match's chat", replies[0])

    def test_rcl_by_id_still_needs_a_captain(self):
        ctx = _Ctx()
        self.seed(ctx)
        calls, replies = self._rcl(ctx, self._update(user=333), args=[str(MID)])
        self.assertEqual(calls, [])
        self.assertIn("Only the two captains", replies[-1])

    def test_rcl_with_a_bad_id_shows_usage(self):
        ctx = _Ctx()
        calls, replies = self._rcl(ctx, self._update(), args=["abc"])
        self.assertEqual(calls, [])
        self.assertIn("Usage", replies[-1])

    def test_rcl_points_a_captain_at_their_match_elsewhere(self):
        ctx = _Ctx()
        self.seed(ctx)
        with mock.patch.object(cp, "_find_cipl_match_in_chat",
                               mock.AsyncMock(return_value=(None, None))):
            calls, replies = self._rcl(ctx, self._update(chat=-5))
        self.assertEqual(calls, [])
        self.assertIn(f"/rcl {MID}", replies[-1])

    def test_resume_on_a_cipl_match_goes_through_rcl(self):
        from handlers import match as match_handlers
        ctx = _Ctx()
        self.seed(ctx)
        calls, replies = self._rcl(ctx, self._update(),
                                   handler=match_handlers.resume_handler)
        self.assertEqual(calls, [MID])
        self.assertIn(f"match #{MID}", replies[0])


class SyncFromRowTests(unittest.TestCase):
    def _row(self, state, version):
        return {"state_json": json.dumps(state), "version": version,
                "next_action": A_PICK_CIPL_BOWLER}

    def test_same_version_keeps_the_memory_object(self):
        ctx = _Ctx()
        mem = {"_rev": 3, "_ver": 5}
        ctx.bot_data[store._mem_key(MID)] = mem
        self.assertIs(store.sync_from_row(ctx, MID, self._row({"_rev": 3}, 5)), mem)

    def test_a_newer_row_replaces_memory(self):
        ctx = _Ctx()
        ctx.bot_data[store._mem_key(MID)] = {"_rev": 3, "_ver": 5, "x": "old"}
        got = store.sync_from_row(ctx, MID, self._row({"_rev": 4, "x": "new"}, 6))
        self.assertEqual(got["x"], "new")
        self.assertEqual(got["_ver"], 6)
        self.assertEqual(got["_na"], A_PICK_CIPL_BOWLER)
        self.assertIs(ctx.bot_data[store._mem_key(MID)], got)

    def test_an_unversioned_copy_of_the_same_revision_is_kept(self):
        """Loaded by the plain get_state (heartbeat) or saved by the plain
        save_state (Lets Play launch): same row, so keep the object."""
        ctx = _Ctx()
        mem = {"_rev": 4, "x": "same"}
        ctx.bot_data[store._mem_key(MID)] = mem
        got = store.sync_from_row(ctx, MID, self._row({"_rev": 4, "x": "same"}, 9))
        self.assertIs(got, mem)
        self.assertEqual(got["_ver"], 9)
        self.assertEqual(got["_na"], A_PICK_CIPL_BOWLER)

    def test_memory_ahead_of_the_row_is_kept(self):
        ctx = _Ctx()
        mem = {"_rev": 5, "_ver": 5}
        ctx.bot_data[store._mem_key(MID)] = mem
        self.assertIs(store.sync_from_row(ctx, MID, self._row({"_rev": 4}, 6)), mem)

    def test_a_missing_row_drops_memory(self):
        ctx = _Ctx()
        ctx.bot_data[store._mem_key(MID)] = {"_rev": 5}
        self.assertIsNone(store.sync_from_row(ctx, MID, None))
        self.assertNotIn(store._mem_key(MID), ctx.bot_data)


class StartupSweepTests(unittest.TestCase):
    def test_every_live_over_by_over_match_is_picked_back_up(self):
        seen = []

        async def _recover(ctx, mid):
            seen.append(mid)

        with mock.patch.object(hb, "_live_cipl_match_ids", lambda: [3, 8]), \
                mock.patch.object(hb, "_recover_cipl", _recover):
            asyncio.run(hb._startup_cipl_sweep(_Ctx()))
        self.assertEqual(seen, [3, 8])

    def test_only_unfinished_cipl_matches_are_listed(self):
        rows = [
            (1, A_PICK_CIPL_BOWLER, json.dumps({"mode": "cipl_approach"})),
            (2, A_COMPLETED, json.dumps({"mode": "cipl_approach"})),
            (3, "PICK_SHOT", json.dumps({"mode": "classic"})),
            (4, A_PICK_CIPL_BOWLER, json.dumps({"mode": "cipl_approach",
                                                "played_via": "webapp"})),
            (5, A_PICK_BAT_APPROACH, "not json"),
        ]
        session = mock.MagicMock()
        session.query.return_value.order_by.return_value.all.return_value = rows
        with mock.patch("database.get_session", lambda: session):
            self.assertEqual(hb._live_cipl_match_ids(), [1])

    def test_start_heartbeat_schedules_the_sweep(self):
        app = mock.MagicMock()
        app.job_queue.get_jobs_by_name.return_value = ()
        hb.start_heartbeat(app)
        names = [c.kwargs.get("name")
                 for c in app.job_queue.run_once.call_args_list]
        self.assertIn(hb.STARTUP_SWEEP_JOB_NAME, names)


if __name__ == "__main__":
    unittest.main()

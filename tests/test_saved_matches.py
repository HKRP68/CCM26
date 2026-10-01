"""Pause, save and continue over-by-over matches; private plans in the Mini App.

  • **Saving** keeps the state and the pick it waits on; a paused Match row
    keeps its tournament fixture, and ``heal_live_fixtures`` leaves it alone.
  • **Continuing** revives the SAME Match row, re-reserving a fixture a clear
    handed back — and refuses one that has been played since.
  • **Only one resume wins** a save; a discard frees the fixture.
  • **Timeouts:** a tournament match's first idle turn saves it; the second,
    a CL Tour match and a friendly still forfeit.
  • **/pause and /continue** end to end, with the chat and store mocked.
  • **Private plans:** two humans in the Mini App see only their own plan, and
    the chat summary gives neither away.
"""

import asyncio
import itertools
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from tests.test_tournament_rating_rule import (  # noqa: F401 — module fixtures
    setUpModule, tearDownModule)

import handlers.cipl_play as cp
from services import cipl_match as cm
from services.match_state_store import (
    A_PICK_BAT_APPROACH, A_PICK_BOWL_APPROACH, A_PICK_CIPL_BOWLER, SAVE_OK)

_SEQ = itertools.count(7000)


def _mk(rid, name, cat, bat, bowl):
    return {"roster_id": rid, "player_id": rid, "name": name,
            "rating": max(bat, bowl), "category": cat, "bat_rating": bat,
            "bowl_rating": bowl, "bowl_style": "Fast", "bowl_hand": "Right",
            "bat_hand": "Right"}


def _xi(offset):
    return ([_mk(offset + i, "Bat%d" % i, "Batsman", 80 - i, 30) for i in range(1, 7)]
            + [_mk(offset + i, "Bwl%d" % i, "Bowler", 30, 82 - i) for i in range(7, 12)])


def _state(mid, u1, u2, t1, t2, **extra):
    s = cm.build_cipl_state(
        match_id=mid, overs=20, bat_user_id=u1, bowl_user_id=u2,
        bat_user_tg=t1, bowl_user_tg=t2, bat_xi=_xi(100), bowl_xi=_xi(500),
        bat_team_name="RCB", bowl_team_name="CSK", chat_id=-100, pitch_type="Hard")
    s["user_names"] = {str(t1): "Asha", str(t2): "Ravi"}
    s.update(extra)
    return s


class _DB(unittest.TestCase):
    """A tournament with one fixture, two users and a live Match row."""

    def setUp(self):
        from database import get_session
        from models import Match, Tournament, TournamentMatch, User
        self.session = get_session()
        tag = next(_SEQ)
        self.u1 = User(telegram_id=tag * 10 + 1, first_name="Asha")
        self.u2 = User(telegram_id=tag * 10 + 2, first_name="Ravi")
        self.tour = Tournament(name=f"Cup {tag}", status="active", is_active=True,
                               schedule_generated=True)
        self.session.add_all([self.u1, self.u2, self.tour])
        self.session.flush()
        self.match = Match(user1_id=self.u1.id, user2_id=self.u2.id,
                           status="active", chat_id=-100,
                           tournament_id=self.tour.id)
        self.session.add(self.match)
        self.session.flush()
        self.fx = TournamentMatch(tournament_id=self.tour.id, status="live",
                                  match_id=self.match.id)
        self.session.add(self.fx)
        self.session.commit()
        self.mid = self.match.id
        self.state = _state(self.mid, self.u1.id, self.u2.id,
                            self.u1.telegram_id, self.u2.telegram_id,
                            tournament_id=self.tour.id,
                            reserved_fixture_id=self.fx.id,
                            action_msg_id=55, over_msg_ids=[55],
                            pinned_msg_id=9, prompt_delivered=True)

    def tearDown(self):
        self.session.close()

    def _fresh(self, cls, pk):
        self.session.expire_all()
        return self.session.get(cls, pk)


class SaveAndReviveTests(_DB):

    def test_a_paused_match_keeps_its_fixture_and_state(self):
        from models import Match, TournamentMatch
        from services import saved_match_service as svc
        from services import league_schedule_service as lss
        row = svc.save_snapshot(self.mid, self.state, A_PICK_BAT_APPROACH,
                                svc.REASON_PAUSED, by_user_id=self.u1.id)
        self.assertEqual(row["status"], svc.ST_SAVED)
        self.assertIn("RCB", row["title"])
        saved = json.loads(row["state_json"])
        # The chat plumbing of the moment it stopped is not part of the save.
        for key in svc.TRANSIENT_KEYS:
            self.assertNotIn(key, saved)
        self.assertEqual(saved["bat_team_name"], "RCB")

        self.assertEqual(svc.park_match_row(self.mid), "active")
        m = self._fresh(Match, self.mid)
        self.assertEqual((m.status, m.end_reason), ("paused", "paused"))
        # heal_live_fixtures must not hand the fixture back under a paused match.
        self.assertEqual(lss.heal_live_fixtures(self.session, self.tour.id), [])
        self.session.commit()
        self.assertEqual(self._fresh(TournamentMatch, self.fx.id).status, "live")

        # It is listed for both captains, and only for them.
        self.assertEqual([r["match_id"] for r in svc.list_saved(self.u2.telegram_id)],
                         [self.mid])
        self.assertEqual(svc.list_saved(424242), [])

    def test_resaving_overwrites_the_older_snapshot(self):
        from services import saved_match_service as svc
        svc.save_snapshot(self.mid, self.state, A_PICK_CIPL_BOWLER, svc.REASON_PAUSED)
        self.state["total_runs"] = 99
        row = svc.save_snapshot(self.mid, self.state, A_PICK_BOWL_APPROACH,
                                svc.REASON_CLEARED)
        self.assertEqual(row["times_saved"], 2)
        self.assertEqual(row["reason"], svc.REASON_CLEARED)
        self.assertEqual(json.loads(svc.get_saved(self.mid)["state_json"])["total_runs"], 99)

    def test_only_one_resume_can_claim_a_save(self):
        from services import saved_match_service as svc
        svc.save_snapshot(self.mid, self.state, A_PICK_CIPL_BOWLER, svc.REASON_PAUSED)
        self.assertTrue(svc.claim(self.mid))
        self.assertFalse(svc.claim(self.mid))
        self.assertTrue(svc.unclaim(self.mid))
        self.assertTrue(svc.claim(self.mid))
        self.assertTrue(svc.finish_claim(self.mid))
        self.assertEqual(svc.get_saved(self.mid)["status"], svc.ST_RESUMED)
        self.assertEqual(svc.list_saved(self.u1.telegram_id), [])

    def test_revive_re_reserves_a_fixture_a_clear_handed_back(self):
        from models import Match, TournamentMatch
        from services import saved_match_service as svc
        # /clearmatches: the row is completed, the fixture healed back.
        m = self._fresh(Match, self.mid)
        m.status, m.end_reason = "completed", "cleared_by_user"
        fx = self.session.get(TournamentMatch, self.fx.id)
        fx.status, fx.match_id = "scheduled", None
        self.session.commit()

        svc.revive_match(self.mid, -200, self.state)
        m = self._fresh(Match, self.mid)
        self.assertEqual((m.status, m.end_reason, m.chat_id), ("active", None, -200))
        fx = self._fresh(TournamentMatch, self.fx.id)
        self.assertEqual((fx.status, fx.match_id), ("live", self.mid))

    def test_a_played_fixture_voids_the_save(self):
        from models import Match, TournamentMatch
        from services import saved_match_service as svc
        m = self._fresh(Match, self.mid)
        m.status = "completed"
        fx = self.session.get(TournamentMatch, self.fx.id)
        fx.status = "completed"
        self.session.commit()
        with self.assertRaises(svc.ReviveError) as err:
            svc.revive_match(self.mid, -100, self.state)
        self.assertTrue(err.exception.permanent)
        # Nothing moved.
        self.assertEqual(self._fresh(Match, self.mid).status, "completed")

    def test_a_fixture_taken_by_another_match_voids_the_save(self):
        from models import Match, TournamentMatch
        from services import saved_match_service as svc
        m = self._fresh(Match, self.mid)
        m.status = "completed"
        fx = self.session.get(TournamentMatch, self.fx.id)
        fx.match_id = self.mid + 1000
        self.session.commit()
        with self.assertRaises(svc.ReviveError) as err:
            svc.revive_match(self.mid, -100, self.state)
        self.assertTrue(err.exception.permanent)

    def test_a_finished_tournament_voids_the_save(self):
        from models import Match, Tournament
        from services import saved_match_service as svc
        self._fresh(Match, self.mid).status = "paused"
        self.session.get(Tournament, self.tour.id).status = "completed"
        self.session.commit()
        with self.assertRaises(svc.ReviveError) as err:
            svc.revive_match(self.mid, -100, self.state)
        self.assertTrue(err.exception.permanent)

    def test_a_live_match_is_not_revived_twice(self):
        from services import saved_match_service as svc
        with self.assertRaises(svc.ReviveError) as err:
            svc.revive_match(self.mid, -100, self.state)
        self.assertFalse(err.exception.permanent)

    def test_discard_frees_the_fixture(self):
        from models import Match, TournamentMatch
        from services import saved_match_service as svc
        svc.save_snapshot(self.mid, self.state, A_PICK_CIPL_BOWLER, svc.REASON_PAUSED)
        svc.park_match_row(self.mid)
        self.assertTrue(svc.discard(self.mid, by_user_id=self.u2.id))
        self.assertFalse(svc.discard(self.mid))
        self.assertEqual(self._fresh(Match, self.mid).status, "abandoned")
        fx = self._fresh(TournamentMatch, self.fx.id)
        self.assertEqual((fx.status, fx.match_id), ("scheduled", None))
        self.assertEqual(svc.get_saved(self.mid)["status"], svc.ST_DISCARDED)

    def test_finished_states_are_not_saveable(self):
        from services import saved_match_service as svc
        from services.match_state_store import A_COMPLETED
        self.assertTrue(svc.is_saveable(self.state, A_PICK_CIPL_BOWLER))
        self.assertFalse(svc.is_saveable(self.state, A_COMPLETED))
        self.assertFalse(svc.is_saveable({**self.state, "result_finalized": True}))
        self.assertFalse(svc.is_saveable({"mode": "classic"}))


class _ChatHarness(_DB):
    """cipl_play's store, timers and Telegram replaced by in-memory fakes."""

    def setUp(self):
        super().setUp()
        import handlers.cipl_pause as pause
        self.pause = pause
        self._saved = {n: getattr(cp, n) for n in
                       ("_gs", "_ss", "_get_next_action", "_cancel_timer",
                        "_super_over_active", "_resume_locked", "_miniapp_row",
                        "_find_cipl_match_in_chat")}
        self._pause_saved = {n: getattr(pause, n) for n in
                             ("cleanup_state", "write_state_guarded", "_chat_busy")}
        self.store = {"state": self.state, "next": A_PICK_BAT_APPROACH}

        async def _gs(ctx, mid):
            return self.store["state"]

        async def _ss(ctx, mid, s, next_action=None, **_kw):
            self.store["state"] = s
            if next_action:
                self.store["next"] = next_action

        async def _na(ctx, mid):
            return self.store["next"]

        async def _find(ctx, cid):
            s = self.store["state"]
            return (self.mid, s) if s else (None, None)

        def _cleanup(ctx, mid):
            self.store["state"] = None
            self.store["next"] = None

        def _write(mid, snapshot, rev, next_action, *_a):
            self.store["state"] = json.loads(snapshot)
            self.store["next"] = next_action
            return {"status": SAVE_OK, "version": 1, "inserted": True}

        cp._gs, cp._ss, cp._get_next_action = _gs, _ss, _na
        cp._cancel_timer = lambda ctx, mid: None
        cp._super_over_active = lambda ctx, mid: False
        cp._resume_locked = AsyncMock(return_value=True)
        cp._miniapp_row = lambda s: None
        cp._find_cipl_match_in_chat = _find
        pause.cleanup_state = _cleanup
        pause.write_state_guarded = _write
        pause._chat_busy = lambda cid, uids: None

        sent = MagicMock()
        sent.message_id = 77
        self.ctx = MagicMock()
        self.ctx.bot_data = {}
        self.ctx.bot.send_message = AsyncMock(return_value=sent)
        self.ctx.bot.delete_message = AsyncMock()
        self.ctx.bot.unpin_chat_message = AsyncMock()
        self.ctx.bot.pin_chat_message = AsyncMock()
        self.ctx.args = []

    def tearDown(self):
        for n, v in self._saved.items():
            setattr(cp, n, v)
        for n, v in self._pause_saved.items():
            setattr(self.pause, n, v)
        super().tearDown()

    def _update(self, tg_id, chat_type="supergroup"):
        upd = MagicMock()
        upd.effective_user.id = tg_id
        upd.effective_user.first_name = "X"
        upd.effective_chat.id = -100
        upd.effective_chat.type = chat_type
        upd.effective_message.reply_text = AsyncMock()
        return upd

    def _replies(self, upd):
        return [c.args[0] for c in upd.effective_message.reply_text.call_args_list]


class PauseContinueFlowTests(_ChatHarness):

    def test_pause_then_continue_picks_up_the_same_ball(self):
        from models import Match
        from services import saved_match_service as svc
        self.state["total_runs"] = 87
        self.state["current_bowler"] = cm.eligible_bowlers(self.state)[0]

        upd = self._update(self.u1.telegram_id)
        asyncio.run(self.pause.pause_handler(upd, self.ctx))
        self.assertIn("paused", self._replies(upd)[-1].lower())
        self.assertIsNone(self.store["state"])          # live state gone
        self.assertEqual(self._fresh(Match, self.mid).status, "paused")
        row = svc.get_saved(self.mid)
        self.assertEqual((row["status"], row["next_action"], row["prev_status"]),
                         (svc.ST_SAVED, A_PICK_BAT_APPROACH, "active"))

        # Ravi asks to continue; Asha has to tap Ready first.
        upd2 = self._update(self.u2.telegram_id)
        self.ctx.args = [str(self.mid)]
        asyncio.run(self.pause.continue_handler(upd2, self.ctx))
        self.assertIsNone(self.store["state"])
        req = self.ctx.bot_data[f"svreq_{self.mid}"]
        self.assertEqual(req["opp"], self.u1.telegram_id)

        # Someone else can't accept for her.
        q = MagicMock()
        q.data = f"svm_ok_{self.mid}"
        q.from_user.id = self.u2.telegram_id
        q.answer = AsyncMock()
        q.edit_message_reply_markup = AsyncMock()
        upd3 = MagicMock()
        upd3.callback_query = q
        asyncio.run(self.pause.saved_callback(upd3, self.ctx))
        self.assertIsNone(self.store["state"])

        q.from_user.id = self.u1.telegram_id
        asyncio.run(self.pause.saved_callback(upd3, self.ctx))
        s = self.store["state"]
        self.assertEqual(s["total_runs"], 87)
        self.assertEqual(s["current_bowler"]["roster_id"],
                         self.state["current_bowler"]["roster_id"])
        self.assertEqual(self.store["next"], A_PICK_BAT_APPROACH)
        self.assertEqual(s["resumed_count"], 1)
        self.assertEqual(self._fresh(Match, self.mid).status, "active")
        self.assertEqual(svc.get_saved(self.mid)["status"], svc.ST_RESUMED)
        cp._resume_locked.assert_awaited()
        self.assertNotIn(f"svreq_{self.mid}", self.ctx.bot_data)

    def test_only_a_captain_may_pause(self):
        upd = self._update(999)
        with patch.object(self.pause, "_is_admin", return_value=False):
            asyncio.run(self.pause.pause_handler(upd, self.ctx))
        self.assertIn("Only the two captains", self._replies(upd)[-1])
        self.assertIsNotNone(self.store["state"])

    def test_a_human_match_continues_only_in_a_group(self):
        from services import saved_match_service as svc
        svc.save_snapshot(self.mid, self.state, A_PICK_CIPL_BOWLER, svc.REASON_PAUSED)
        svc.park_match_row(self.mid)
        upd = self._update(self.u1.telegram_id, chat_type="private")
        self.ctx.args = [str(self.mid)]
        asyncio.run(self.pause.continue_handler(upd, self.ctx))
        self.assertIn("group", self._replies(upd)[-1])

    def test_a_tournament_timeout_saves_once_then_forfeits(self):
        from services import saved_match_service as svc
        self.assertTrue(self.pause.saves_on_timeout(self.state))
        ok = asyncio.run(self.pause.autosave_idle_match(
            self.ctx, self.mid, self.state, A_PICK_BAT_APPROACH))
        self.assertTrue(ok)
        row = svc.get_saved(self.mid)
        self.assertEqual(row["reason"], svc.REASON_AUTO)
        again = json.loads(row["state_json"])
        self.assertEqual(again["idle_saves"], 1)
        self.assertFalse(self.pause.saves_on_timeout(again))
        # Friendlies and CL Tour games keep forfeiting.
        self.assertFalse(self.pause.saves_on_timeout({**self.state, "tournament_id": None}))
        self.assertFalse(self.pause.saves_on_timeout({**self.state, "cl_tour_match_id": 3}))


# ════════════════════════════════════════════════════════════════════
# Private plans (/playapp between two humans)
# ════════════════════════════════════════════════════════════════════

def _pvp(play_mode):
    s = _state(42, 1, 2, 1001, 1002, play_mode=play_mode)
    s["current_bowler"] = cm.eligible_bowlers(s)[0]
    s["bowling_approach"] = "variation"
    s["batting_approach"] = "aggressive"
    return s


class PrivatePlanTests(unittest.TestCase):

    def test_both_humans_can_play_a_moved_match_in_the_app(self):
        from services.match_webapp_service import is_app_played_match, is_view_only_match
        self.assertTrue(is_app_played_match(_pvp("app")))
        self.assertFalse(is_view_only_match(_pvp("app")))
        self.assertTrue(is_view_only_match(_pvp("chat")))
        self.assertTrue(cp._app_mode(_pvp("app")))
        self.assertIsNone(cp._pick_surface_error(_pvp("app"), "app"))
        self.assertIn("/playapp", cp._pick_surface_error(_pvp("chat"), "app"))

    def test_the_chat_summary_gives_neither_plan_away(self):
        for mode, shown in (("chat", True), ("app", False)):
            s = _pvp(mode)
            summary = cm.simulate_over(s)
            summary["combo"] = "The Chess Match"
            summary["flavour"] = "cat and mouse"
            summary["bat_repeat"] = 9
            text = cp._render_over_summary(s, summary)
            self.assertEqual("The Chess Match" in text, shown, mode)
            self.assertEqual("has it read" in text, shown, mode)
            self.assertEqual("Plans stay private" in text, not shown, mode)

    def test_the_app_shows_no_plan_to_anyone(self):
        from services.crickidex_arena import _approach_last_over
        for mode in ("app", "chat"):
            s = _pvp(mode)
            cm.simulate_over(s)
            for role in ("batting", "bowling", "spectator"):
                lo = _approach_last_over(s, role)
                for key in ("battingApproach", "bowlingApproach", "combo",
                            "flavour", "hiddenNote", "privatePlans"):
                    self.assertNotIn(key, lo, (mode, role))
                self.assertNotIn("Hidden", str(lo))

    def test_a_new_innings_flips_the_viewers_side(self):
        from services.crickidex_arena import _approach_last_over
        s = _pvp("app")
        cm.simulate_over(s)
        s["approach_log"][-1]["innings"] = 1
        s["innings"] = 2      # the viewer batted that over and bowls now
        lo = _approach_last_over(s, "bowling")
        self.assertEqual(lo["viewerSide"], "bat")

    def test_the_raw_state_never_carries_a_plan(self):
        from services.match_webapp_service import HIDDEN_PLAN_KEYS, strip_hidden_plans
        s = _pvp("chat")
        out = strip_hidden_plans(s)
        for key in HIDDEN_PLAN_KEYS:
            self.assertNotIn(key, out)
        self.assertEqual(out["bat_team_name"], "RCB")
        self.assertEqual(s["bowling_approach"], "variation")   # a copy


if __name__ == "__main__":
    unittest.main()

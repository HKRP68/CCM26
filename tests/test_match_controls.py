"""Permissions, rolling limits, atomic Elo charges and recoverable clears."""
import asyncio
import itertools
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.test_tournament_rating_rule import setUpModule, tearDownModule
from services import match_control_service as svc
from handlers import match_control as h

_ids = itertools.count(900000)


class Controls(unittest.TestCase):
    def setUp(self):
        from database import get_session
        from models import User, Match
        store_db = patch("services.match_state_store.get_session", get_session)
        store_db.start()
        self.addCleanup(store_db.stop)
        self.db = get_session()
        self.tg = next(_ids)
        self.user = User(telegram_id=self.tg, first_name="Admin")
        self.opponent = User(telegram_id=next(_ids), first_name="Opponent")
        self.db.add_all([self.user, self.opponent])
        self.db.flush()
        self.cid = -self.tg
        self.match = Match(user1_id=self.user.id, user2_id=self.opponent.id,
                           status="active", chat_id=self.cid)
        self.db.add(self.match)
        self.db.commit()
        self.mid = self.match.id
        self.ctx = SimpleNamespace(bot_data={}, job_queue=None, args=[],
                                   bot=SimpleNamespace(get_chat_member=AsyncMock(
                                       return_value=SimpleNamespace(status="administrator"))))
        self.msg = SimpleNamespace(reply_text=AsyncMock())
        self.update = SimpleNamespace(effective_chat=SimpleNamespace(id=self.cid, type="supergroup"),
                                      effective_user=SimpleNamespace(id=self.tg),
                                      effective_message=self.msg)

    def tearDown(self):
        self.db.close()

    def _finish(self, action):
        svc.finish_in_session(self.db, action, {"match_ids": [self.mid]})
        self.db.commit()

    def test_rolling_combined_limit_all_chats_and_exact_boundary(self):
        now = datetime.utcnow()
        for i in range(5):
            action = svc.reserve(self.tg, self.cid - i,
                                 "pause" if i % 2 else "clearmatches",
                                 now=now - timedelta(hours=23, minutes=10 - i))
            self._finish(action)
        with self.assertRaisesRegex(svc.ControlDenied, "all 5"):
            svc.reserve(self.tg, -123, "pause", now=now)
        # The oldest action expires precisely 24 hours after use.
        svc.reserve(self.tg, self.cid, "pause", now=now + timedelta(minutes=50))

    def test_cooldown_is_shared(self):
        now = datetime.utcnow()
        self._finish(svc.reserve(self.tg, self.cid, "pause", now=now))
        with self.assertRaisesRegex(svc.ControlDenied, "60 seconds"):
            svc.reserve(self.tg, self.cid - 1, "clearmatches", now=now + timedelta(seconds=59))
        svc.reserve(self.tg, self.cid - 1, "clearmatches", now=now + timedelta(seconds=60))

    def test_failure_charges_nothing_and_frees_reservation(self):
        from models import RankedRating
        action = svc.reserve(self.tg, self.cid, "pause")
        svc.fail(action, "backup failed")
        svc.reserve(self.tg, self.cid, "pause")
        self.assertIsNone(self.db.query(RankedRating).filter_by(user_id=self.user.id).first())
        self.assertEqual(svc.history(self.tg)[1]["outcome"], "failed")

    def test_exactly_five_elo_only_on_actor_and_once(self):
        from models import RankedRating
        action = svc.reserve(self.tg, self.cid, "pause")
        self._finish(action)
        rating = self.db.query(RankedRating).filter_by(user_id=self.user.id).one()
        self.assertEqual((rating.rating, rating.played, rating.wins, rating.losses), (995, 0, 0, 0))
        self.assertIsNone(self.db.query(RankedRating).filter_by(user_id=self.opponent.id).first())
        with self.assertRaises(svc.ControlDenied):
            self._finish(action)
        self.db.rollback()
        svc.fail(action, "late failure must not reverse success")
        self.assertEqual(svc.history(self.tg)[0]["rating_delta"], -5)

    def test_concurrent_fifth_reservations_cannot_exceed_limit(self):
        now = datetime.utcnow()
        for i in range(4):
            self._finish(svc.reserve(self.tg, self.cid, "pause",
                                     now=now - timedelta(minutes=10 - i)))
        def attempt(i):
            try:
                return svc.reserve(self.tg, self.cid - i, "clearmatches", now=now)
            except svc.ControlDenied:
                return None
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(attempt, range(3)))
        self.assertEqual(sum(r is not None for r in results), 1)

    def test_revoked_access(self):
        svc.set_blocked(self.tg, True)
        with self.assertRaisesRegex(svc.ControlDenied, "revoked"):
            svc.reserve(self.tg, self.cid, "pause")
        svc.set_blocked(self.tg, False)
        svc.reserve(self.tg, self.cid, "pause")

    def test_player_without_admin_role_cannot_clear(self):
        self.ctx.bot.get_chat_member.return_value.status = "member"
        with patch("handlers.cipl_pause._is_admin", return_value=False):
            asyncio.run(h.clear_handler(self.update, self.ctx))
        self.assertIn("Only an admin", self.msg.reply_text.call_args.args[0])
        self.assertEqual(svc.history(self.tg), [])
        self.db.refresh(self.match)
        self.assertEqual(self.match.status, "active")

    def test_private_chat_owner_cannot_clear_but_bot_admin_can(self):
        self.update.effective_chat.type = "private"
        with patch("handlers.cipl_pause._is_admin", return_value=False):
            asyncio.run(h.clear_handler(self.update, self.ctx))
        self.assertIn("Only an admin", self.msg.reply_text.call_args.args[0])
        with patch("handlers.cipl_pause._is_admin", return_value=True):
            asyncio.run(h.clear_handler(self.update, self.ctx))
        self.assertIn("Clear 1", self.msg.reply_text.call_args.args[0])

    def _preview(self):
        with patch("handlers.cipl_pause._is_admin", return_value=False):
            asyncio.run(h.clear_handler(self.update, self.ctx))
        token = next(k[6:] for k in self.ctx.bot_data if k.startswith("mctrl_"))
        q = SimpleNamespace(data=f"mctrl_yes_{token}", from_user=SimpleNamespace(id=self.tg),
                            message=SimpleNamespace(chat=self.update.effective_chat, reply_text=AsyncMock()),
                            answer=AsyncMock(), edit_message_text=AsyncMock())
        return SimpleNamespace(callback_query=q), q

    def _confirm(self, update):
        with patch("handlers.cipl_pause._is_admin", return_value=False), \
                patch("handlers.cipl_play._super_over_active", return_value=False):
            asyncio.run(h.clear_callback(update, self.ctx))

    def test_preview_cancel_expiry_and_caller_binding(self):
        upd, q = self._preview()
        self.assertEqual(svc.history(self.tg), [])
        q.from_user.id += 99
        self._confirm(upd)
        self.assertIn("Only the admin", q.answer.call_args.args[0])
        q.from_user.id = self.tg
        q.data = q.data.replace("_yes_", "_no_")
        self._confirm(upd)
        self.assertEqual(svc.history(self.tg), [])
        upd, q = self._preview()
        for key, value in self.ctx.bot_data.items():
            if key.startswith("mctrl_"):
                value["expires"] = 0
        self._confirm(upd)
        self.assertIn("expired", q.answer.call_args.args[0])

    def test_recheck_permissions_and_changed_targets_on_confirmation(self):
        upd, q = self._preview()
        self.ctx.bot.get_chat_member.return_value.status = "member"
        self._confirm(upd)
        self.assertIn("no longer", q.edit_message_text.call_args.args[0])
        self.ctx.bot.get_chat_member.return_value.status = "administrator"
        upd, q = self._preview()
        self.ctx.bot_data["lp_123"] = {"chat_id": self.cid, "invite_id": 123}
        self._confirm(upd)
        self.assertIn("changed", q.edit_message_text.call_args.args[0])
        self.assertEqual(svc.history(self.tg), [])

    def test_clear_backup_undo_and_no_penalty_refund(self):
        from models import MatchState, MatchControlAction, RankedRating
        state = {"chat_id": self.cid, "mode": "regular", "total_runs": 87}
        self.db.add(MatchState(match_id=self.mid, state_json=json.dumps(state), next_action="PICK_SHOT"))
        self.db.commit()
        self.ctx.bot_data[f"ms_{self.mid}"] = state
        upd, q = self._preview()
        self._confirm(upd)
        self.db.expire_all()
        self.assertEqual(self.match.status, "completed")
        action = self.db.query(MatchControlAction).filter_by(telegram_id=self.tg).one()
        self.assertEqual((action.outcome, action.rating_delta), ("success", -5))
        self.assertEqual(json.loads(action.backup_json)["states"][0]["next_action"], "PICK_SHOT")
        self.assertEqual(h._undo(self.ctx, self.cid, action.id), [self.mid])
        self.db.expire_all()
        self.assertEqual(self.match.status, "active")
        self.assertEqual(self.ctx.bot_data[f"ms_{self.mid}"]["total_runs"], 87)
        self.assertEqual(self.db.query(RankedRating).filter_by(user_id=self.user.id).one().rating, 995)
        with self.assertRaises(svc.ControlDenied):
            h._undo(self.ctx, self.cid, action.id)
        # Replaying the old confirmation cannot clear/charge a second time.
        self._confirm(upd)
        self.assertEqual(len(svc.history(self.tg)), 1)

    def test_backup_failure_keeps_live_match_and_no_debit(self):
        from models import RankedRating
        self.ctx.bot_data[f"ms_{self.mid}"] = {"chat_id": self.cid, "mode": "cipl_approach"}
        upd, q = self._preview()
        with patch("services.saved_match_service.save_snapshot", side_effect=RuntimeError("disk unavailable")):
            self._confirm(upd)
        self.db.refresh(self.match)
        self.assertEqual(self.match.status, "active")
        self.assertIn(f"ms_{self.mid}", self.ctx.bot_data)
        self.assertEqual(svc.history(self.tg)[0]["outcome"], "failed")
        self.assertIsNone(self.db.query(RankedRating).filter_by(user_id=self.user.id).first())

    def test_undo_refuses_new_play(self):
        upd, q = self._preview()
        self._confirm(upd)
        action_id = svc.history(self.tg)[0]["id"]
        self.ctx.bot_data["lp_new"] = {"chat_id": self.cid}
        with self.assertRaisesRegex(svc.ControlDenied, "already has"):
            h._undo(self.ctx, self.cid, action_id)

    def test_empty_chat_is_free(self):
        self.match.status = "completed"
        self.db.commit()
        with patch("handlers.cipl_pause._is_admin", return_value=False):
            asyncio.run(h.clear_handler(self.update, self.ctx))
        self.assertIn("No ongoing", self.msg.reply_text.call_args.args[0])
        self.assertEqual(svc.history(self.tg), [])

    def _over_by_over(self):
        from models import MatchState
        from tests.test_saved_matches import _state
        from services.match_state_store import A_PICK_BAT_APPROACH
        state = _state(self.mid, self.user.id, self.opponent.id,
                       self.tg, self.opponent.telegram_id)
        state["chat_id"] = self.cid
        self.ctx.bot_data[f"ms_{self.mid}"] = state
        self.db.add(MatchState(match_id=self.mid, state_json=json.dumps(state),
                               next_action=A_PICK_BAT_APPROACH))
        self.db.commit()

    def test_resumable_clear_and_undo(self):
        from services import saved_match_service as saved
        self._over_by_over()
        upd, q = self._preview()
        self._confirm(upd)
        self.assertEqual(saved.get_saved(self.mid)["status"], "saved")
        self.assertIn(f"/continue {self.mid}", q.edit_message_text.call_args.args[0])
        h._undo(self.ctx, self.cid, svc.history(self.tg)[0]["id"])
        self.assertEqual(saved.get_saved(self.mid)["status"], "resumed")

    def test_late_failure_rolls_back_snapshot_clear_and_rating(self):
        from models import MatchState, RankedRating
        from services import saved_match_service as saved
        self._over_by_over()
        upd, q = self._preview()
        with patch.object(svc, "finish_in_session", side_effect=RuntimeError("DB failure")):
            self._confirm(upd)
        self.db.expire_all()
        self.assertEqual(self.match.status, "active")
        self.assertIsNotNone(self.db.query(MatchState).filter_by(match_id=self.mid).first())
        self.assertIsNone(saved.get_saved(self.mid))
        self.assertEqual(svc.history(self.tg)[0]["outcome"], "failed")
        self.assertIsNone(self.db.query(RankedRating).filter_by(user_id=self.user.id).first())

    def test_undo_restores_reserved_fixture_and_cannot_steal_reused_fixture(self):
        from models import Tournament, TournamentMatch
        tour = Tournament(name=f"Cup {self.tg}")
        self.db.add(tour)
        self.db.flush()
        fixture = TournamentMatch(tournament_id=tour.id, match_id=self.mid, status="live")
        self.db.add(fixture)
        self.db.commit()
        upd, q = self._preview()
        self._confirm(upd)
        self.db.refresh(fixture)
        self.assertEqual((fixture.match_id, fixture.status), (None, "scheduled"))
        fixture.status = "completed"
        self.db.commit()
        with self.assertRaisesRegex(svc.ControlDenied, "fixture has been reused"):
            h._undo(self.ctx, self.cid, svc.history(self.tg)[0]["id"])
        self.db.refresh(self.match)
        self.assertEqual(self.match.status, "completed")
        fixture.status = "scheduled"
        self.db.commit()
        h._undo(self.ctx, self.cid, svc.history(self.tg)[0]["id"])
        self.db.refresh(fixture)
        self.assertEqual((fixture.match_id, fixture.status), (self.mid, "live"))


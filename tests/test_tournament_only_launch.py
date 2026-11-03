"""Access changes must be rechecked when an already-open setup launches."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from handlers import cipl_play, letsplay


def _users(restricted):
    return {
        uid: SimpleNamespace(id=uid, telegram_id=uid + 100,
                             tournament_only=uid == restricted)
        for uid in (1, 2)
    }


def _context():
    return SimpleNamespace(bot_data={}, job_queue=MagicMock(),
                           bot=SimpleNamespace(send_message=AsyncMock(),
                                               delete_message=AsyncMock()))


def _session(users):
    session = MagicMock()
    session.get.side_effect = lambda model, uid: users.get(uid)
    return session


class LetsPlayLaunchAccessTests(unittest.TestCase):
    def _draft(self):
        return {
            "invite_id": 8, "chat_id": -100, "status": "showxi",
            "match_launched": True,
            "host": {"user_id": 1, "tg_id": 101, "name": "Host"},
            "guest": {"user_id": 2, "tg_id": 102, "name": "Guest"},
        }

    def test_either_captains_new_restriction_cancels_pending_friendly(self):
        for restricted in (1, 2):
            with self.subTest(restricted=restricted):
                ctx, draft = _context(), self._draft()
                ctx.bot_data["lp_8"] = draft
                session = _session(_users(restricted))
                with patch.object(letsplay, "get_session", return_value=session), \
                     patch.object(letsplay, "_get_ordered_roster") as roster:
                    asyncio.run(letsplay._launch_match(ctx, draft, "bat", "host"))
                roster.assert_not_called()
                session.add.assert_not_called()
                session.commit.assert_not_called()
                self.assertNotIn("lp_8", ctx.bot_data)
                self.assertFalse(draft["match_launched"])
                self.assertEqual(draft["status"], "cancelled")
                self.assertEqual(
                    {call.args[0] for call in ctx.job_queue.get_jobs_by_name.call_args_list},
                    {"lp_invite_to_8", "lp_setup_to_8", "lp_traitvote_to_8"})
                self.assertIn("tournament-only", ctx.bot.send_message.await_args.args[1])
                session.close.assert_called_once()

    def test_notification_failure_still_frees_the_chat(self):
        ctx, draft = _context(), self._draft()
        ctx.bot_data["lp_8"] = draft
        ctx.bot.send_message.side_effect = RuntimeError("Telegram unavailable")
        session = _session(_users(2))
        with patch.object(letsplay, "get_session", return_value=session):
            asyncio.run(letsplay._launch_match(ctx, draft, "bat", "host"))
        self.assertNotIn("lp_8", ctx.bot_data)
        session.add.assert_not_called()

    def test_official_tournament_setup_reaches_normal_roster_validation(self):
        ctx, draft = _context(), self._draft()
        draft["lpt"] = {"tournament_id": 42}
        ctx.bot_data["lp_8"] = draft
        session = _session(_users(2))
        with patch.object(letsplay, "get_session", return_value=session), \
             patch.object(letsplay, "_get_ordered_roster", return_value=[]) as roster, \
             patch.object(letsplay, "validate_xi", return_value=(False, ["Missing XI"])):
            asyncio.run(letsplay._launch_match(ctx, draft, "bat", "host"))
        roster.assert_called()
        self.assertNotIn("tournament-only", ctx.bot.send_message.await_args.args[1])


class ChallengeLeagueLaunchAccessTests(unittest.TestCase):
    def _draft(self):
        return {
            "draft_id": 9, "chat_id": -100, "turn": "complete",
            "host_user_id": 1, "target_user_id": 2,
            "host_tg_id": 101, "target_tg_id": 102,
            "sel_awaiting": [102], "sel_remind_msg_id": 66,
        }

    def test_either_captains_new_restriction_cancels_pending_league_match(self):
        for restricted in (1, 2):
            with self.subTest(restricted=restricted):
                ctx, draft = _context(), self._draft()
                ctx.bot_data.update(challenge_team_draft_9=draft,
                                    **{"cl_draft_chat_-200": 10})
                ctx.bot_data["cl_draft_chat_-100"] = 9
                session = _session(_users(restricted))
                q = SimpleNamespace(answer=AsyncMock())
                with patch.object(cipl_play, "get_session", return_value=session), \
                     patch("handlers.match._active_match_in_chat") as busy:
                    asyncio.run(cipl_play._launch_after_toss(
                        ctx, q, draft, 9, "bat", "host"))
                busy.assert_not_called()
                session.add.assert_not_called()
                session.commit.assert_not_called()
                self.assertNotIn("challenge_team_draft_9", ctx.bot_data)
                self.assertNotIn("cl_draft_chat_-100", ctx.bot_data)
                self.assertEqual(ctx.bot_data["cl_draft_chat_-200"], 10)
                self.assertEqual(draft["sel_awaiting"], [])
                self.assertEqual(draft["turn"], "cancelled")
                self.assertFalse(draft["launch_in_progress"])
                self.assertEqual(
                    {call.args[0] for call in ctx.job_queue.get_jobs_by_name.call_args_list},
                    {"cl_remind_9", "cl_forfeit_9", "cl_draft_9"})
                ctx.bot.delete_message.assert_awaited_once_with(-100, 66)
                self.assertIn("tournament-only", q.answer.await_args.args[0])
                session.close.assert_called_once()

    def test_bot_election_falls_back_to_chat_after_answered_query(self):
        ctx, draft = _context(), self._draft()
        draft["vs_bot"] = True
        ctx.bot_data["challenge_team_draft_9"] = draft
        session = _session(_users(1))
        q = SimpleNamespace(answer=AsyncMock(side_effect=RuntimeError("Already answered")))
        with patch.object(cipl_play, "get_session", return_value=session):
            asyncio.run(cipl_play._launch_after_toss(ctx, q, draft, 9, "bat", "target"))
        self.assertNotIn("challenge_team_draft_9", ctx.bot_data)
        self.assertIn("tournament-only", ctx.bot.send_message.await_args.args[1])
        session.add.assert_not_called()

    def test_official_draft_still_reaches_the_match_concurrency_gate(self):
        ctx, draft = _context(), self._draft()
        draft.update(is_tournament=True, tournament_id=42)
        ctx.bot_data["challenge_team_draft_9"] = draft
        session = _session(_users(2))
        q = SimpleNamespace(answer=AsyncMock())
        with patch.object(cipl_play, "get_session", return_value=session), \
             patch("handlers.match._active_match_in_chat", return_value=object()) as busy:
            asyncio.run(cipl_play._launch_after_toss(ctx, q, draft, 9, "bat", "host"))
        busy.assert_called_once_with(session, -100)
        self.assertIn("already active", q.answer.await_args.args[0])
        self.assertIn("challenge_team_draft_9", ctx.bot_data)

    def test_private_tour_metadata_does_not_exempt_a_friendly(self):
        ctx, draft = _context(), self._draft()
        draft.update(cl_tour_id=51, cl_tour_match_id=52)
        ctx.bot_data["challenge_team_draft_9"] = draft
        session = _session(_users(2))
        q = SimpleNamespace(answer=AsyncMock())
        with patch.object(cipl_play, "get_session", return_value=session):
            asyncio.run(cipl_play._launch_after_toss(ctx, q, draft, 9, "bat", "host"))
        session.add.assert_not_called()
        self.assertNotIn("challenge_team_draft_9", ctx.bot_data)
        self.assertIn("tournament-only", q.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()

"""Exercise real PTB dispatch and persisted restrictions, without Telegram I/O."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from telegram import CallbackQuery, Chat, Message, MessageEntity, Update, User as TGUser
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, TypeHandler

from database import Base
from models import Match, Tournament, TournamentMatch, TournamentTeam, User
from handlers import ciplbot, challenge, letsplay, lpbot, tournament_only as commands
from services import tournament_only_guard as gate, tournament_only_service as service


class EnforcementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.tmp.name) / 'test.db'}",
                                    connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        with self.sessions() as db:
            self.actor = User(telegram_id=1001, username="captain", first_name="Captain")
            self.other = User(telegram_id=1002, username="opponent", first_name="Opponent")
            tour = Tournament(name="Current Cup", kind="letsplay", status="active")
            db.add_all([self.actor, self.other, tour])
            db.flush()
            a = TournamentTeam(tournament_id=tour.id, name="Captain XI", user_tg_id=1001)
            b = TournamentTeam(tournament_id=tour.id, name="Opponent XI", user_tg_id=1002)
            db.add_all([a, b])
            db.flush()
            fx = TournamentMatch(tournament_id=tour.id, team1_id=a.id, team2_id=b.id,
                                 status="scheduled", stage="league", round_no=1, match_no=1)
            match = Match(user1_id=self.actor.id, user2_id=self.other.id,
                          tournament_id=tour.id, status="active", chat_id=-100)
            db.add_all([fx, match])
            db.commit()
            self.actor_id, self.other_id = self.actor.id, self.other.id
            self.fixture_id, self.match_id, self.tour_id = fx.id, match.id, tour.id
        self.reply = AsyncMock()
        self.dm = AsyncMock()
        self.answer = AsyncMock()
        patches = [patch("database.get_session", self.sessions),
                   patch.object(commands, "get_session", self.sessions),
                   patch.object(commands, "is_bot_admin", side_effect=lambda tg: tg == 9001),
                   patch("telegram.Message.reply_text", self.reply),
                   patch("telegram.ext.ExtBot.send_message", self.dm),
                   patch("telegram.CallbackQuery.answer", self.answer)]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.app = Application.builder().token("123456:test-token").updater(None).build()
        # Network initialization only fetches bot identity; provide it locally.
        self.app.bot._bot_user = TGUser(id=123456, first_name="Bot", is_bot=True, username="TestBot")
        self.app._initialized = True
        self.app.add_handler(TypeHandler(Update, gate.tournament_only_guard), group=-15)
        self.app.add_handler(CommandHandler(["touronly", "tournamentonly"], commands.tournamentonly_handler))
        self.game = AsyncMock()
        self.app.add_handler(CommandHandler(sorted(gate.START_COMMANDS - {"16-0ipl"}), self.game))
        self.app.add_handler(CommandHandler(["lptour", "lptplay"], self.game))
        self.app.add_handler(CallbackQueryHandler(self.game))
        self.update_id = 0

    async def asyncTearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def incoming(self, text=None, *, sender=1001, callback=None):
        self.update_id += 1
        tg = TGUser(id=sender, first_name="Captain", is_bot=False,
                    username="captain" if sender == 1001 else "opponent")
        entities = ([MessageEntity(MessageEntity.BOT_COMMAND, 0, len(text.split()[0]))]
                    if text else [])
        msg = Message(message_id=self.update_id, date=datetime.now(timezone.utc),
                      chat=Chat(-10, "supergroup"), from_user=tg, text=text, entities=entities)
        msg.set_bot(self.app.bot)
        if callback:
            query = CallbackQuery(str(self.update_id), tg, "chat", message=msg, data=callback)
            query.set_bot(self.app.bot)
            incoming = Update(self.update_id, callback_query=query)
        else:
            incoming = Update(self.update_id, message=msg)
        incoming.set_bot(self.app.bot)
        return incoming

    async def send(self, text=None, **kwargs):
        self.game.reset_mock()
        self.reply.reset_mock()
        self.answer.reset_mock()
        await self.app.process_update(self.incoming(text, **kwargs))

    async def block(self, target=1001):
        await self.send(f"/touronly block {target}", sender=9001)
        self.assertIn("ACTIVE", self.dm.await_args.kwargs["text"])

    async def test_plain_block_stops_letsplay_and_every_registered_casual_mode(self):
        await self.block()
        for name in sorted(gate.START_COMMANDS - {"16-0ipl"}):
            with self.subTest(command=name):
                await self.send(f"/{name}")
                self.game.assert_not_awaited()
                self.assertIn("ACTIVE", self.reply.await_args.args[0])

    async def test_old_implicit_grace_is_blocked_too(self):
        await self.block()
        with self.sessions() as db:
            user = db.get(User, self.actor_id)
            user.tournament_only_starts_at = datetime.utcnow() + timedelta(hours=24)
            user.tournament_only_warning = False
            db.commit()
        await self.send("/lp@TestBot @opponent")
        self.game.assert_not_awaited()
        self.assertIn("ACTIVE", self.reply.await_args.args[0])

    async def test_explicit_warning_expires_then_blocks_real_dispatch(self):
        await self.send("/touronly warn 1001 --grace=1", sender=9001)
        self.assertIn("IST", self.dm.await_args.kwargs["text"])
        await self.send("/letsplay @opponent")
        self.game.assert_awaited_once()
        with self.sessions() as db:
            db.get(User, self.actor_id).tournament_only_starts_at = datetime.utcnow() - timedelta(seconds=1)
            db.commit()
        await self.send("/letsplay @opponent")
        self.game.assert_not_awaited()

    async def test_old_buttons_and_rematches_cannot_bypass_block(self):
        await self.block()
        self.app.bot_data["lp_7"] = {"host": {"user_id": self.actor_id}, "lpt": None}
        for data in ("lp_accept_7", "lp_pitch_7_flat", "lp_startmatch_7", "lp_coin_heads_7",
                     "botmatch_again_lp", "botmatch_again_cipl_ipl", "cric_join"):
            with self.subTest(callback=data):
                await self.send(callback=data)
                self.game.assert_not_awaited()
                self.answer.assert_awaited_once()
                self.assertTrue(self.answer.await_args.kwargs["show_alert"])
                alert = self.answer.await_args.args[0]
                self.assertIn("IST", alert)
                self.assertIn("/touronly status", alert)
                self.assertLessEqual(len(alert), 200)

    async def test_official_play_allowed_and_completion_unlocks_casual_play(self):
        await self.block()
        await self.send("/lptour")
        self.game.assert_awaited_once()
        await self.send(callback=f"cipl_bowler_{self.match_id}_11")
        self.game.assert_awaited_once()
        with self.sessions() as db:
            db.get(TournamentMatch, self.fixture_id).status = "completed"
            service.refresh_restrictions(db)
            db.commit()
        await self.send("/letsplay @opponent")
        self.game.assert_awaited_once()

    async def test_letsplay_handler_itself_blocks_host_or_opponent(self):
        await self.block()
        ctx = NS(args=["@opponent"], bot_data={}, job_queue=None)
        with patch.object(letsplay, "get_session", self.sessions), \
                patch.object(letsplay, "_get_ordered_roster") as roster:
            await letsplay.letsplay_handler(self.incoming("/letsplay @opponent"), ctx)
            roster.assert_not_called()
        self.assertIn("ACTIVE", self.reply.await_args.args[0])
        with self.sessions() as db:
            service.set_restriction(db, db.get(User, self.actor_id), enabled=False, admin_telegram_id=9001)
            db.commit()
        await self.block(target=1002)
        with patch.object(letsplay, "get_session", self.sessions), \
                patch.object(letsplay, "_active_match_in_chat", return_value=None), \
                patch.object(letsplay, "_get_ordered_roster") as roster:
            await letsplay.letsplay_handler(self.incoming("/letsplay @opponent"), ctx)
            roster.assert_not_called()
        self.assertIn("participant", self.reply.await_args.args[0])
        self.assertEqual(ctx.bot_data, {})

    async def test_bot_handlers_deny_before_difficulty_or_team_selection(self):
        await self.block()
        ctx = NS(args=[], bot_data={}, job_queue=None)
        with patch.object(lpbot, "get_session", self.sessions), \
                patch("handlers.botlevel.prompt_difficulty", AsyncMock()) as difficulty:
            await lpbot.lpbot_handler(self.incoming("/lpbot"), ctx)
            difficulty.assert_not_awaited()
        with patch.object(ciplbot, "get_session", self.sessions), \
                patch.object(ciplbot, "_resolve_league") as resolve:
            await ciplbot.ciplbot_handler(self.incoming("/ciplbot"), ctx)
            resolve.assert_not_called()
        self.assertEqual(ctx.bot_data, {})

    async def test_wpm_and_sim_handlers_recheck_before_gameplay(self):
        from handlers import match, sim
        await self.block()
        ctx = NS(args=[], bot_data={}, job_queue=None)
        with patch.object(match, "get_session", self.sessions), \
                patch.object(match, "_active_cric_match_in_chat") as busy:
            await match.wpm_handler(self.incoming("/wpm"), ctx)
            busy.assert_not_called()
        self.assertIn("ACTIVE", self.reply.await_args.args[0])
        progress = NS(edit_text=AsyncMock())
        with patch.object(sim, "get_session", self.sessions), \
                patch.object(sim, "sim_cooldown_state") as cooldown, \
                patch("telegram.Message.reply_text", AsyncMock(return_value=progress)):
            await sim.sim_handler(self.incoming("/sim"), ctx)
            cooldown.assert_not_called()
        self.assertIn("ACTIVE", progress.edit_text.await_args.args[0])

    async def test_callback_classification_failure_still_stops_other_handlers(self):
        await self.block()
        with patch.object(gate, "_callback_action", side_effect=ValueError("stale callback")):
            await self.send(callback="lp_startmatch_7")
        self.game.assert_not_awaited()
        self.answer.assert_awaited_once()

    async def test_league_picker_cannot_create_casual_draft_for_restricted_guest(self):
        await self.block(target=1002)
        ctx = NS(args=[], bot_data={}, job_queue=None)
        with self.sessions() as db:
            await challenge._send_league_team_picker(
                self.incoming("/cipl"), ctx, challenger=db.get(User, self.actor_id),
                target=db.get(User, self.other_id), session=db, league_key="ipl",
                league_name="IPL", league_record=None, teams=[])
        self.assertEqual(ctx.bot_data, {})
        self.assertIn("participant", self.reply.await_args.args[0])


if __name__ == "__main__":
    unittest.main()

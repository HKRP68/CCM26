"""Permissions, exact targeting and persistent changes for /tournamentonly."""

import asyncio
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap

_MODULES = ("config", "database", "models", "handlers.tournament_only",
            "services.tournament_only_service")


def setUpModule():
    global _OLD_URL, _SAVED, _TMP, _ENGINE
    _OLD_URL = os.environ.get("DATABASE_URL")
    _SAVED = _module_swap.save(_MODULES)
    _module_swap.unload(_MODULES)
    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"
    from database import Base, engine
    import models
    _ENGINE = engine
    Base.metadata.create_all(bind=engine)


def tearDownModule():
    _ENGINE.dispose()
    if _OLD_URL is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _OLD_URL
    _module_swap.restore(_SAVED)
    os.unlink(_TMP.name)


class CommandTests(unittest.TestCase):
    def setUp(self):
        from database import get_session
        from models import User, AdminLog, Tournament, TournamentTeam, TournamentMatch
        from handlers import tournament_only
        self.handler = tournament_only
        self.User = User
        self.AdminLog = AdminLog
        self.session = get_session()
        self.session.query(TournamentMatch).delete()
        self.session.query(TournamentTeam).delete()
        self.session.query(Tournament).delete()
        self.session.query(AdminLog).delete()
        self.session.query(User).delete()
        self.user = User(telegram_id=70001, username="Cricket_Player",
                         first_name="Cricket player")
        self.other = User(telegram_id=70002, username="Other_Player")
        self.session.add_all([self.user, self.other])
        tour = Tournament(name="Cup", status="active")
        self.session.add(tour)
        self.session.flush()
        a = TournamentTeam(tournament_id=tour.id, name="A", user_tg_id=70001)
        b = TournamentTeam(tournament_id=tour.id, name="B", user_tg_id=70002)
        self.session.add_all([a,b])
        self.session.flush()
        self.session.add(TournamentMatch(tournament_id=tour.id, team1_id=a.id,
                                        team2_id=b.id, round_no=1, match_no=1,
                                        status="scheduled", stage="league"))
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def run_command(self, *args, reply_id=None, admin=True):
        message = SimpleNamespace(reply_text=AsyncMock(), reply_to_message=(
            SimpleNamespace(from_user=SimpleNamespace(id=reply_id, is_bot=False))
            if reply_id is not None else None))
        update = SimpleNamespace(effective_user=SimpleNamespace(id=90001),
                                 effective_message=message)
        with patch.object(self.handler, "is_bot_admin", return_value=admin):
            asyncio.run(self.handler.tournamentonly_handler(
                update, SimpleNamespace(args=list(args))))
        self.session.expire_all()
        return message.reply_text.call_args.args[0]

    def test_nonadmin_cannot_read_or_mutate(self):
        with patch.object(self.handler, "_execute") as execute:
            for args in (("block", "70001"), ("status", "70001"), ("list",)):
                self.assertIn("Only bot admins", self.run_command(*args, admin=False))
            execute.assert_not_called()
        self.assertFalse(self.user.tournament_only)

    def test_block_by_case_insensitive_username_persists_and_audits(self):
        text = self.run_command("block", "@CRICKET_PLAYER", "Pending", "fixtures")
        self.assertIn("tournament-only access", text)
        self.assertTrue(self.user.tournament_only)
        self.assertEqual(self.user.tournament_only_reason, "Pending fixtures")
        self.assertEqual(self.user.tournament_only_by, 90001)
        log = self.session.query(self.AdminLog).one()
        self.assertEqual(log.action, "tournament_only_block")
        self.assertEqual(log.target_id, self.user.id)

    def test_reply_block_accepts_reason(self):
        self.run_command("block", "Finish", "your", "fixtures", reply_id=70001)
        self.assertTrue(self.user.tournament_only)
        self.assertEqual(self.user.tournament_only_reason, "Finish your fixtures")

    def test_grace_option_can_restrict_immediately(self):
        from services import tournament_only_service
        self.run_command("block", "70001", "--grace=0")
        self.assertTrue(tournament_only_service.is_restricted(self.user))

    def test_warning_dm_includes_required_fixture_and_deadline(self):
        message = SimpleNamespace(reply_text=AsyncMock(), reply_to_message=None)
        bot = SimpleNamespace(send_message=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=90001), effective_message=message)
        with patch.object(self.handler, "is_bot_admin", return_value=True):
            asyncio.run(self.handler.tournamentonly_handler(
                update, SimpleNamespace(args=["block", "70001"], bot=bot)))
        self.assertEqual(bot.send_message.await_args.kwargs["chat_id"], 70001)
        notice = bot.send_message.await_args.kwargs["text"]
        self.assertIn("warning", notice)
        self.assertIn("UTC", notice)
        self.assertIn("M1", notice)

    def test_target_first_shortcut_and_manual_release(self):
        self.run_command("70001")
        self.assertTrue(self.user.tournament_only)
        self.run_command("off", "70001")
        self.assertFalse(self.user.tournament_only)
        self.assertIsNone(self.user.tournament_only_reason)
        self.assertIsNone(self.user.tournament_only_at)
        self.assertIsNone(self.user.tournament_only_by)

    def test_reply_status_and_unblock(self):
        self.run_command("block", reply_id=70001)
        self.assertIn("tournament-only", self.run_command("status", reply_id=70001))
        self.run_command("unblock", reply_id=70001)
        self.assertIn("normal game access", self.run_command("status", "70001"))

    def test_explicit_target_wins_over_reply(self):
        self.run_command("block", "70002", reply_id=70001)
        self.assertTrue(self.other.tournament_only)
        self.assertFalse(self.user.tournament_only)

    def test_username_underscore_does_not_match_other_character(self):
        self.user.username = "CricketXPlayer"
        self.session.commit()
        self.assertIn("Player not found", self.run_command("block", "@Cricket_Player"))
        self.assertFalse(self.user.tournament_only)

    def test_ambiguous_username_requires_id(self):
        self.other.username = "cricket_player"
        self.session.commit()
        self.assertIn("More than one", self.run_command("block", "@Cricket_Player"))
        self.assertFalse(self.user.tournament_only)
        self.assertFalse(self.other.tournament_only)

    def test_reason_and_name_are_html_escaped(self):
        self.user.username = None
        self.user.first_name = "<player>&"
        self.session.commit()
        text = self.run_command("block", "70001", "<b>Complete</b>", "&", "play")
        self.assertIn("&lt;player&gt;&amp;", text)
        self.assertIn("&lt;b&gt;Complete&lt;/b&gt; &amp; play", text)
        self.assertIn("&lt;b&gt;Complete&lt;/b&gt;", self.run_command("status", "70001"))
        self.assertIn("&lt;b&gt;Complete&lt;/b&gt;", self.run_command("list"))

    def test_invalid_reason_is_rejected_before_mutation(self):
        self.assertIn("500 characters", self.run_command("block", "70001", "x" * 501))
        self.assertFalse(self.user.tournament_only)

    def test_list_paginates_and_rejects_invalid_pages(self):
        count = self.handler.PAGE_SIZE + 1
        for index in range(count):
            self.session.add(self.User(telegram_id=71000 + index, username=f"Page{index}",
                                       tournament_only=True))
        self.session.commit()
        first = self.run_command("list")
        self.assertIn("1/2", first)
        self.assertEqual(first.count('href="tg://user'), self.handler.PAGE_SIZE)
        second = self.run_command("list", "2")
        self.assertIn("2/2", second)
        self.assertEqual(second.count('href="tg://user'), 1)
        self.assertIn("starting at page 1", self.run_command("list", "0"))

    def test_list_fits_telegram_limit_with_maximum_html_expansion(self):
        for index in range(self.handler.PAGE_SIZE + 1):
            self.session.add(self.User(
                telegram_id=9223372036854775800 + index,
                username='"' * 100, first_name='"' * 100,
                tournament_only=True, tournament_only_reason='"' * 500))
        self.session.commit()
        first = self.run_command("list")
        self.assertEqual(first.count('href="tg://user'), self.handler.PAGE_SIZE)
        self.assertIn("&quot;", first)
        self.assertIn("Next:", first)
        self.assertLessEqual(len(first), 4096)

    def test_help_has_no_side_effects(self):
        text = self.run_command()
        self.assertIn("/tournamentonly block", text)
        self.assertFalse(self.user.tournament_only)


if __name__ == "__main__":
    unittest.main()

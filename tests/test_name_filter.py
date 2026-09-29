"""The shared banned-words filter for team names and career names.

  • a banned word is caught through spacing, punctuation, look-alike symbols
    and stretched letters
  • an allowed word lets an innocent name through ("Scunthorpe")
  • a word with its own double letter never over-matches ("boob" vs "Bobby")
  • /teamname refuses a banned name and leaves the old one in place
  • names already in use that break the list are found, bots skipped
"""

import asyncio
import itertools
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap  # noqa: E402  (sibling helper; see its docstring)

from services import name_filter  # noqa: E402

_TG_IDS = itertools.count(97_001)
_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "services.config_service",
                 "services.activity_service", "handlers.team")


class BlockedWordTests(unittest.TestCase):
    CONF = {"blocklist": "slur, cunt\nboob", "allowlist": "scunthorpe"}

    def test_disguised_forms_are_caught(self):
        for name in ("Slur XI", "S l u r", "s.l.u.r", "$lur", "5lur",
                     "sluuur", "sluur", "The-SLUR-Kings"):
            self.assertEqual(name_filter.blocked_word(name, self.CONF), "slur",
                             name)

    def test_an_allowed_word_lets_an_innocent_name_through(self):
        self.assertIsNone(
            name_filter.blocked_word("Scunthorpe United", self.CONF))
        # ...but the banned word on its own is still caught.
        self.assertEqual(name_filter.blocked_word("Cunt XI", self.CONF),
                         "cunt")

    def test_a_double_letter_word_does_not_over_match(self):
        self.assertIsNone(name_filter.blocked_word("Bobby XI", self.CONF))
        self.assertEqual(name_filter.blocked_word("Boooob", self.CONF), "boob")

    def test_clean_names_pass(self):
        for name in ("Royal Challengers", "Mumbai Indians", "Chennai 11"):
            self.assertIsNone(name_filter.blocked_word(name, self.CONF), name)

    def test_an_empty_list_blocks_nothing(self):
        for raw in ("", "  ", ", ,\n"):
            self.assertIsNone(name_filter.blocked_word(
                "slur", {"blocklist": raw, "allowlist": ""}))

    def test_terms_split_on_commas_and_lines(self):
        self.assertEqual(name_filter.terms("Foo, b-a-r\n\nBaz ,"),
                         ["foo", "bar", "baz"])


def setUpModule():
    global _PREV_DATABASE_URL, _SAVED_MODULES, _TMP, _ENGINE

    _PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
    _SAVED_MODULES = _module_swap.save(_MODULE_NAMES)
    _module_swap.unload(_MODULE_NAMES)

    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"

    from database import Base, engine
    import models  # noqa: F401  (registers the tables on Base)

    _ENGINE = engine
    Base.metadata.create_all(bind=engine)


def tearDownModule():
    try:
        _ENGINE.dispose()
    except Exception:
        pass
    if _PREV_DATABASE_URL is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _PREV_DATABASE_URL
    _module_swap.restore(_SAVED_MODULES)
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        from database import get_session
        from services.config_service import save_config

        self.session = get_session()
        save_config(self.session, {"career_name_blocklist": "slur",
                                   "name_allowlist": ""})
        self.session.commit()

    def tearDown(self):
        from models import User
        self.session.query(User).delete()
        self.session.commit()
        self.session.close()

    def _user(self, team_name, telegram_id=None):
        from models import User
        user = User(telegram_id=telegram_id or next(_TG_IDS),
                    first_name="Owner", team_name=team_name)
        self.session.add(user)
        self.session.commit()
        return user

    def test_flagged_users_are_found_and_bots_skipped(self):
        bad = self._user("The 5lur XI")
        self._user("Royal Challengers")
        self._user(None)
        self._user("Slur Bots", telegram_id=-1)
        flagged = name_filter.find_flagged_users(self.session)
        self.assertEqual([(u.id, w) for u, w in flagged], [(bad.id, "slur")])

    def test_teamname_refuses_a_banned_name(self):
        from handlers import team

        user = self._user("Old Name")
        update = mock.MagicMock()
        update.effective_user.id = user.telegram_id
        update.message.reply_text = mock.AsyncMock()
        context = mock.MagicMock(args=["Sluuur", "Kings"])

        asyncio.run(team.teamname_handler(update, context))

        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("isn't allowed", reply)
        self.assertNotIn("slur", reply.lower())
        from database import get_session
        from models import User
        check = get_session()
        try:
            self.assertEqual(check.get(User, user.id).team_name, "Old Name")
        finally:
            check.close()

    def test_teamname_accepts_a_clean_name(self):
        from database import get_session
        from handlers import team
        from models import User

        user = self._user("Old Name")
        update = mock.MagicMock()
        update.effective_user.id = user.telegram_id
        update.message.reply_text = mock.AsyncMock()
        context = mock.MagicMock(args=["Royal", "Challengers"])

        asyncio.run(team.teamname_handler(update, context))
        check = get_session()
        try:
            self.assertEqual(check.get(User, user.id).team_name,
                             "Royal Challengers")
        finally:
            check.close()


if __name__ == "__main__":
    unittest.main()

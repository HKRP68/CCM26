"""A Career Player can never be sold, released or swapped away.

The card is the user's own identity and cost them thousands of gems, so every
route that removes a card from a roster has to refuse it: the chat /release and
/releasemultiple flows, the Mini App single and bulk sell endpoints, and the
overflow "drop one to make room" swap.
"""

import itertools
import os
import sys
import tempfile
import unittest

_TG_IDS = itertools.count(96_001)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "services.career_service",
                 "handlers.release", "services.overflow_service")


def setUpModule():
    global _PREV_DATABASE_URL, _SAVED_MODULES, _TMP, _ENGINE

    _PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
    _SAVED_MODULES = {name: sys.modules.get(name) for name in _MODULE_NAMES}
    for name in _MODULE_NAMES:
        sys.modules.pop(name, None)

    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"

    from database import Base, engine
    import models  # noqa: F401

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
    for name, module in _SAVED_MODULES.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


class CareerReleaseGuardTests(unittest.TestCase):
    def setUp(self):
        from database import get_session
        from models import Player, User, UserRoster
        from services import career_service as cs

        self.session = get_session()
        self.user = User(telegram_id=next(_TG_IDS), username="locked",
                         total_coins=1000, total_gems=0, roster_count=0)
        self.session.add(self.user)
        self.session.flush()

        # An ordinary card the user genuinely may sell, for contrast.
        self.ordinary = Player(name=f"Sellable Sam {self.user.id}",
                               version="Base card", rating=80,
                               category="Batsman", country="India",
                               bat_hand="Right", bowl_hand="Right",
                               bowl_style="Fast", bat_rating=80, bowl_rating=40)
        self.session.add(self.ordinary)
        self.session.flush()
        self.ordinary_entry = UserRoster(user_id=self.user.id,
                                         player_id=self.ordinary.id,
                                         order_position=1)
        self.session.add(self.ordinary_entry)

        result = cs.create_career_player(
            self.session, self.user, name=f"Never Sold {self.user.id}",
            country="India", bat_hand="Right", bowl_hand="Right",
            bowl_style="Fast", face_slot=1)
        self.career = result["player"]
        self.session.commit()
        self.career_entry = cs.career_roster_entry(self.session, self.user.id)

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def test_do_release_refuses_the_career_player(self):
        from handlers.release import _do_release
        result = _do_release(self.session, self.user,
                             [(self.career_entry, self.career)])
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "career_player")
        self.assertIn("can't be sold", result["message"])

    def test_the_roster_row_and_the_balance_are_untouched(self):
        from models import UserRoster
        from handlers.release import _do_release
        coins_before = self.user.total_coins
        _do_release(self.session, self.user, [(self.career_entry, self.career)])
        still_there = (self.session.query(UserRoster)
                       .filter(UserRoster.id == self.career_entry.id).first())
        self.assertIsNotNone(still_there)
        self.assertEqual(self.user.total_coins, coins_before)

    def test_a_mixed_batch_is_refused_whole(self):
        """Selling a range that contains the career card must sell nothing."""
        from models import UserRoster
        from handlers.release import _do_release
        result = _do_release(self.session, self.user, [
            (self.ordinary_entry, self.ordinary),
            (self.career_entry, self.career),
        ])
        self.assertFalse(result["success"])
        survivors = {r.id for r in self.session.query(UserRoster)
                     .filter(UserRoster.user_id == self.user.id).all()}
        self.assertIn(self.ordinary_entry.id, survivors,
                      "the ordinary card must not have been sold either")

    def test_an_ordinary_card_still_sells(self):
        """The guard must not have broken selling in general."""
        from handlers.release import _do_release
        result = _do_release(self.session, self.user,
                             [(self.ordinary_entry, self.ordinary)])
        self.assertTrue(result["success"], result.get("message"))
        self.assertEqual(len(result["released"]), 1)

    def test_find_by_arg_still_reports_it_so_the_handler_can_explain(self):
        """The lookup returns it; the handler is what filters and explains."""
        from handlers.release import _find_by_arg
        matches = _find_by_arg(self.session, self.user.id, "Never Sold")
        self.assertEqual(len(matches), 1)
        self.assertTrue(matches[0][1].is_career)

    def test_overflow_replace_will_not_drop_it(self):
        from models import Player, RosterOverflowClaim
        from services.overflow_service import resolve_replace

        incoming = Player(name=f"Incoming Ian {self.user.id}",
                          version="Base card", rating=90, category="Bowler",
                          country="India", bat_hand="Right", bowl_hand="Right",
                          bowl_style="Fast")
        self.session.add(incoming)
        self.session.flush()
        claim = RosterOverflowClaim(user_id=self.user.id,
                                    player_id=incoming.id, source="reward")
        self.session.add(claim)
        self.session.flush()

        result = resolve_replace(self.session, self.user, claim.id,
                                 self.career_entry.id)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "career_player")


class CareerLockedMessageTests(unittest.TestCase):
    def test_the_message_says_both_sold_and_traded(self):
        from services.career_service import CAREER_LOCKED_MESSAGE
        self.assertIn("sold", CAREER_LOCKED_MESSAGE)
        self.assertIn("traded", CAREER_LOCKED_MESSAGE)


if __name__ == "__main__":
    unittest.main()

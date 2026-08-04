"""Tests for the website's "reassign quests for all users" action.

An admin who edits the quest catalogue used to have to wait for the daily or
monthly rollover before anyone saw the change, because ensure_quests_assigned
deliberately does nothing once a user has a set for the period. Reassigning
clears the period so everyone re-draws — losing progress (that is the point)
but never a reward that was already earned.
"""

import os
import sys
import tempfile
import unittest
import itertools
from datetime import datetime

_TG_IDS = itertools.count(98_401)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "services.quest_service")


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


def _make_user(session):
    from models import User
    tid = next(_TG_IDS)
    user = User(telegram_id=tid, username=f"u{tid}", first_name="T",
                total_coins=0, total_gems=0, quest_points=0)
    session.add(user)
    session.flush()
    return user


def _make_quest(session, quest_type, name, **kw):
    from models import Quest
    quest = Quest(name=name, description=name, quest_type=quest_type,
                  event_key=kw.pop("event_key", "match_won"),
                  target_count=kw.pop("target_count", 1),
                  reward_points=kw.pop("reward_points", 5),
                  reward_coins=kw.pop("reward_coins", 0),
                  reward_gems=kw.pop("reward_gems", 0),
                  is_active=True, **kw)
    session.add(quest)
    session.flush()
    return quest


def _assign(session, user, quest, *, period=None, progress=0,
            completed=False, claimed=False, assigned=True):
    from models import UserQuestProgress
    from services.quest_service import period_key_for
    row = UserQuestProgress(
        user_id=user.id, quest_id=quest.id,
        period_key=period or period_key_for(quest.quest_type),
        progress=progress, completed=completed, claimed=claimed,
        assigned=assigned, last_updated=datetime.utcnow())
    session.add(row)
    session.flush()
    return row


class ReassignAllUsersTests(unittest.TestCase):
    def setUp(self):
        from database import get_session
        self.session = get_session()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def _rows_for(self, quest_type):
        from models import Quest, UserQuestProgress
        from services.quest_service import period_key_for
        return (self.session.query(UserQuestProgress)
                .join(Quest, Quest.id == UserQuestProgress.quest_id)
                .filter(Quest.quest_type == quest_type,
                        UserQuestProgress.period_key
                        == period_key_for(quest_type))
                .count())

    def test_current_period_assignments_are_cleared_for_every_user(self):
        from services.quest_service import reassign_all_users
        quest = _make_quest(self.session, "daily", "Daily A")
        users = [_make_user(self.session) for _ in range(3)]
        for user in users:
            _assign(self.session, user, quest, progress=1)
        self.assertEqual(self._rows_for("daily"), 3)

        result = reassign_all_users(self.session, "daily")
        self.assertEqual(result["cleared"], 3)
        self.assertEqual(self._rows_for("daily"), 0)

    def test_completed_but_unclaimed_quests_are_paid_out_first(self):
        from services.quest_service import reassign_all_users
        quest = _make_quest(self.session, "daily", "Daily Pay",
                            reward_points=7, reward_coins=250, reward_gems=2)
        user = _make_user(self.session)
        _assign(self.session, user, quest, progress=1, completed=True)

        result = reassign_all_users(self.session, "daily")
        self.assertEqual(result["auto_claimed"], 1)
        self.assertEqual(result["points"], 7)
        self.assertEqual(result["coins"], 250)
        self.assertEqual(result["gems"], 2)
        self.assertEqual(user.quest_points, 7)
        self.assertEqual(user.total_coins, 250)
        self.assertEqual(user.total_gems, 2)

    def test_already_claimed_quests_are_not_paid_twice(self):
        from services.quest_service import reassign_all_users
        quest = _make_quest(self.session, "daily", "Daily Claimed",
                            reward_coins=500)
        user = _make_user(self.session)
        _assign(self.session, user, quest, progress=1,
                completed=True, claimed=True)

        result = reassign_all_users(self.session, "daily")
        self.assertEqual(result["auto_claimed"], 0)
        self.assertEqual(user.total_coins, 0)

    def test_unassigned_completed_rows_are_not_paid_out(self):
        # A row the user was never dealt must not pay, even if it somehow
        # carries completed=True — it is deleted with the rest of the period.
        from services.quest_service import reassign_all_users
        quest = _make_quest(self.session, "daily", "Daily Stale",
                            reward_points=9, reward_coins=400, reward_gems=3)
        user = _make_user(self.session)
        _assign(self.session, user, quest, progress=1,
                completed=True, assigned=False)

        result = reassign_all_users(self.session, "daily")
        self.assertEqual(result["auto_claimed"], 0)
        self.assertEqual(user.quest_points, 0)
        self.assertEqual(user.total_coins, 0)
        self.assertEqual(user.total_gems, 0)
        self.assertEqual(self._rows_for("daily"), 0)

    def test_incomplete_progress_is_dropped_without_payment(self):
        from services.quest_service import reassign_all_users
        quest = _make_quest(self.session, "daily", "Daily Partial",
                            target_count=5, reward_coins=999)
        user = _make_user(self.session)
        _assign(self.session, user, quest, progress=3)

        result = reassign_all_users(self.session, "daily")
        self.assertEqual(result["auto_claimed"], 0)
        self.assertEqual(user.total_coins, 0)
        self.assertEqual(self._rows_for("daily"), 0)

    def test_other_quest_types_are_untouched(self):
        from services.quest_service import reassign_all_users
        daily = _make_quest(self.session, "daily", "Daily B")
        monthly = _make_quest(self.session, "monthly", "Monthly B")
        user = _make_user(self.session)
        _assign(self.session, user, daily)
        _assign(self.session, user, monthly)

        reassign_all_users(self.session, "daily")
        self.assertEqual(self._rows_for("daily"), 0)
        self.assertEqual(self._rows_for("monthly"), 1)

    def test_previous_periods_are_left_as_history(self):
        from services.quest_service import reassign_all_users
        from models import UserQuestProgress
        quest = _make_quest(self.session, "daily", "Daily History")
        user = _make_user(self.session)
        _assign(self.session, user, quest, period="1999-01-01",
                completed=True, claimed=True)
        _assign(self.session, user, quest)

        reassign_all_users(self.session, "daily")
        old = (self.session.query(UserQuestProgress)
               .filter(UserQuestProgress.period_key == "1999-01-01").count())
        self.assertEqual(old, 1, "an old period's history was deleted")

    def test_unassigned_rows_go_too_so_progress_cannot_be_inherited(self):
        # A re-picked quest must start at zero. Anything left behind for the
        # period would be found by the next assignment pass instead.
        from services.quest_service import reassign_all_users
        quest = _make_quest(self.session, "daily", "Daily Stub")
        user = _make_user(self.session)
        _assign(self.session, user, quest, progress=4, assigned=False)

        reassign_all_users(self.session, "daily")
        self.assertEqual(self._rows_for("daily"), 0)

    def test_reassign_lets_ensure_deal_a_fresh_set(self):
        # The whole point: after a reassign the user is back in the
        # "not dealt yet" state and draws again from the live catalogue.
        from services.quest_service import ensure_quests_assigned, reassign_all_users
        old = _make_quest(self.session, "daily", "Old Daily", event_key="claim")
        user = _make_user(self.session)
        _assign(self.session, user, old)

        # Retire the old quest and publish a new one, as an admin would.
        old.is_active = False
        _make_quest(self.session, "daily", "New Daily", event_key="daily")
        self.session.flush()

        reassign_all_users(self.session, "daily")
        result = ensure_quests_assigned(self.session, user.id, "daily")
        names = [q.name for q in result["assigned"]]
        self.assertEqual(names, ["New Daily"])

    def test_no_quests_of_that_type_is_a_no_op(self):
        from services.quest_service import reassign_all_users
        result = reassign_all_users(self.session, "weekly")
        self.assertEqual(result["cleared"], 0)
        self.assertEqual(result["auto_claimed"], 0)


if __name__ == "__main__":
    unittest.main()

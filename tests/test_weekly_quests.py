"""Weekly quests, the Career Player quest pool, and the streak jackpot.

Before this, ``period_key_for`` fell through to the monthly key for anything
that was not "daily", so the Mini App's Weekly tab was permanently empty. Weekly
is now a real cadence that rolls over on Monday, and it carries the Career
Player quests — which are only handed to users who actually have a career card,
and which build a streak that pays gems when cleared week after week.
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
import itertools

_TG_IDS = itertools.count(97_201)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "services.quest_service",
                 "services.career_service", "services.config_service")


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


class PeriodKeyTests(unittest.TestCase):
    def test_weekly_has_its_own_key_and_is_not_the_monthly_one(self):
        from services.quest_service import monthly_period_key, period_key_for
        weekly = period_key_for("weekly")
        self.assertIn("-W", weekly)
        self.assertNotEqual(weekly, monthly_period_key())

    def test_the_week_turns_over_on_monday(self):
        from services.quest_service import weekly_period_key
        monday = datetime(2026, 8, 3)
        self.assertEqual(weekly_period_key(monday), "2026-W32")
        # The Sunday before belongs to the previous week...
        self.assertEqual(weekly_period_key(monday - timedelta(days=1)),
                         "2026-W31")
        # ...and every day up to the next Sunday stays in this one.
        self.assertEqual(weekly_period_key(monday + timedelta(days=6)),
                         "2026-W32")
        self.assertEqual(weekly_period_key(monday + timedelta(days=7)),
                         "2026-W33")

    def test_daily_and_monthly_are_unchanged(self):
        from services.quest_service import (daily_period_key,
                                            monthly_period_key,
                                            period_key_for)
        self.assertEqual(period_key_for("daily"), daily_period_key())
        self.assertEqual(period_key_for("monthly"), monthly_period_key())


def _make_user(session, telegram_id, gems=0):
    from models import User
    user = User(telegram_id=telegram_id, username=f"u{telegram_id}",
                total_coins=0, total_gems=gems, roster_count=0)
    session.add(user)
    session.flush()
    return user


def _career_quests(session, count=3, gems=15):
    from models import Quest
    quests = []
    for index in range(count):
        quest = Quest(name=f"Career Quest {index}",
                      description="Do a career thing",
                      quest_type="weekly", event_key="career_match_played",
                      target_count=1, reward_points=8, reward_coins=0,
                      reward_gems=gems, is_active=True, career_only=True,
                      emoji="🎖", sort_order=index)
        session.add(quest)
        quests.append(quest)
    session.flush()
    return quests


class CareerQuestAssignmentTests(unittest.TestCase):
    def setUp(self):
        from database import get_session
        from models import Quest, UserQuestProgress

        self.session = get_session()
        self.session.query(UserQuestProgress).delete()
        self.session.query(Quest).delete()
        self.quests = _career_quests(self.session)
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def test_a_user_without_a_career_player_gets_none_of_them(self):
        from services.quest_service import ensure_quests_assigned
        user = _make_user(self.session, 97_101)
        self.session.commit()
        result = ensure_quests_assigned(self.session, user.id, "weekly")
        self.session.commit()
        self.assertEqual(result["assigned"], [])

    def test_a_career_owner_gets_them(self):
        from services import career_service as cs
        from services.quest_service import ensure_quests_assigned

        user = _make_user(self.session, 97_102)
        cs.create_career_player(self.session, user, name="Weekly Wanda",
                                country="India", bat_hand="Right",
                                bowl_hand="Right", bowl_style="Fast",
                                face_slot=1)
        self.session.commit()
        result = ensure_quests_assigned(self.session, user.id, "weekly")
        self.session.commit()
        self.assertTrue(result["assigned"])
        for quest in result["assigned"]:
            self.assertTrue(quest.career_only)

    def test_an_ordinary_weekly_quest_reaches_everyone(self):
        from models import Quest
        from services.quest_service import ensure_quests_assigned

        self.session.add(Quest(name="Open Weekly", description="Play matches",
                               quest_type="weekly", event_key="match_played",
                               target_count=3, reward_points=8, reward_gems=5,
                               is_active=True, career_only=False, emoji="🎯"))
        self.session.flush()
        user = _make_user(self.session, 97_103)
        self.session.commit()
        result = ensure_quests_assigned(self.session, user.id, "weekly")
        self.session.commit()
        self.assertEqual([q.name for q in result["assigned"]], ["Open Weekly"])

    def test_career_quests_pay_gems(self):
        from models import Quest
        rows = self.session.query(Quest).filter(Quest.career_only.is_(True)).all()
        self.assertTrue(rows)
        for quest in rows:
            self.assertEqual(quest.reward_gems, 15)


class CareerStreakTests(unittest.TestCase):
    def setUp(self):
        from database import get_session
        from models import GameConfig, Quest, UserQuestProgress
        from services import career_service as cs
        from services import config_service

        self.session = get_session()
        self.session.query(UserQuestProgress).delete()
        self.session.query(Quest).delete()
        self.quests = _career_quests(self.session)

        if not self.session.query(GameConfig).first():
            self.session.add(GameConfig())
        self.session.flush()
        config_service._CACHE["data"] = None

        self.user = _make_user(self.session, next(_TG_IDS))
        cs.create_career_player(self.session, self.user, name=f"Streaky {self.user.id}",
                                country="India", bat_hand="Right",
                                bowl_hand="Right", bowl_style="Fast",
                                face_slot=1)
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def _log_week(self, weeks_ahead, cleared=True):
        """Record the user's assigned career quests for one past week."""
        from models import UserQuestProgress
        from services.quest_service import weekly_period_key
        period = weekly_period_key(datetime.utcnow() + timedelta(weeks=weeks_ahead))
        for index, quest in enumerate(self.quests):
            done = cleared or index < len(self.quests) - 1
            self.session.add(UserQuestProgress(
                user_id=self.user.id, quest_id=quest.id, period_key=period,
                progress=quest.target_count if done else 0,
                completed=done, claimed=done, assigned=True))
        self.session.flush()

    def _rollover(self, weeks_ahead):
        from services.quest_service import (_evaluate_career_streak,
                                            weekly_period_key)
        now = datetime.utcnow() + timedelta(weeks=weeks_ahead)
        result = _evaluate_career_streak(self.session, self.user,
                                         weekly_period_key(now), now)
        self.session.flush()
        return result

    def test_a_cleared_week_increments_the_streak(self):
        self._log_week(0, cleared=True)
        result = self._rollover(1)
        self.assertTrue(result["cleared"])
        self.assertEqual(self.user.career_weekly_streak, 1)

    def test_four_cleared_weeks_pay_the_jackpot_once(self):
        gems_before = self.user.total_gems or 0
        payouts = []
        for week in range(4):
            self._log_week(week, cleared=True)
            payouts.append(self._rollover(week + 1)["bonus"])
        self.assertEqual(payouts, [0, 0, 0, 100])
        self.assertEqual(self.user.career_weekly_streak, 4)
        self.assertEqual(self.user.total_gems, gems_before + 100)

    def test_the_rollover_is_idempotent_within_a_week(self):
        self._log_week(0, cleared=True)
        self._rollover(1)
        gems = self.user.total_gems
        streak = self.user.career_weekly_streak
        self.assertIsNone(self._rollover(1), "a second call must do nothing")
        self.assertEqual(self.user.total_gems, gems)
        self.assertEqual(self.user.career_weekly_streak, streak)

    def test_one_missed_quest_resets_the_streak(self):
        for week in range(2):
            self._log_week(week, cleared=True)
            self._rollover(week + 1)
        self.assertEqual(self.user.career_weekly_streak, 2)

        self._log_week(2, cleared=False)
        result = self._rollover(3)
        self.assertFalse(result["cleared"])
        self.assertEqual(self.user.career_weekly_streak, 0)
        self.assertEqual(result["lost"], 2)

    def test_the_best_streak_survives_a_reset(self):
        for week in range(2):
            self._log_week(week, cleared=True)
            self._rollover(week + 1)
        self._log_week(2, cleared=False)
        self._rollover(3)
        self.assertEqual(self.user.career_weekly_best_streak, 2)

    def test_a_week_with_no_career_quests_neither_builds_nor_breaks(self):
        self.user.career_weekly_streak = 3
        self.session.flush()
        # No UserQuestProgress rows logged for the closing week at all.
        self.assertIsNone(self._rollover(1))
        self.assertEqual(self.user.career_weekly_streak, 3)


if __name__ == "__main__":
    unittest.main()

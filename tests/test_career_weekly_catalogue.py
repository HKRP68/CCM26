"""The Career Player weekly quest catalogue and how a week's five are dealt.

Every owner gets five career quests a week, drawn two batting / two bowling /
one all-round from a catalogue far larger than five. Two things have to hold for
that to work, and both are easy to break by accident:

* **Every quest in the catalogue must be reachable.** A quest whose event key is
  in no bucket can never be drawn by the stratified deal, and a quest whose
  event key nothing fires can never be *cleared* — which, because the streak
  needs every assigned career quest completed, would put the jackpot out of
  reach for whoever drew it.
* **The deal must always produce five.** Career quests get their own slots
  rather than competing with the ordinary weekly ones, so an owner is never
  handed a week with no career card and therefore no week that can be judged.
"""

import itertools
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

_TG_IDS = itertools.count(98_401)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
# "admin" is in here because one test reads its EVENT_KEYS list. Importing it
# binds a Flask app to whatever DATABASE_URL is live, so it has to be swapped
# out and back with everything else — otherwise the copy this module imports
# outlives our temp database and the next test file to use it queries a file
# that has already been deleted.
_MODULE_NAMES = ("database", "models", "config", "services.quest_service",
                 "services.career_service", "services.config_service", "admin")


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

    from database import get_session
    from models import CareerFace
    session = get_session()
    try:
        session.add(CareerFace(slot=1, label="Face 1", sort_order=1,
                               is_active=True))
        session.commit()
    finally:
        session.close()


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


def _fired_career_events():
    """Career event keys ``track_user_match_quests`` actually emits.

    Read off the source rather than by running a match, because the point is to
    catch a key that was added to a bucket or the catalogue and then never
    wired up — which no single simulated match would reveal.
    """
    import services.quest_service as qs
    source = open(qs.__file__.replace(".pyc", ".py")).read()
    body = source.split("def track_user_match_quests")[1]
    fired = set(re.findall(
        r'safe_track\(session, uid, "(career_[a-z0-9_]+)"', body))
    fired |= set(re.findall(r'\("(career_[a-z0-9_]+)",\s*career_', body))
    return fired


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        from seed_career_quests import CAREER_QUESTS
        self.catalogue = CAREER_QUESTS

    def test_every_quest_name_is_unique(self):
        names = [q[0] for q in self.catalogue]
        self.assertEqual(len(names), len(set(names)))

    def test_every_quest_falls_in_exactly_one_bucket(self):
        """A quest in no bucket can never be drawn; in two, it skews the mix."""
        from services.quest_service import (CAREER_ALL_EVENTS,
                                            CAREER_BAT_EVENTS,
                                            CAREER_BOWL_EVENTS)
        buckets = (CAREER_BAT_EVENTS, CAREER_BOWL_EVENTS, CAREER_ALL_EVENTS)
        for name, _desc, event_key, *_rest in self.catalogue:
            hits = sum(1 for bucket in buckets if event_key in bucket)
            self.assertEqual(hits, 1, f"{name} ({event_key}) is in {hits} buckets")

    def test_every_bucket_can_fill_its_share_of_the_deal(self):
        from services.quest_service import CAREER_QUEST_MIX
        for events, count in CAREER_QUEST_MIX:
            available = [q for q in self.catalogue if q[2] in events]
            self.assertGreaterEqual(
                len(available), count,
                f"bucket has {len(available)} quests but the deal wants {count}")

    def test_the_catalogue_is_far_larger_than_one_week(self):
        from services.quest_service import CAREER_QUESTS_PER_USER
        self.assertGreater(len(self.catalogue), CAREER_QUESTS_PER_USER * 5)

    def test_every_bucketed_event_is_actually_fired(self):
        from services.quest_service import (CAREER_ALL_EVENTS,
                                            CAREER_BAT_EVENTS,
                                            CAREER_BOWL_EVENTS)
        bucketed = CAREER_BAT_EVENTS | CAREER_BOWL_EVENTS | CAREER_ALL_EVENTS
        self.assertEqual(bucketed - _fired_career_events(), set(),
                         "bucketed but never fired — unclearable quests")

    def test_every_fired_event_is_bucketed(self):
        from services.quest_service import (CAREER_ALL_EVENTS,
                                            CAREER_BAT_EVENTS,
                                            CAREER_BOWL_EVENTS)
        bucketed = CAREER_BAT_EVENTS | CAREER_BOWL_EVENTS | CAREER_ALL_EVENTS
        self.assertEqual(_fired_career_events() - bucketed, set(),
                         "fired but in no bucket — undrawable quests")

    def test_every_event_key_is_offered_on_the_website(self):
        """An admin must be able to build a quest on any key the game fires."""
        import admin
        offered = {key for key, _label in admin.EVENT_KEYS}
        for name, _desc, event_key, *_rest in self.catalogue:
            self.assertIn(event_key, offered, f"{name} uses an unlisted key")

    def test_targets_and_tiers_are_sane(self):
        from seed_career_quests import TIERS
        for name, description, _key, target, emoji, tier in self.catalogue:
            self.assertGreater(target, 0, name)
            self.assertIn(tier, TIERS, name)
            self.assertTrue(description.strip(), name)
            self.assertTrue(emoji.strip(), name)
            self.assertLessEqual(len(name), 100, name)
            self.assertLessEqual(len(description), 300, name)


class SeedTests(unittest.TestCase):
    def setUp(self):
        from database import get_session
        from models import Quest, UserQuestProgress
        self.session = get_session()
        self.session.query(UserQuestProgress).delete()
        self.session.query(Quest).delete()
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def test_seeding_creates_the_whole_catalogue(self):
        from models import Quest
        from seed_career_quests import CAREER_QUESTS, seed
        created, updated, retired = seed(self.session)
        self.session.commit()
        self.assertEqual((created, updated, retired),
                         (len(CAREER_QUESTS), 0, 0))
        live = (self.session.query(Quest)
                .filter(Quest.career_only.is_(True),
                        Quest.is_active.is_(True)).count())
        self.assertEqual(live, len(CAREER_QUESTS))

    def test_re_seeding_updates_in_place(self):
        from models import Quest
        from seed_career_quests import CAREER_QUESTS, seed
        seed(self.session)
        self.session.commit()
        created, updated, retired = seed(self.session)
        self.session.commit()
        self.assertEqual((created, updated, retired),
                         (0, len(CAREER_QUESTS), 0))
        self.assertEqual(self.session.query(Quest).count(), len(CAREER_QUESTS))

    def test_a_quest_dropped_from_the_catalogue_is_retired(self):
        """A rebalance must not leave last generation's quests in the draw."""
        from models import Quest
        from seed_career_quests import seed
        self.session.add(Quest(
            name="Career: Ancient History", description="From an older build",
            quest_type="weekly", event_key="career_runs_scored",
            target_count=10, reward_points=8, reward_gems=15, is_active=True,
            career_only=True, emoji="🎖", sort_order=1))
        self.session.commit()
        _created, _updated, retired = seed(self.session)
        self.session.commit()
        self.assertEqual(retired, 1)
        old = (self.session.query(Quest)
               .filter(Quest.name == "Career: Ancient History").first())
        self.assertFalse(old.is_active, "kept for history, out of the draw")

    def test_gems_scale_with_the_tier_and_points_do_not(self):
        from models import Quest
        from seed_career_quests import (CAREER_QUESTS,
                                        CAREER_WEEKLY_REWARD_POINTS, TIERS,
                                        career_quest_gems, seed)
        seed(self.session)
        self.session.commit()
        base = career_quest_gems(self.session)
        by_name = {q.name: q for q in self.session.query(Quest).all()}
        for name, _desc, _key, _target, _emoji, tier in CAREER_QUESTS:
            quest = by_name[name]
            self.assertEqual(quest.reward_points, CAREER_WEEKLY_REWARD_POINTS)
            self.assertEqual(quest.reward_gems, base * TIERS[tier])

    def test_an_ordinary_weekly_quest_is_left_alone(self):
        from models import Quest
        from seed_career_quests import seed
        self.session.add(Quest(
            name="Weekly Grinder", description="Play matches",
            quest_type="weekly", event_key="match_played", target_count=10,
            reward_points=8, reward_gems=0, is_active=True, career_only=False,
            emoji="🎯", sort_order=1))
        self.session.commit()
        seed(self.session)
        self.session.commit()
        ordinary = (self.session.query(Quest)
                    .filter(Quest.name == "Weekly Grinder").first())
        self.assertTrue(ordinary.is_active)


class WeeklyDealTests(unittest.TestCase):
    """Five career quests a week, balanced, and different from last week's."""

    def setUp(self):
        from database import get_session
        from models import GameConfig, Quest, UserQuestProgress, User
        from seed_career_quests import seed
        from services import career_service as cs
        from services import config_service

        self.session = get_session()
        self.session.query(UserQuestProgress).delete()
        self.session.query(Quest).delete()
        seed(self.session)
        if not self.session.query(GameConfig).first():
            self.session.add(GameConfig())
        self.session.flush()
        config_service._CACHE["data"] = None

        self.user = User(telegram_id=next(_TG_IDS), username="dealt",
                         total_coins=0, total_gems=0, roster_count=0)
        self.session.add(self.user)
        self.session.flush()
        created = cs.create_career_player(
            self.session, self.user, name=f"Dealt {self.user.id}",
            country="India", bat_hand="Right", bowl_hand="Right",
            bowl_style="Fast", face_slot=1)
        self.assertTrue(created["ok"], created.get("message"))
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def _new_owner(self):
        from models import User
        from services import career_service as cs
        user = User(telegram_id=next(_TG_IDS), username="owner",
                    total_coins=0, total_gems=0, roster_count=0)
        self.session.add(user)
        self.session.flush()
        created = cs.create_career_player(
            self.session, user, name=f"Owner {user.id}", country="India",
            bat_hand="Right", bowl_hand="Right", bowl_style="Fast",
            face_slot=1)
        self.assertTrue(created["ok"], created.get("message"))
        self.session.commit()
        return user

    def _deal(self, user):
        from services.quest_service import ensure_quests_assigned
        result = ensure_quests_assigned(self.session, user.id, "weekly")
        self.session.commit()
        return [q for q in result["assigned"]
                if getattr(q, "career_only", False)]

    def test_an_owner_gets_exactly_five_career_quests(self):
        from services.quest_service import CAREER_QUESTS_PER_USER
        self.assertEqual(len(self._deal(self.user)), CAREER_QUESTS_PER_USER)

    def test_the_five_are_two_batting_two_bowling_one_all_round(self):
        from services.quest_service import (CAREER_ALL_EVENTS,
                                            CAREER_BAT_EVENTS,
                                            CAREER_BOWL_EVENTS)
        dealt = self._deal(self.user)
        keys = [q.event_key for q in dealt]
        self.assertEqual(sum(1 for k in keys if k in CAREER_BAT_EVENTS), 2)
        self.assertEqual(sum(1 for k in keys if k in CAREER_BOWL_EVENTS), 2)
        self.assertEqual(sum(1 for k in keys if k in CAREER_ALL_EVENTS), 1)

    def test_no_quest_is_dealt_twice_in_the_same_week(self):
        dealt = self._deal(self.user)
        ids = [q.id for q in dealt]
        self.assertEqual(len(ids), len(set(ids)))

    def test_career_quests_do_not_eat_the_ordinary_weekly_slots(self):
        """They used to share, so an owner could get no career quest at all."""
        from models import Quest
        from services.quest_service import (ensure_quests_assigned,
                                            WEEKLY_QUESTS_PER_USER)
        for index in range(4):
            self.session.add(Quest(
                name=f"Plain Weekly {index}", description="Ordinary",
                quest_type="weekly", event_key="match_played", target_count=5,
                reward_points=8, reward_gems=0, is_active=True,
                career_only=False, emoji="🎯", sort_order=index))
        self.session.commit()

        result = ensure_quests_assigned(self.session, self.user.id, "weekly")
        self.session.commit()
        career = [q for q in result["assigned"] if q.career_only]
        plain = [q for q in result["assigned"] if not q.career_only]
        self.assertEqual(len(career), 5)
        self.assertEqual(len(plain), WEEKLY_QUESTS_PER_USER)

    def test_a_user_without_a_career_player_gets_none_of_them(self):
        from models import User
        from services.quest_service import ensure_quests_assigned
        outsider = User(telegram_id=next(_TG_IDS), username="nocareer",
                        total_coins=0, total_gems=0, roster_count=0)
        self.session.add(outsider)
        self.session.commit()
        result = ensure_quests_assigned(self.session, outsider.id, "weekly")
        self.session.commit()
        self.assertEqual([q for q in result["assigned"] if q.career_only], [])

    def test_two_owners_are_very_unlikely_to_share_a_week(self):
        """Not a guarantee — a large catalogue and a random deal, so assert the
        distribution rather than any single pair."""
        sets = []
        for _ in range(8):
            sets.append(frozenset(q.id for q in self._deal(self._new_owner())))
        self.assertGreaterEqual(
            len(set(sets)), 7, "eight owners should not share weeks")

    def test_last_weeks_five_are_pushed_to_the_back_of_the_queue(self):
        from models import UserQuestProgress
        from services.quest_service import (_pick_career_weeklies,
                                            weekly_period_key)
        from models import Quest

        pool = (self.session.query(Quest)
                .filter(Quest.career_only.is_(True),
                        Quest.is_active.is_(True)).all())
        now = datetime.utcnow()
        last_week = weekly_period_key(now - timedelta(days=7))

        first = _pick_career_weeklies(self.session, self.user.id, pool, now)
        for quest in first:
            self.session.add(UserQuestProgress(
                user_id=self.user.id, quest_id=quest.id, period_key=last_week,
                progress=0, completed=False, claimed=False, assigned=True))
        self.session.commit()

        # With a catalogue this size every bucket has plenty of unseen quests,
        # so none of last week's five should come back.
        second = _pick_career_weeklies(self.session, self.user.id, pool, now)
        self.assertEqual(
            {q.id for q in first} & {q.id for q in second}, set(),
            "last week's quests should not repeat while fresh ones exist")

    def test_a_week_is_never_all_marathons(self):
        """Five stretch quests is not a hard week, it is a written-off one.

        The streak needs every assigned quest cleared, so a week of five
        top-tier quests writes off the jackpot too. Each two-slot bucket takes
        one from its lighter half, which bounds the week at three heavy quests.
        """
        from models import Quest
        from services.quest_service import _pick_career_weeklies

        pool = (self.session.query(Quest)
                .filter(Quest.career_only.is_(True),
                        Quest.is_active.is_(True)).all())
        top_gems = max(q.reward_gems for q in pool)
        for _ in range(25):
            dealt = _pick_career_weeklies(self.session, self.user.id, pool,
                                          datetime.utcnow())
            marathons = sum(1 for q in dealt if q.reward_gems == top_gems)
            self.assertLessEqual(
                marathons, 3,
                f"dealt {marathons} top-tier quests: "
                f"{[q.name for q in dealt]}")

    def test_every_week_contains_something_achievable(self):
        """At least one quest from the easier end of both skill buckets."""
        from models import Quest
        from services.quest_service import (CAREER_BAT_EVENTS,
                                            CAREER_BOWL_EVENTS,
                                            _by_effort,
                                            _pick_career_weeklies)
        pool = (self.session.query(Quest)
                .filter(Quest.career_only.is_(True),
                        Quest.is_active.is_(True)).all())
        for events in (CAREER_BAT_EVENTS, CAREER_BOWL_EVENTS):
            lighter, _heavier = _by_effort(
                [q for q in pool if q.event_key in events])
            light_ids = {q.id for q in lighter}
            for _ in range(15):
                dealt = _pick_career_weeklies(self.session, self.user.id, pool,
                                              datetime.utcnow())
                self.assertTrue(
                    light_ids & {q.id for q in dealt},
                    f"no lighter quest from this bucket: "
                    f"{[q.name for q in dealt]}")

    def test_a_thin_bucket_still_yields_five(self):
        """An admin who deactivates most of a category must not shrink the deal."""
        from models import Quest
        from services.quest_service import (CAREER_BOWL_EVENTS,
                                            CAREER_QUESTS_PER_USER,
                                            _pick_career_weeklies)
        pool = (self.session.query(Quest)
                .filter(Quest.career_only.is_(True),
                        Quest.is_active.is_(True)).all())
        thin = [q for q in pool if q.event_key not in CAREER_BOWL_EVENTS]
        thin += [q for q in pool if q.event_key in CAREER_BOWL_EVENTS][:1]

        dealt = _pick_career_weeklies(self.session, self.user.id, thin,
                                      datetime.utcnow())
        self.assertEqual(len(dealt), CAREER_QUESTS_PER_USER)
        self.assertEqual(len({q.id for q in dealt}), CAREER_QUESTS_PER_USER)

    def test_a_catalogue_smaller_than_the_deal_does_not_raise(self):
        from models import Quest
        from services.quest_service import _pick_career_weeklies
        pool = (self.session.query(Quest)
                .filter(Quest.career_only.is_(True),
                        Quest.is_active.is_(True)).all()[:3])
        dealt = _pick_career_weeklies(self.session, self.user.id, pool,
                                      datetime.utcnow())
        self.assertEqual(len(dealt), 3)


if __name__ == "__main__":
    unittest.main()

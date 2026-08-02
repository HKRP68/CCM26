"""Which matches feed quests, and the events they fire.

Three separate reports sat behind this file:

  * quests only moved in some match modes — /lp and Challenge League finished
    through a code path that fired nothing at all;
  * quests "sometimes don't count" — most of the daily/monthly catalogue was
    bulk-imported onto ``event_key='manual'``, which nothing in the game fires,
    and those quests were still being dealt at random; and
  * a match against a far weaker XI still ticked quests along, even though the
    fair-match gate had already voided it for stats, coins and W/L.
"""

import itertools
import os
import sys
import tempfile
import unittest

_TG_IDS = itertools.count(98_401)

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


def _make_user(session, telegram_id):
    from models import User
    user = User(telegram_id=telegram_id, username=f"u{telegram_id}",
                total_coins=0, total_gems=0, roster_count=0)
    session.add(user)
    session.flush()
    return user


# ════════════════════════════════════════════════════════════════════
# Which matches count
# ════════════════════════════════════════════════════════════════════

class MatchCountsForQuestsTests(unittest.TestCase):
    """The single gate every finalize goes through."""

    def test_a_plain_user_vs_user_match_counts(self):
        from services.quest_service import match_counts_for_quests
        self.assertTrue(match_counts_for_quests({"bat_team_id": 1}))

    def test_an_unknown_state_counts(self):
        # A caller with no state must not silently lose every quest.
        from services.quest_service import match_counts_for_quests
        self.assertTrue(match_counts_for_quests(None))
        self.assertTrue(match_counts_for_quests({}))

    def test_bot_matches_do_not_count(self):
        from services.quest_service import match_counts_for_quests
        self.assertFalse(match_counts_for_quests({}, is_vsbot=True))
        self.assertFalse(match_counts_for_quests({"is_vsbot": True}))
        self.assertFalse(match_counts_for_quests({"is_bot_match": True}))

    def test_matches_nobody_played_do_not_count(self):
        from services.quest_service import match_counts_for_quests
        self.assertFalse(match_counts_for_quests({"is_spectator": True}))
        self.assertFalse(match_counts_for_quests({"is_bot_vs_bot": True}))

    def test_a_team_overall_mismatch_does_not_count(self):
        # stats_disabled is what the 10+ Team Overall gap sets.
        from services.quest_service import match_counts_for_quests
        self.assertFalse(match_counts_for_quests({"stats_disabled": True}))

    def test_unranked_practice_does_not_count(self):
        from services.quest_service import match_counts_for_quests
        self.assertFalse(match_counts_for_quests({"unranked": True}))


class MismatchGateTests(unittest.TestCase):
    """A voided match must leave every quest exactly where it was."""

    def setUp(self):
        from database import get_session
        from models import Quest, UserQuestProgress, Player, UserRoster
        from services.quest_service import daily_period_key

        self.session = get_session()
        self.session.query(UserQuestProgress).delete()
        self.session.query(Quest).delete()

        self.quest = Quest(name="Runs Today", description="Score runs",
                           quest_type="daily", event_key="runs_scored",
                           target_count=100, reward_points=10,
                           reward_coins=0, reward_gems=0, is_active=True,
                           emoji="🏏", sort_order=0)
        self.session.add(self.quest)
        self.user = _make_user(self.session, next(_TG_IDS))

        player = Player(name=f"Bat {self.user.id}", rating=80,
                        category="Batsman", country="India",
                        bat_hand="Right", bowl_hand="Right",
                        bowl_style="Fast")
        self.session.add(player)
        self.session.flush()
        roster = UserRoster(user_id=self.user.id, player_id=player.id)
        self.session.add(roster)
        self.session.flush()
        self.roster_id = roster.id

        self.session.add(UserQuestProgress(
            user_id=self.user.id, quest_id=self.quest.id,
            period_key=daily_period_key(), progress=0, completed=False,
            claimed=False, assigned=True))
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def _state(self, **extra):
        state = {
            "inn1_bat_team_id": self.user.id,
            "inn1_bat_xi": [{"roster_id": self.roster_id}],
            "inn1_bat_stats": {self.roster_id: {"runs": 80, "balls": 40,
                                                "fours": 6, "sixes": 4,
                                                "out": True}},
        }
        state.update(extra)
        return state

    def _progress(self):
        from models import UserQuestProgress
        return (self.session.query(UserQuestProgress)
                .filter(UserQuestProgress.user_id == self.user.id,
                        UserQuestProgress.quest_id == self.quest.id)
                .first().progress)

    def test_a_fair_match_credits_the_runs(self):
        from services.quest_service import track_user_match_quests
        track_user_match_quests(self.session, self._state(), self.user,
                                True, False, self.user.id)
        self.session.flush()
        self.assertEqual(self._progress(), 80)

    def test_a_10_plus_overall_gap_credits_nothing(self):
        from services.quest_service import track_user_match_quests
        track_user_match_quests(self.session, self._state(stats_disabled=True),
                                self.user, True, False, self.user.id)
        self.session.flush()
        self.assertEqual(self._progress(), 0)

    def test_a_bot_match_credits_nothing(self):
        from services.quest_service import track_user_match_quests
        track_user_match_quests(self.session, self._state(), self.user,
                                True, True, self.user.id)
        self.session.flush()
        self.assertEqual(self._progress(), 0)


# ════════════════════════════════════════════════════════════════════
# Events the tracker fires
# ════════════════════════════════════════════════════════════════════

class MatchEventTests(unittest.TestCase):
    """Each new trigger has to move its own quest and nothing else's."""

    EVENTS = {
        "fours_hit": 3,
        "dot_balls": 8,
        "three_fer": 1,
        "five_fer": 1,
        "hattrick": 1,
        "defended_total": 1,
        "powerplay_runs": 48,
        "death_over_runs": 30,
    }

    def setUp(self):
        from database import get_session
        from models import Quest, UserQuestProgress, Player, UserRoster
        from services.quest_service import daily_period_key

        self.session = get_session()
        self.session.query(UserQuestProgress).delete()
        self.session.query(Quest).delete()

        self.user = _make_user(self.session, next(_TG_IDS))
        self.quests = {}
        for order, (event_key, target) in enumerate(self.EVENTS.items()):
            quest = Quest(name=f"Q {event_key}", description=event_key,
                          quest_type="daily", event_key=event_key,
                          target_count=target, reward_points=1,
                          reward_coins=0, reward_gems=0, is_active=True,
                          emoji="🎯", sort_order=order)
            self.session.add(quest)
            self.session.flush()
            self.quests[event_key] = quest
            self.session.add(UserQuestProgress(
                user_id=self.user.id, quest_id=quest.id,
                period_key=daily_period_key(), progress=0, completed=False,
                claimed=False, assigned=True))

        # One batter and one bowler, both owned by the user.
        self.rids = {}
        for role in ("bat", "bowl"):
            player = Player(name=f"{role} {self.user.id}", rating=80,
                            category="All-rounder", country="India",
                            bat_hand="Right", bowl_hand="Right",
                            bowl_style="Fast")
            self.session.add(player)
            self.session.flush()
            roster = UserRoster(user_id=self.user.id, player_id=player.id)
            self.session.add(roster)
            self.session.flush()
            self.rids[role] = roster.id
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def _progress(self, event_key):
        from models import UserQuestProgress
        return (self.session.query(UserQuestProgress)
                .filter(UserQuestProgress.user_id == self.user.id,
                        UserQuestProgress.quest_id == self.quests[event_key].id)
                .first().progress)

    def _fire(self):
        from services.quest_service import track_user_match_quests
        state = {
            # The user batted first (and won, so defended a total) and bowled
            # in the second innings.
            "inn1_bat_team_id": self.user.id,
            "inn1_bowl_team_id": 999,
            "bat_team_id": 999,
            "bowl_team_id": self.user.id,
            "inn1_bat_xi": [{"roster_id": self.rids["bat"]}],
            "inn1_bat_stats": {self.rids["bat"]: {"runs": 90, "balls": 50,
                                                  "fours": 3, "sixes": 5,
                                                  "out": True}},
            "bowl_xi": [{"roster_id": self.rids["bowl"]}],
            "bowl_stats": {self.rids["bowl"]: {"balls": 24, "runs": 20,
                                               "wickets": 5, "dots": 8,
                                               "maidens": 1,
                                               "hattrick": True}},
            # 20 overs: 48 in the powerplay, 30 in the last five.
            "inn1_over_runs": [8, 8, 8, 8, 8, 8] + [5] * 9 + [6] * 5,
        }
        track_user_match_quests(self.session, state, self.user, True, False,
                                self.user.id)
        self.session.flush()

    def test_every_new_trigger_fires(self):
        self._fire()
        for event_key, target in self.EVENTS.items():
            with self.subTest(event=event_key):
                self.assertEqual(self._progress(event_key), target,
                                 f"{event_key} did not reach its target")

    def test_a_short_innings_has_no_death_overs(self):
        # A 6-over game is all powerplay. Counting its last 5 overs as "death"
        # too would pay out both quests for the same runs.
        from services.quest_service import _phase_runs
        state = {"bat_team_id": 7, "over_runs": [10, 10, 10, 10, 10, 10]}
        powerplay, death = _phase_runs(state, 7)
        self.assertEqual(powerplay, 60)
        self.assertEqual(death, 0)

    def test_phase_runs_ignores_an_innings_the_user_did_not_bat(self):
        from services.quest_service import _phase_runs
        state = {"bat_team_id": 7, "over_runs": [10] * 20}
        self.assertEqual(_phase_runs(state, 8), (0, 0))


class HatTrickDetectionTests(unittest.TestCase):
    """Nothing ever wrote the flag the 'hattrick' quest reads."""

    def test_three_in_three_sets_the_flag(self):
        from services.match_engine import note_bowler_ball
        bws = {}
        for _ in range(3):
            note_bowler_ball(bws, bowler_wicket=True)
        self.assertTrue(bws.get("hattrick"))

    def test_a_gap_breaks_the_streak(self):
        from services.match_engine import note_bowler_ball
        bws = {}
        note_bowler_ball(bws, bowler_wicket=True)
        note_bowler_ball(bws, bowler_wicket=True)
        note_bowler_ball(bws, bowler_wicket=False)
        note_bowler_ball(bws, bowler_wicket=True)
        self.assertFalse(bws.get("hattrick"))
        self.assertEqual(bws["wkt_streak"], 1)

    def test_four_in_four_still_reads_as_a_hat_trick(self):
        from services.match_engine import note_bowler_ball
        bws = {}
        for _ in range(4):
            note_bowler_ball(bws, bowler_wicket=True)
        self.assertTrue(bws.get("hattrick"))

    def test_every_ball_loop_still_calls_it(self):
        # Three separate loops score balls. A 'hattrick' quest is only fair if
        # all three set the flag — miss one and whether the quest can be
        # cleared depends on which mode the match was played in.
        for path in ("handlers/match.py",
                     "services/match_webapp_service.py",
                     "services/cipl_match.py"):
            with self.subTest(loop=path):
                with open(os.path.join(os.path.dirname(__file__), "..",
                                       path)) as fh:
                    source = fh.read()
                self.assertIn("note_bowler_ball(", source)
                self.assertIn("wkts_before_ball", source)

    def test_the_mini_app_loop_flags_three_in_three(self):
        from services.match_webapp_service import _apply_outcome

        def player(rid, name):
            return {"roster_id": rid, "player_id": rid, "name": name,
                    "rating": 80, "category": "All-rounder", "bat_rating": 80,
                    "bowl_rating": 80, "bowl_style": "Fast",
                    "bowl_hand": "Right", "bat_hand": "Right"}

        bat = [player(i, f"A{i}") for i in range(1, 12)]
        bowler = player(100, "B1")
        state = {
            "innings": 1, "current_over": 1, "current_ball": 0,
            "total_runs": 0, "total_wickets": 0, "extras_total": 0,
            "wides": 0, "noballs": 0, "legbyes": 0, "byes": 0,
            "bat_stats": {}, "bowl_stats": {}, "batting_order": bat,
            "striker_idx": 0, "non_striker_idx": 1, "next_batsman_idx": 2,
            "bat_xi": bat, "bowl_xi": [bowler], "timeline": [],
            "over_runs": [], "partnership_runs": 0, "partnership_balls": 0,
            "partnership_history": [], "overs": 20, "wicket_limit": 10,
        }
        for _ in range(3):
            _apply_outcome(state,
                           {"type": "wicket", "runs": 0, "how": "Bowled"},
                           "Drive", "Yorker",
                           state["batting_order"][state["striker_idx"]],
                           bowler)
        self.assertTrue(state["bowl_stats"]["100"].get("hattrick"))


# ════════════════════════════════════════════════════════════════════
# The catalogue
# ════════════════════════════════════════════════════════════════════

class ManualQuestAssignmentTests(unittest.TestCase):
    """A quest nothing fires must not eat one of the three daily slots."""

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

    def _quest(self, name, event_key):
        from models import Quest
        quest = Quest(name=name, description=name, quest_type="daily",
                      event_key=event_key, target_count=1, reward_points=1,
                      reward_coins=0, reward_gems=0, is_active=True,
                      emoji="🎯", sort_order=0)
        self.session.add(quest)
        return quest

    def test_manual_quests_are_never_dealt_when_trackable_ones_exist(self):
        from services.quest_service import ensure_quests_assigned
        for index in range(6):
            self._quest(f"Manual {index}", "manual")
        for index in range(4):
            self._quest(f"Real {index}", "match_played")
        user = _make_user(self.session, next(_TG_IDS))
        self.session.commit()

        result = ensure_quests_assigned(self.session, user.id, "daily")
        self.session.commit()
        self.assertTrue(result["assigned"])
        for quest in result["assigned"]:
            self.assertNotEqual(quest.event_key, "manual")

    def test_manual_quests_still_fill_an_otherwise_empty_catalogue(self):
        # Better a manual quest than an empty quest list.
        from services.quest_service import ensure_quests_assigned
        for index in range(4):
            self._quest(f"Manual only {index}", "manual")
        user = _make_user(self.session, next(_TG_IDS))
        self.session.commit()

        result = ensure_quests_assigned(self.session, user.id, "daily")
        self.session.commit()
        self.assertTrue(result["assigned"])

    def test_a_pinned_manual_quest_is_still_assigned(self):
        # Pinning is the admin saying "I will drive this one by hand".
        from services.quest_service import ensure_quests_assigned
        pinned = self._quest("Watch an ad", "manual")
        pinned.always_assign = True
        self._quest("Real", "match_played")
        user = _make_user(self.session, next(_TG_IDS))
        self.session.commit()

        result = ensure_quests_assigned(self.session, user.id, "daily")
        self.session.commit()
        self.assertIn("Watch an ad", [q.name for q in result["assigned"]])


class CatalogueTests(unittest.TestCase):
    """Every seeded daily/monthly quest must be on a trigger that fires."""

    def _fired_event_keys(self):
        """Event keys the game actually emits, read off the source."""
        import re
        keys = set()
        for path in ("services/quest_service.py", "handlers/traits.py",
                     "handlers/claim.py", "handlers/daily.py",
                     "handlers/gspin.py", "handlers/packs.py",
                     "handlers/playermarket.py", "handlers/cmumysterybox.py",
                     "services/free_pack_service.py", "admin.py",
                     "handlers/super_over.py",
                     "services/match_webapp_service.py"):
            with open(os.path.join(os.path.dirname(__file__), "..", path)) as fh:
                source = fh.read()
            keys.update(re.findall(
                r'safe_track\([^,]+,\s*[^,]+,\s*"([a-z0-9_]+)"', source))
            # The tracker also fires from (event_key, amount) tuple tables.
            keys.update(re.findall(r'^\s*\("([a-z0-9_]+)",\s*\w+\),\s*$',
                                   source, re.M))
        return keys

    def test_no_seeded_quest_uses_an_event_nothing_fires(self):
        from seed_quests_v3 import DAILY_QUESTS, MONTHLY_QUESTS
        fired = self._fired_event_keys()
        for name, _desc, event_key, _t, _e, _tier in (DAILY_QUESTS
                                                      + MONTHLY_QUESTS):
            with self.subTest(quest=name):
                self.assertIn(event_key, fired,
                              f"'{name}' is on '{event_key}', which nothing fires")

    def test_no_seeded_quest_is_manual(self):
        from seed_quests_v3 import DAILY_QUESTS, MONTHLY_QUESTS
        for name, _desc, event_key, _t, _e, _tier in (DAILY_QUESTS
                                                      + MONTHLY_QUESTS):
            self.assertNotEqual(event_key, "manual", name)

    def test_every_seeded_trigger_is_offered_on_the_admin_form(self):
        # An admin editing a seeded quest must not have its trigger silently
        # rewritten to whatever sits at the top of the dropdown.
        import re
        from seed_quests_v3 import DAILY_QUESTS, MONTHLY_QUESTS
        with open(os.path.join(os.path.dirname(__file__), "..",
                               "admin.py")) as fh:
            block = fh.read().split("EVENT_KEYS = [")[1].split("\n]")[0]
        offered = set(re.findall(r'\("([a-z0-9_]+)",', block))
        for name, _desc, event_key, _t, _e, _tier in (DAILY_QUESTS
                                                      + MONTHLY_QUESTS):
            with self.subTest(quest=name):
                self.assertIn(event_key, offered)

    def test_names_are_unique_within_a_type(self):
        # seed() matches on (name, quest_type); a duplicate name would make the
        # seeder update the same row twice and quietly drop a quest.
        from seed_quests_v3 import DAILY_QUESTS, MONTHLY_QUESTS
        for catalogue in (DAILY_QUESTS, MONTHLY_QUESTS):
            names = [row[0] for row in catalogue]
            self.assertEqual(len(names), len(set(names)))

    def test_the_catalogue_is_bigger_than_a_period_deal(self):
        from seed_quests_v3 import DAILY_QUESTS, MONTHLY_QUESTS
        from services.quest_service import QUESTS_PER_USER
        self.assertGreater(len(DAILY_QUESTS), QUESTS_PER_USER["daily"] * 3)
        self.assertGreater(len(MONTHLY_QUESTS), QUESTS_PER_USER["monthly"] * 3)


class SeederTests(unittest.TestCase):
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

    def test_seeding_is_idempotent(self):
        from models import Quest
        from seed_quests_v3 import seed, DAILY_QUESTS, MONTHLY_QUESTS
        total = len(DAILY_QUESTS) + len(MONTHLY_QUESTS)

        first = seed(self.session)
        self.session.commit()
        self.assertEqual(first["created"], total)

        second = seed(self.session)
        self.session.commit()
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["updated"], total)
        self.assertEqual(second["retired"], 0)
        self.assertEqual(
            self.session.query(Quest).filter(Quest.is_active.is_(True)).count(),
            total)

    def test_legacy_quests_are_retired_not_deleted(self):
        from models import Quest
        from seed_quests_v3 import seed
        legacy = Quest(name="Yorker King", description="Bowl 4 yorkers",
                       quest_type="daily", event_key="manual", target_count=4,
                       reward_points=10, reward_coins=0, reward_gems=0,
                       is_active=True, emoji="🎯", sort_order=0)
        self.session.add(legacy)
        self.session.commit()

        result = seed(self.session)
        self.session.commit()
        self.assertEqual(result["retired"], 1)
        refreshed = self.session.query(Quest).get(legacy.id)
        self.assertIsNotNone(refreshed)
        self.assertFalse(refreshed.is_active)

    def test_pinned_and_career_quests_are_left_alone(self):
        from models import Quest
        from seed_quests_v3 import seed
        pinned = Quest(name="Watch 3 ads", description="ads",
                       quest_type="daily", event_key="ad_watched",
                       target_count=3, reward_points=5, reward_coins=0,
                       reward_gems=0, is_active=True, always_assign=True,
                       emoji="📺", sort_order=0)
        career = Quest(name="Career: Monthly Thing", description="career",
                       quest_type="monthly", event_key="career_runs_scored",
                       target_count=100, reward_points=5, reward_coins=0,
                       reward_gems=0, is_active=True, career_only=True,
                       emoji="🎖", sort_order=0)
        self.session.add_all([pinned, career])
        self.session.commit()

        seed(self.session)
        self.session.commit()
        self.assertTrue(self.session.query(Quest).get(pinned.id).is_active)
        self.assertTrue(self.session.query(Quest).get(career.id).is_active)

    def test_dry_run_writes_nothing(self):
        from models import Quest
        from seed_quests_v3 import seed, DAILY_QUESTS, MONTHLY_QUESTS
        result = seed(self.session, dry_run=True)
        self.session.commit()
        self.assertEqual(result["created"],
                         len(DAILY_QUESTS) + len(MONTHLY_QUESTS))
        self.assertEqual(self.session.query(Quest).count(), 0)

    def test_keep_legacy_retires_nothing(self):
        from models import Quest
        from seed_quests_v3 import seed
        legacy = Quest(name="Catch 5", description="catches",
                       quest_type="daily", event_key="manual", target_count=5,
                       reward_points=10, reward_coins=0, reward_gems=0,
                       is_active=True, emoji="🧤", sort_order=0)
        self.session.add(legacy)
        self.session.commit()

        result = seed(self.session, retire_legacy=False)
        self.session.commit()
        self.assertEqual(result["retired"], 0)
        self.assertTrue(self.session.query(Quest).get(legacy.id).is_active)


if __name__ == "__main__":
    unittest.main()

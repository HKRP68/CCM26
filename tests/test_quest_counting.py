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
from datetime import datetime

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

    def test_bot_matches_pass_the_gate(self):
        # /wpmbot and /vsbot complete quests. The gate lets them through; the
        # DAILY limit on them is consume_bot_quest_allowance's job, not this
        # predicate's — see BotMatchQuestAllowanceTests.
        from services.quest_service import match_counts_for_quests
        self.assertTrue(match_counts_for_quests({}, is_vsbot=True))
        self.assertTrue(match_counts_for_quests({"is_vsbot": True}))
        self.assertTrue(match_counts_for_quests({"is_bot_match": True}))

    def test_unranked_bot_practice_still_does_not_count(self):
        # /lpbot and /ciplbot mark their state unranked + stats_disabled
        # (handlers.cipl_play.mark_bot_match); they pay nothing and stay out.
        from services.quest_service import match_counts_for_quests
        self.assertFalse(match_counts_for_quests(
            {"is_bot_match": True, "stats_disabled": True, "unranked": True}))

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

    def test_a_bot_match_credits_the_runs_while_allowance_remains(self):
        # /wpmbot completes quests — that is the whole point of the allowance.
        from services.quest_service import track_user_match_quests
        track_user_match_quests(self.session, self._state(), self.user,
                                True, True, self.user.id)
        self.session.flush()
        self.assertEqual(self._progress(), 80)

    def test_a_bot_match_credits_nothing_once_the_daily_cap_is_spent(self):
        from services import quest_service
        from services.quest_service import track_user_match_quests
        self.user.bot_quest_matches_date = datetime.utcnow().strftime("%Y-%m-%d")
        self.user.bot_quest_matches_today = quest_service.BOT_QUEST_MATCH_DAILY_CAP
        self.session.flush()
        track_user_match_quests(self.session, self._state(), self.user,
                                True, True, self.user.id)
        self.session.flush()
        self.assertEqual(self._progress(), 0)

    def test_a_junk_over_count_does_not_abandon_the_rest_of_the_tracking(self):
        # The match-length keys are worked out outside safe_track and before
        # the batting/bowling totals are credited, so an exception escaping
        # from a bad 'overs' would silently cost this user their runs.
        from services.quest_service import track_user_match_quests
        track_user_match_quests(self.session, self._state(overs=float("inf")),
                                self.user, True, False, self.user.id)
        self.session.flush()
        self.assertEqual(self._progress(), 80)

    def test_the_cap_only_applies_to_bot_matches(self):
        # A spent bot allowance must not block a real PvP match.
        from services import quest_service
        from services.quest_service import track_user_match_quests
        self.user.bot_quest_matches_date = datetime.utcnow().strftime("%Y-%m-%d")
        self.user.bot_quest_matches_today = quest_service.BOT_QUEST_MATCH_DAILY_CAP
        self.session.flush()
        track_user_match_quests(self.session, self._state(), self.user,
                                True, False, self.user.id)
        self.session.flush()
        self.assertEqual(self._progress(), 80)


class BotMatchQuestAllowanceTests(unittest.TestCase):
    """The per-day limit on how many AI matches may feed quests."""

    def setUp(self):
        from database import get_session
        self.session = get_session()
        self.user = _make_user(self.session, next(_TG_IDS))
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def test_the_cap_is_spent_then_refused(self):
        from services import quest_service
        from services.quest_service import consume_bot_quest_allowance
        cap = quest_service.BOT_QUEST_MATCH_DAILY_CAP
        for i in range(cap):
            self.assertTrue(consume_bot_quest_allowance(self.session, self.user),
                            f"bot match {i + 1} of {cap} should have been allowed")
        self.assertFalse(consume_bot_quest_allowance(self.session, self.user))
        self.assertEqual(self.user.bot_quest_matches_today, cap)

    def test_a_new_day_resets_the_allowance(self):
        from services.quest_service import consume_bot_quest_allowance
        consume_bot_quest_allowance(self.session, self.user)
        self.user.bot_quest_matches_date = "2000-01-01"
        self.user.bot_quest_matches_today = 999
        self.assertTrue(consume_bot_quest_allowance(self.session, self.user))
        self.assertEqual(self.user.bot_quest_matches_today, 1)
        self.assertEqual(self.user.bot_quest_matches_date,
                         datetime.utcnow().strftime("%Y-%m-%d"))


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

    @staticmethod
    def _mini_app_state():
        def player(rid, name):
            return {"roster_id": rid, "player_id": rid, "name": name,
                    "rating": 80, "category": "All-rounder", "bat_rating": 80,
                    "bowl_rating": 80, "bowl_style": "Fast",
                    "bowl_hand": "Right", "bat_hand": "Right"}

        bat = [player(i, f"A{i}") for i in range(1, 12)]
        return {
            "innings": 1, "current_over": 1, "current_ball": 0,
            "total_runs": 0, "total_wickets": 0, "extras_total": 0,
            "wides": 0, "noballs": 0, "legbyes": 0, "byes": 0,
            "bat_stats": {}, "bowl_stats": {}, "batting_order": bat,
            "striker_idx": 0, "non_striker_idx": 1, "next_batsman_idx": 2,
            "bat_xi": bat, "bowl_xi": [player(100, "B1")], "timeline": [],
            "over_runs": [], "partnership_runs": 0, "partnership_balls": 0,
            "partnership_history": [], "overs": 20, "wicket_limit": 10,
        }

    def _bowl_three(self, how):
        from services.match_webapp_service import _apply_outcome
        state = self._mini_app_state()
        bowler = state["bowl_xi"][0]
        for _ in range(3):
            _apply_outcome(state, {"type": "wicket", "runs": 0, "how": how},
                           "Drive", "Yorker",
                           state["batting_order"][state["striker_idx"]],
                           bowler)
        return state

    def test_the_mini_app_loop_flags_three_in_three(self):
        state = self._bowl_three("Bowled")
        self.assertTrue(state["bowl_stats"]["100"].get("hattrick"))

    def test_three_run_outs_are_not_a_hat_trick(self):
        # A run-out is the fielding side's wicket. Three in a row must leave
        # the bowler's figures and their hat-trick streak untouched, while the
        # team still loses three wickets.
        state = self._bowl_three("Run Out")
        row = state["bowl_stats"]["100"]
        self.assertFalse(row.get("hattrick"))
        self.assertEqual(row.get("wkt_streak", 0), 0)
        self.assertEqual(row["wickets"], 0)
        self.assertEqual(state["total_wickets"], 3)

    def test_a_run_out_breaks_a_bowlers_streak(self):
        from services.match_webapp_service import _apply_outcome
        state = self._mini_app_state()
        bowler = state["bowl_xi"][0]
        for how in ("Bowled", "Run Out", "Bowled", "Bowled"):
            _apply_outcome(state, {"type": "wicket", "runs": 0, "how": how},
                           "Drive", "Yorker",
                           state["batting_order"][state["striker_idx"]],
                           bowler)
        row = state["bowl_stats"]["100"]
        self.assertFalse(row.get("hattrick"))
        self.assertEqual(row["wickets"], 3)
        self.assertEqual(state["total_wickets"], 4)

    def test_run_outs_are_never_the_bowlers_wicket(self):
        from services.match_engine import is_bowler_wicket
        for how in ("Run Out", "run out", " RUN OUT "):
            with self.subTest(how=how):
                self.assertFalse(is_bowler_wicket(how))
        for how in ("Bowled", "Caught", "LBW", "Stumped", "Caught & Bowled"):
            with self.subTest(how=how):
                self.assertTrue(is_bowler_wicket(how))

    def test_every_ball_loop_excludes_run_outs_from_bowler_figures(self):
        # All four loops score wickets. If one credits a run-out to the bowler,
        # the same dismissal inflates figures in one mode but not another.
        for path in ("handlers/match.py",
                     "services/match_webapp_service.py",
                     "services/cipl_match.py",
                     "services/sim_match.py"):
            with self.subTest(loop=path):
                with open(os.path.join(os.path.dirname(__file__), "..",
                                       path)) as fh:
                    source = fh.read()
                self.assertTrue(
                    "is_bowler_wicket(" in source
                    or 'wtype != "Run Out"' in source,
                    f"{path} credits run-outs to the bowler")


def _loop_source(path):
    with open(os.path.join(os.path.dirname(__file__), "..", path)) as fh:
        return fh.read()


class DotBallTests(unittest.TestCase):
    """'Bowl 30 dot balls today' only counts where the loop records dots.

    ``track_user_match_quests`` reads ``dots`` off each bowler's stat row. A
    ball loop that never writes it leaves the dot-ball quests on 0 however many
    dots were actually bowled — and ``career_dot_balls`` with them, which also
    puts the career weekly streak (every assigned career quest cleared) out of
    reach for anyone whose matches run through that mode.
    """

    @staticmethod
    def _mini_app_state():
        return HatTrickDetectionTests._mini_app_state()

    def _apply(self, state, outcome):
        from services.match_webapp_service import _apply_outcome
        _apply_outcome(state, outcome, "Drive", "Yorker",
                       state["batting_order"][state["striker_idx"]],
                       state["bowl_xi"][0])

    def test_the_mini_app_loop_credits_a_dot_to_bowler_and_batter(self):
        state = self._mini_app_state()
        striker = state["batting_order"][state["striker_idx"]]
        self._apply(state, {"type": "run", "runs": 0})
        self.assertEqual(state["bowl_stats"]["100"]["dots"], 1)
        self.assertEqual(state["bat_stats"][str(striker["roster_id"])]["dots"], 1)

    def test_runs_off_the_bat_are_not_a_dot(self):
        state = self._mini_app_state()
        self._apply(state, {"type": "run", "runs": 2})
        self.assertEqual(state["bowl_stats"]["100"].get("dots", 0), 0)

    def test_a_wicket_off_a_no_run_ball_is_a_dot(self):
        state = self._mini_app_state()
        striker = state["batting_order"][state["striker_idx"]]
        self._apply(state, {"type": "wicket", "runs": 0, "how": "Bowled"})
        self.assertEqual(state["bowl_stats"]["100"]["dots"], 1)
        self.assertEqual(state["bat_stats"][str(striker["roster_id"])]["dots"], 1)

    def test_the_challenge_league_loop_records_dots_over_a_full_innings(self):
        # /letsplay and Challenge League score their balls in cipl_match, which
        # kept every other figure the quest tracker reads but not this one.
        import random
        from services import cipl_match
        from services.bot_xi_builder import (
            LPBOT_SHAPE, _assign_lpbot_traits, _lpbot_player_dict)

        class _P:
            _n = 0

            def __init__(self, category, rating):
                _P._n += 1
                self.id = _P._n
                self.name = f"{category[:3]}{_P._n}"
                self.rating = rating
                self.category = category
                self.bat_rating = rating if category != "Bowler" else rating - 25
                self.bowl_rating = (rating if category in ("Bowler", "All-rounder")
                                    else rating - 40)
                self.bowl_style = "Fast"
                self.bowl_hand = "Right"
                self.bat_hand = "Right"

        def _xi(rating, offset):
            rows = [_P(c, rating - i) for c, n in LPBOT_SHAPE for i in range(n)]
            xi = [_lpbot_player_dict(p) for p in rows]
            _assign_lpbot_traits(xi)
            for i, p in enumerate(xi):
                p["roster_id"] = offset + i
            return xi

        random.seed(7)
        state = cipl_match.build_cipl_state(
            match_id=1, overs=20, bat_user_id=1, bowl_user_id=2,
            bat_user_tg=1, bowl_user_tg=2, bat_xi=_xi(80, 0),
            bowl_xi=_xi(78, 5000), bat_team_name="A", bowl_team_name="B",
            chat_id=1, pitch_type="Hard")
        while not cipl_match.is_innings_over(state):
            state["current_bowler"] = cipl_match.eligible_bowlers(state)[0]
            state["batting_approach"] = "balanced"
            state["bowling_approach"] = "balanced"
            cipl_match.simulate_over(state)

        bowler_dots = sum(v.get("dots", 0) for v in state["bowl_stats"].values())
        batter_dots = sum(v.get("dots", 0) for v in state["bat_stats"].values())
        legal_balls = sum(v.get("balls", 0) for v in state["bowl_stats"].values())
        self.assertGreater(bowler_dots, 0, "a 20-over innings with no dot balls")
        self.assertEqual(bowler_dots, batter_dots,
                         "a dot is one ball — both cards must agree")
        self.assertLessEqual(bowler_dots, legal_balls)

    def test_every_ball_loop_records_dots(self):
        # Three loops score balls. A dot-ball quest is only fair if all three
        # count them — miss one and whether the quest can be cleared depends on
        # which mode the match happened to be played in.
        for path in ("handlers/match.py",
                     "services/match_webapp_service.py",
                     "services/cipl_match.py"):
            with self.subTest(loop=path):
                source = _loop_source(path)
                self.assertIn('bws["dots"]', source,
                              f"{path} never credits a bowler with a dot")
                self.assertIn('bs["dots"]', source,
                              f"{path} never credits a batter with a dot")


class InningsBreakArchiveTests(unittest.TestCase):
    """The powerplay / death-over quests read the per-over run list.

    ``_phase_runs`` scores innings 1 off ``inn1_over_runs`` and innings 2 off
    the live ``over_runs``. A break that archives neither leaves the side
    batting first with nothing to score at all, and hands the side batting
    second the *opponent's* opening overs — one list holding both innings.
    """

    def test_each_side_scores_its_own_innings(self):
        from services.quest_service import _phase_runs
        state = {
            "inn1_bat_team_id": 1, "bat_team_id": 2,
            # 60 in the powerplay, 10 at the death.
            "inn1_over_runs": [10] * 6 + [1] * 9 + [2] * 5,
            # 18 in the powerplay, 45 at the death.
            "over_runs": [3] * 6 + [1] * 9 + [9] * 5,
        }
        self.assertEqual(_phase_runs(state, 1), (60, 10))
        self.assertEqual(_phase_runs(state, 2), (18, 45))

    def test_every_innings_break_archives_and_resets_the_over_list(self):
        for path in ("handlers/match.py",          # in-chat /wpm, /playmatch
                     "services/match_engine.py",   # Mini App
                     "services/cipl_match.py"):    # /letsplay, Challenge League
            with self.subTest(mode=path):
                source = _loop_source(path)
                self.assertIn('"inn1_over_runs"', source,
                              f"{path} never archives innings 1's over list")
                self.assertRegex(
                    source, r'\["over_runs"\]\s*=\s*\[\]',
                    f"{path} never clears the over list for the chase")


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
        # Pitch keys are the one family built at runtime rather than written as
        # a literal — track_user_match_quests fires pitch_event_key(pitch_type)
        # for whatever surface the match was on. Take them from the same
        # PITCH_TYPES-derived map the tracker uses, so a pitch added to
        # match_constants shows up here without anyone editing this test, and a
        # quest on a surface that does not exist still fails.
        from services.quest_service import PITCH_EVENT_KEYS, OVERS_EVENT_KEYS
        keys.update(PITCH_EVENT_KEYS.values())
        # Match-length keys are built the same way, from
        # MATCH_LENGTH_THRESHOLDS, so a threshold added there needs no edit
        # here — and a quest on a threshold that does not exist still fails.
        keys.update(OVERS_EVENT_KEYS.values())
        return keys

    def test_pitch_event_keys_match_the_playable_surfaces(self):
        from services.match_constants import PITCH_TYPES
        from services.quest_service import PITCH_EVENT_KEYS, pitch_event_key
        self.assertEqual(set(PITCH_EVENT_KEYS), set(PITCH_TYPES))
        self.assertEqual(pitch_event_key("Green"), "pitch_green")
        # Whatever case/padding the state carries resolves to the same key.
        self.assertEqual(pitch_event_key("  green "), "pitch_green")
        self.assertIsNone(pitch_event_key(None))
        self.assertIsNone(pitch_event_key(""))

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


# ════════════════════════════════════════════════════════════════════
# Match-length quests
# ════════════════════════════════════════════════════════════════════

class MatchLengthEventTests(unittest.TestCase):
    """Which length thresholds a match's over count clears."""

    def test_a_20_over_match_clears_every_threshold(self):
        # The ladder is the point: picking the longest format must not leave a
        # "play 2 matches of 10+ overs" quest sitting at 0.
        from services.quest_service import match_length_event_keys
        self.assertEqual(match_length_event_keys(20),
                         ["overs_10", "overs_15", "overs_20"])

    def test_a_match_clears_only_the_thresholds_it_reaches(self):
        from services.quest_service import match_length_event_keys
        self.assertEqual(match_length_event_keys(15), ["overs_10", "overs_15"])
        self.assertEqual(match_length_event_keys(10), ["overs_10"])
        self.assertEqual(match_length_event_keys(9), [])
        self.assertEqual(match_length_event_keys(1), [])

    def test_a_length_written_as_a_string_or_float_still_counts(self):
        # State round-trips through JSON, and the modes disagree about type.
        from services.quest_service import match_length_event_keys
        self.assertEqual(match_length_event_keys("20"),
                         ["overs_10", "overs_15", "overs_20"])
        self.assertEqual(match_length_event_keys(15.0),
                         ["overs_10", "overs_15"])

    def test_a_missing_or_unreadable_length_clears_nothing(self):
        # Better a length quest that does not move than one handed out free to
        # every match whose state forgot to record its own format.
        from services.quest_service import match_length_event_keys
        self.assertEqual(match_length_event_keys(None), [])
        self.assertEqual(match_length_event_keys(""), [])
        self.assertEqual(match_length_event_keys("T20"), [])

    def test_an_infinite_length_clears_nothing_rather_than_raising(self):
        # int(float(x)) raises OverflowError, not ValueError, for an infinite
        # length. This call sits outside safe_track, so an escape would abandon
        # the rest of the match's quest tracking over a junk field.
        from services.quest_service import match_length_event_keys
        self.assertEqual(match_length_event_keys(float("inf")), [])
        self.assertEqual(match_length_event_keys("1e309"), [])
        self.assertEqual(match_length_event_keys(float("nan")), [])

    def test_the_hundred_does_not_clear_the_20_over_threshold(self):
        # Challenge League stores a 100-ball Hundred innings as 20 sets of
        # five, so overs == 20 exactly as a 120-ball T20 does. By length it is
        # a 16.4-over game: it clears 10 and 15, and must not clear 20.
        from services.quest_service import match_length_event_keys
        self.assertEqual(match_length_event_keys(20, 5),
                         ["overs_10", "overs_15"])

    def test_balls_per_unit_comes_from_the_format_on_the_state(self):
        # Read through the real cipl_match helper rather than a copy, so a
        # format whose ball count changes there is reflected here.
        from services.quest_service import match_balls_per_unit
        self.assertEqual(match_balls_per_unit({"ball_format": "The100"}), 5)
        self.assertEqual(match_balls_per_unit({"ball_format": "T20"}), 6)
        self.assertEqual(match_balls_per_unit({}), 6)
        self.assertEqual(match_balls_per_unit(None), 6)
        self.assertEqual(match_balls_per_unit({"ball_format": "nonsense"}), 6)

    def test_a_hundred_match_is_a_real_hundred_balls(self):
        # Guards the premise of the test above: if The Hundred ever stopped
        # being 20x5, the threshold maths here would need revisiting.
        from services.cipl_match import total_balls
        self.assertEqual(total_balls({"overs": 20, "ball_format": "The100"}),
                         100)
        self.assertEqual(total_balls({"overs": 20, "ball_format": "T20"}), 120)

    def test_the_keys_line_up_with_the_thresholds(self):
        from services.quest_service import (MATCH_LENGTH_THRESHOLDS,
                                            OVERS_EVENT_KEYS)
        self.assertEqual(set(OVERS_EVENT_KEYS), set(MATCH_LENGTH_THRESHOLDS))
        self.assertEqual(OVERS_EVENT_KEYS[20], "overs_20")


class MatchLengthTrackingTests(unittest.TestCase):
    """A finished match moves the length quests the player is holding."""

    def setUp(self):
        from database import get_session
        from models import Quest, UserQuestProgress
        from services.quest_service import daily_period_key

        self.session = get_session()
        self.session.query(UserQuestProgress).delete()
        self.session.query(Quest).delete()

        self.quests = {}
        for key in ("overs_10", "overs_15", "overs_20"):
            quest = Quest(name=f"Play {key}", description=key,
                          quest_type="daily", event_key=key, target_count=4,
                          reward_points=10, reward_coins=0, reward_gems=0,
                          is_active=True, emoji="🕛", sort_order=0)
            self.session.add(quest)
            self.quests[key] = quest
        self.user = _make_user(self.session, next(_TG_IDS))
        self.session.flush()

        for quest in self.quests.values():
            self.session.add(UserQuestProgress(
                user_id=self.user.id, quest_id=quest.id,
                period_key=daily_period_key(), progress=0, completed=False,
                claimed=False, assigned=True))
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def _progress(self, key):
        from models import UserQuestProgress
        return (self.session.query(UserQuestProgress)
                .filter(UserQuestProgress.user_id == self.user.id,
                        UserQuestProgress.quest_id == self.quests[key].id)
                .first().progress)

    def _play(self, overs):
        from services.quest_service import track_user_match_quests
        track_user_match_quests(self.session, {"overs": overs}, self.user,
                                True, False, self.user.id)
        self.session.flush()

    def test_a_20_over_match_moves_all_three_bars(self):
        self._play(20)
        self.assertEqual(self._progress("overs_10"), 1)
        self.assertEqual(self._progress("overs_15"), 1)
        self.assertEqual(self._progress("overs_20"), 1)

    def test_a_15_over_match_leaves_the_20_over_bar_alone(self):
        self._play(15)
        self.assertEqual(self._progress("overs_10"), 1)
        self.assertEqual(self._progress("overs_15"), 1)
        self.assertEqual(self._progress("overs_20"), 0)

    def test_a_short_match_moves_nothing(self):
        self._play(5)
        self.assertEqual(self._progress("overs_10"), 0)

    def test_a_hundred_match_moves_the_10_and_15_bars_only(self):
        # End-to-end through the tracker: a Challenge League Hundred game
        # carries overs == 20 but is only 100 balls.
        from services.quest_service import track_user_match_quests
        track_user_match_quests(self.session,
                                {"overs": 20, "ball_format": "The100"},
                                self.user, True, False, self.user.id)
        self.session.flush()
        self.assertEqual(self._progress("overs_10"), 1)
        self.assertEqual(self._progress("overs_15"), 1)
        self.assertEqual(self._progress("overs_20"), 0)

    def test_a_voided_match_moves_nothing(self):
        from services.quest_service import track_user_match_quests
        track_user_match_quests(self.session,
                                {"overs": 20, "stats_disabled": True},
                                self.user, True, False, self.user.id)
        self.session.flush()
        self.assertEqual(self._progress("overs_20"), 0)


# ════════════════════════════════════════════════════════════════════
# The guaranteed daily families
# ════════════════════════════════════════════════════════════════════

class DailyGuaranteedDealTests(unittest.TestCase):
    """Every daily card carries one pitch quest and one match-length quest."""

    def setUp(self):
        from database import get_session
        from models import Quest, UserQuestProgress
        self.session = get_session()
        self.session.query(UserQuestProgress).delete()
        self.session.query(Quest).delete()

        # A general pool big enough that the random three could never happen to
        # contain a pitch or length quest by luck.
        for i in range(12):
            self.session.add(Quest(
                name=f"General {i}", description="general", quest_type="daily",
                event_key="match_played", target_count=1, reward_points=10,
                reward_coins=0, reward_gems=0, is_active=True, emoji="🏏",
                sort_order=i))
        for surface in ("green", "flat", "dusty"):
            self.session.add(Quest(
                name=f"Pitch {surface}", description=surface,
                quest_type="daily", event_key=f"pitch_{surface}",
                target_count=3, reward_points=20, reward_coins=0,
                reward_gems=0, is_active=True, emoji="🟢", sort_order=0))
        for overs in (10, 15, 20):
            self.session.add(Quest(
                name=f"Overs {overs}", description=str(overs),
                quest_type="daily", event_key=f"overs_{overs}",
                target_count=2, reward_points=20, reward_coins=0,
                reward_gems=0, is_active=True, emoji="🕛", sort_order=0))
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def _deal(self, user=None):
        """Deal today's dailies and return the assigned quests."""
        from services.quest_service import ensure_quests_assigned
        user = user or _make_user(self.session, next(_TG_IDS))
        result = ensure_quests_assigned(self.session, user.id, "daily")
        self.session.commit()
        return user, result["assigned"]

    def _families(self, quests):
        keys = [q.event_key for q in quests]
        return (sum(1 for k in keys if k.startswith("pitch_")),
                sum(1 for k in keys if k.startswith("overs_")))

    def _assigned_quests(self, user):
        """Everything the user actually holds for today, not just new rows."""
        from models import Quest, UserQuestProgress
        from services.quest_service import daily_period_key
        return (self.session.query(Quest)
                .join(UserQuestProgress, Quest.id == UserQuestProgress.quest_id)
                .filter(UserQuestProgress.user_id == user.id,
                        UserQuestProgress.period_key == daily_period_key(),
                        UserQuestProgress.assigned == True)  # noqa: E712
                .all())

    def test_a_daily_deal_contains_one_of_each_family(self):
        for _ in range(8):
            _user, dealt = self._deal()
            self.assertEqual(self._families(dealt), (1, 1))

    def test_the_guaranteed_slots_do_not_eat_the_random_three(self):
        from services.quest_service import QUESTS_PER_USER
        _user, dealt = self._deal()
        general = [q for q in dealt if q.event_key == "match_played"]
        self.assertEqual(len(general), QUESTS_PER_USER["daily"])
        self.assertEqual(len(dealt), QUESTS_PER_USER["daily"] + 2)

    def test_dealing_twice_in_a_day_adds_nothing(self):
        from services.quest_service import ensure_quests_assigned
        user, first = self._deal()
        again = ensure_quests_assigned(self.session, user.id, "daily")
        self.session.commit()
        self.assertEqual(again["assigned"], [])
        self.assertEqual(self._families(first), (1, 1))

    def test_a_card_dealt_before_the_families_existed_is_topped_up(self):
        # Shipping mid-day must not leave everybody waiting until tomorrow.
        from models import Quest
        from services.quest_service import ensure_quests_assigned
        live = (self.session.query(Quest)
                .filter(Quest.event_key.like("pitch_%")
                        | Quest.event_key.like("overs_%")).all())
        for quest in live:
            quest.is_active = False
        self.session.commit()

        user, dealt = self._deal()
        self.assertEqual(self._families(dealt), (0, 0))

        for quest in live:
            quest.is_active = True
        self.session.commit()

        topped = ensure_quests_assigned(self.session, user.id, "daily")
        self.session.commit()
        self.assertEqual(self._families(topped["assigned"]), (1, 1))

    def test_a_pinned_quest_fills_its_family_rather_than_doubling_it(self):
        from models import Quest
        pin = (self.session.query(Quest)
               .filter(Quest.event_key == "pitch_green").first())
        pin.always_assign = True
        self.session.commit()

        _user, dealt = self._deal()
        self.assertEqual(self._families(dealt), (1, 1))
        self.assertIn(pin.id, [q.id for q in dealt])

    def test_a_pin_added_mid_day_arrives_on_top_and_retracts_nothing(self):
        # An admin pinning a pitch quest after the user opened their card
        # leaves them holding two for the day: the morning's draw plus the pin.
        # Deliberate — see the comment at the top-up call site. What must NOT
        # happen is the morning's quest being unassigned (it may hold progress)
        # or a *third* being dealt on top.
        from models import Quest
        from services.quest_service import ensure_quests_assigned

        user, dealt = self._deal()
        morning = [q for q in dealt if q.event_key.startswith("pitch_")][0]

        pin = (self.session.query(Quest)
               .filter(Quest.event_key.like("pitch_%"),
                       Quest.id != morning.id).first())
        pin.always_assign = True
        self.session.commit()

        added = ensure_quests_assigned(self.session, user.id, "daily")
        self.session.commit()
        self.assertEqual([q.id for q in added["assigned"]], [pin.id])

        held = self._assigned_quests(user)
        self.assertEqual(self._families(held), (2, 1))
        self.assertIn(morning.id, [q.id for q in held])

    def test_an_empty_family_costs_nobody_their_random_three(self):
        from models import Quest
        from services.quest_service import QUESTS_PER_USER
        for quest in (self.session.query(Quest)
                      .filter(Quest.event_key.like("overs_%")).all()):
            quest.is_active = False
        self.session.commit()

        _user, dealt = self._deal()
        self.assertEqual(self._families(dealt), (1, 0))
        self.assertEqual(len([q for q in dealt if q.event_key == "match_played"]),
                         QUESTS_PER_USER["daily"])

    def test_yesterdays_pick_goes_to_the_back_of_the_queue(self):
        from datetime import timedelta
        from models import UserQuestProgress
        from services.quest_service import (daily_period_key,
                                            _deal_daily_guaranteed)
        from models import Quest

        user = _make_user(self.session, next(_TG_IDS))
        yesterday = daily_period_key(datetime.utcnow() - timedelta(days=1))
        stale = (self.session.query(Quest)
                 .filter(Quest.event_key == "pitch_green").first())
        self.session.add(UserQuestProgress(
            user_id=user.id, quest_id=stale.id, period_key=yesterday,
            progress=0, completed=False, claimed=False, assigned=True))
        self.session.commit()

        pool = (self.session.query(Quest)
                .filter(Quest.event_key.like("pitch_%")).all()
                + self.session.query(Quest)
                .filter(Quest.event_key.like("overs_%")).all())
        for _ in range(10):
            picked = _deal_daily_guaranteed(self.session, user.id, pool,
                                            datetime.utcnow())
            pitch = [q for q in picked if q.event_key.startswith("pitch_")]
            self.assertNotEqual(pitch[0].id, stale.id)

    def test_monthly_deals_are_untouched(self):
        from models import Quest
        from services.quest_service import (ensure_quests_assigned,
                                            QUESTS_PER_USER)
        for i in range(10):
            self.session.add(Quest(
                name=f"Monthly {i}", description="m", quest_type="monthly",
                event_key="match_played", target_count=20, reward_points=50,
                reward_coins=0, reward_gems=0, is_active=True, emoji="🏟",
                sort_order=i))
        self.session.commit()

        user = _make_user(self.session, next(_TG_IDS))
        result = ensure_quests_assigned(self.session, user.id, "monthly")
        self.session.commit()
        self.assertEqual(len(result["assigned"]), QUESTS_PER_USER["monthly"])


class DailyGuaranteedCatalogueTests(unittest.TestCase):
    """The seeded catalogue has to be able to fill the guaranteed slots."""

    def test_every_guaranteed_family_has_something_to_deal(self):
        from seed_quests_v3 import DAILY_QUESTS
        from services.quest_service import DAILY_GUARANTEED_BUCKETS
        seeded = {row[2] for row in DAILY_QUESTS}
        for family, keys, slots in DAILY_GUARANTEED_BUCKETS:
            with self.subTest(family=family):
                self.assertGreaterEqual(len(seeded & set(keys)), slots)

    def test_the_families_rotate_rather_than_repeat(self):
        # A family with one quest in it would deal the same quest every day,
        # which is the thing the freshness pass exists to avoid.
        from seed_quests_v3 import DAILY_QUESTS
        from services.quest_service import DAILY_GUARANTEED_BUCKETS
        seeded = {row[2] for row in DAILY_QUESTS}
        for family, keys, _slots in DAILY_GUARANTEED_BUCKETS:
            with self.subTest(family=family):
                self.assertGreater(len(seeded & set(keys)), 1)

    def test_no_length_quest_asks_for_a_format_the_lobby_cannot_make(self):
        # A threshold above the longest lobby /wpm will open is a quest nobody
        # can finish. Read off the source rather than imported — handlers.match
        # drags in the whole Telegram stack for one integer.
        import re
        from services.quest_service import MATCH_LENGTH_THRESHOLDS
        with open(os.path.join(os.path.dirname(__file__), "..",
                               "handlers", "match.py")) as fh:
            found = re.search(r"^WPM_MAX_OVERS = (\d+)", fh.read(), re.M)
        self.assertIsNotNone(found, "WPM_MAX_OVERS moved or was renamed")
        for threshold in MATCH_LENGTH_THRESHOLDS:
            self.assertLessEqual(threshold, int(found.group(1)))

    def test_the_seeded_length_targets_are_reachable(self):
        # Every length quest is a "play N matches" count, so its target has to
        # be a number of matches somebody could get through in a day.
        from seed_quests_v3 import DAILY_QUESTS
        lengths = [row for row in DAILY_QUESTS if row[2].startswith("overs_")]
        self.assertTrue(lengths)
        for name, _desc, _key, target, _emoji, _tier in lengths:
            with self.subTest(quest=name):
                self.assertLessEqual(target, 4)


# ════════════════════════════════════════════════════════════════════
# The panel
# ════════════════════════════════════════════════════════════════════

class ProgressBarTests(unittest.TestCase):
    """The /myquest bar has to show that a quest has started counting.

    Truncating to whole tenths drew an empty bar for anything under 10%, so the
    first few dots of a 30-dot quest — or the first 50 runs of a 600-run one —
    looked exactly like a quest whose trigger never fires at all. That is the
    other half of "no progress bar": a bar that only ever moves late.
    """

    @staticmethod
    def _bar(percent):
        from handlers.quests import _progress_bar
        return _progress_bar(percent)

    def test_no_progress_leaves_the_bar_empty(self):
        self.assertEqual(self._bar(0), "░" * 10)

    def test_any_progress_at_all_lights_a_segment(self):
        for percent in (1, 3, 9):
            with self.subTest(percent=percent):
                self.assertTrue(self._bar(percent).startswith("█"))

    def test_a_part_filled_segment_still_rounds_down(self):
        # Only a finished quest shows a solid bar — 99% must not read as done.
        self.assertEqual(self._bar(99).count("█"), 9)
        self.assertEqual(self._bar(50).count("█"), 5)

    def test_a_finished_quest_fills_the_bar(self):
        self.assertEqual(self._bar(100), "█" * 10)

    def test_the_bar_is_always_ten_segments(self):
        for percent in list(range(0, 101, 7)) + [-5, 250, None, "x"]:
            with self.subTest(percent=percent):
                self.assertEqual(len(self._bar(percent)), 10)


if __name__ == "__main__":
    unittest.main()

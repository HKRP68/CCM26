"""The tournament rating rule, and /statstour's all-seasons total.

  • **"At least N players rated X or lower" in every XI.** The XI validator
    refuses an XI short of it, and the bot's own XI is built to meet it
    without breaking the keeper, bowling or overseas rules.
  • **Total stats** sum a player across every tournament of the same league —
    and nothing from another league leaks in.
"""

import itertools
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap  # noqa: E402  (sibling helper; see its docstring)

from services import bot_xi_builder as bx  # noqa: E402
from services import xi_rules  # noqa: E402

_SEQ = itertools.count(1)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "services.tournament_service")


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


class FakeChallengePlayer:
    def __init__(self, pid, category, rating, overseas=False):
        self.id = pid
        self.name = f"P{pid}"
        self.is_overseas = overseas
        self.details_json = json.dumps({
            "category": category, "rating": rating,
            "bat_rating": rating, "bowl_rating": rating - 20})


def _squad(size=18):
    shape = (["Wicket Keeper"] * 2 + ["Batsman"] * 6
             + ["All-rounder"] * 4 + ["Bowler"] * 6)
    return [FakeChallengePlayer(i + 1, shape[i % len(shape)], 90 - i)
            for i in range(size)]


RULE = [{"max_rating": 75, "min_players": 3}]


class XiRuleTests(unittest.TestCase):

    def test_an_xi_short_of_the_rule_is_refused(self):
        xi = bx.build_challenge_bot_xi(_squad())
        ok, _ = xi_rules.validate_challenge_xi(xi, 0, 11)
        self.assertTrue(ok, "the best XI is legal without the rule")
        ok, error = xi_rules.validate_challenge_xi(xi, 0, 11, RULE)
        self.assertFalse(ok)
        self.assertIn("≤75", error)

    def test_the_bot_xi_meets_the_rule_and_every_other_one(self):
        xi = bx.build_challenge_bot_xi(_squad(), 0, 11, RULE)
        self.assertEqual(11, len(xi))
        ok, error = xi_rules.validate_challenge_xi(xi, 0, 11, RULE)
        self.assertTrue(ok, error)
        low = [p for p in xi if xi_rules.challenge_rating_or_none(p) <= 75]
        self.assertGreaterEqual(len(low), 3)

    def test_no_rule_changes_nothing(self):
        self.assertEqual([p.id for p in bx.build_challenge_bot_xi(_squad())],
                         [p.id for p in bx.build_challenge_bot_xi(_squad(), 0, 11, [])])


class CareerStatsTests(unittest.TestCase):

    def setUp(self):
        from database import get_session
        from models import (ChallengeLeague, ChallengeMode, Tournament,
                            TournamentPlayerStats, User, Player)
        from services import tournament_service as TS

        self.TS = TS
        self.session = get_session()
        tag = next(_SEQ)
        mode = ChallengeMode(name=f"Mode {tag}")
        self.session.add(mode)
        self.session.flush()
        self.league = ChallengeLeague(name=f"League {tag}", mode_id=mode.id)
        other = ChallengeLeague(name=f"Other {tag}", mode_id=mode.id)
        self.session.add_all([self.league, other])
        self.session.flush()
        user = User(telegram_id=880_000 + tag, first_name="Owner")
        player = Player(name="Kohli", rating=90, category="Batsman",
                        country="India", version="Base", is_active=True,
                        bat_hand="Right", bowl_hand="Right",
                        bowl_style="Medium Pacer")
        self.session.add_all([user, player])
        self.session.flush()

        def tour(league, name):
            t = Tournament(name=name, league_id=league.id)
            self.session.add(t)
            self.session.flush()
            return t

        s1 = tour(self.league, "Season 1")
        s2 = tour(self.league, "Season 2")
        elsewhere = tour(other, "Other Cup")

        def stats(t, runs, hs, wk, br):
            row = TournamentPlayerStats(
                tournament_id=t.id, user_id=user.id, player_id=player.id,
                name="Kohli", team_name="RCB", matches=5, bat_runs=runs,
                bat_balls=runs, bat_outs=2, highest_score=hs, bowl_wickets=wk,
                bowl_runs=40, bowl_balls=30, best_bowl_wickets=wk,
                best_bowl_runs=br)
            self.session.add(row)
            self.session.flush()
            return row

        stats(s1, 300, 90, 2, 20)
        self.current = stats(s2, 200, 110, 3, 25)
        stats(elsewhere, 999, 150, 5, 5)
        self.season2 = s2
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def test_totals_sum_the_league_and_only_the_league(self):
        total = self.TS.career_player_stats(self.session, self.season2,
                                            self.current)
        self.assertEqual(2, total.seasons)
        self.assertEqual(500, total.bat_runs)
        self.assertEqual(10, total.matches)
        self.assertEqual(4, total.bat_outs)
        self.assertEqual(110, total.highest_score)
        self.assertEqual((3, 25), (total.best_bowl_wickets, total.best_bowl_runs))
        self.assertAlmostEqual(125.0, self.TS.batting_average(total))


if __name__ == "__main__":
    unittest.main()

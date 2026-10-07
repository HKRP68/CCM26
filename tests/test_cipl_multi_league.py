"""/cipl multi — each side picks a league, then a team from it.

Pins the pieces that make a cross-league friendly work:

  • argument parsing (``multi``, ``multi 10``, ``multi 100B``)
  • which leagues the admin has put in Multi
  • the league → team picker flow for both captains
  • each side's squad, team id and overseas rule come from its OWN league,
    even when a same-named team exists in the other one
  • a format set on the command is not overwritten by a league's own

Follows the throwaway-sqlite pattern from ``tests/test_tournament_team_lock.py``.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap  # noqa: E402  (sibling helper; see its docstring)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "handlers.challenge")


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
    _seed()


def tearDownModule():
    if _ENGINE is not None:
        _ENGINE.dispose()
    if _PREV_DATABASE_URL is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _PREV_DATABASE_URL
    _module_swap.restore(_SAVED_MODULES)
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


HOST_TG = 7001
GUEST_TG = 7002
IDS = {}


def _seed():
    from database import get_session
    from models import ChallengeLeague, ChallengeMode, ChallengePlayer, ChallengeTeam

    s = get_session()
    try:
        mode = ChallengeMode(name="Multi test mode")
        s.add(mode)
        s.flush()

        def league(name, code, sort, **kw):
            row = ChallengeLeague(mode_id=mode.id, name=name, short_code=code,
                                  sort_order=sort, **kw)
            s.add(row)
            s.flush()
            IDS[code] = row.id
            return row

        def team(lg, name, players, overseas=0):
            row = ChallengeTeam(league_id=lg.id, name=name)
            s.add(row)
            s.flush()
            IDS[(lg.short_code, name)] = row.id
            for i in range(players):
                s.add(ChallengePlayer(team_id=row.id, name=f"{lg.short_code} {name} P{i}",
                                      is_overseas=i < overseas, sort_order=i))

        alpha = league("Alpha League", "ALP", 1, max_overseas=4)
        beta = league("Beta League", "BET", 2, min_overseas=1, max_overseas=11)
        off = league("Off League", "OFF", 3, multi_enabled=False)
        league("Empty League", "EMP", 4)  # no teams → never offered
        league("Dead League", "DED", 5, is_active=False)

        # "Strikers" exists in both Alpha and Beta with different rosters.
        team(alpha, "Strikers", 12)
        team(alpha, "Kings", 12)
        team(beta, "Strikers", 13, overseas=2)
        team(beta, "Heat", 12)
        team(off, "Ghosts", 12)
        s.commit()
    finally:
        s.close()


def _query(data, from_id):
    q = MagicMock()
    q.data = data
    q.from_user.id = from_id
    q.answer = AsyncMock()
    q.edit_message_caption = AsyncMock()
    q.edit_message_text = AsyncMock()
    q.message = MagicMock()
    q.message.reply_text = AsyncMock()
    return q


class ParseMultiArgsTests(unittest.TestCase):
    def setUp(self):
        import handlers.challenge as challenge
        self.parse = challenge.parse_multi_args

    def test_not_multi(self):
        self.assertEqual(self.parse([]), (False, None, None, None))
        self.assertEqual(self.parse(["6"]), (False, None, None, None))

    def test_plain_multi_is_full_t20(self):
        self.assertEqual(self.parse(["Multi"]), (True, None, "T20", None))

    def test_multi_with_overs(self):
        self.assertEqual(self.parse(["multi", "10"]), (True, 10, "T20", None))
        self.assertEqual(self.parse(["MULTI", "T5"]), (True, 5, "T20", None))

    def test_multi_hundred(self):
        for token in ("100B", "100b", "100balls", "the100"):
            self.assertEqual(self.parse(["multi", token]), (True, None, "The100", None))

    def test_hundred_with_overs_is_refused(self):
        ok, overs, fmt, error = self.parse(["multi", "100B", "10"])
        self.assertTrue(ok)
        self.assertIsNotNone(error)

    def test_out_of_range_overs(self):
        self.assertIsNotNone(self.parse(["multi", "50"])[3])


class MultiLeagueListTests(unittest.TestCase):
    def test_only_active_enabled_leagues_with_teams(self):
        import handlers.challenge as challenge
        from database import get_session
        s = get_session()
        try:
            leagues = challenge._multi_leagues(s)
        finally:
            s.close()
        self.assertEqual([l["name"] for l in leagues], ["Alpha League", "Beta League"])
        self.assertEqual(leagues[0]["short"], "ALP")


class MultiPickerFlowTests(unittest.TestCase):
    def setUp(self):
        import handlers.challenge as challenge
        from database import get_session
        self.challenge = challenge
        s = get_session()
        try:
            leagues = challenge._multi_leagues(s)
        finally:
            s.close()
        self.draft = {
            "draft_id": 515151,
            "chat_id": -100,
            "host_tg_id": HOST_TG,
            "target_tg_id": GUEST_TG,
            "league_key": challenge.MULTI_LEAGUE_KEY,
            "league_name": challenge.MULTI_LEAGUE_NAME,
            "multi": True,
            "multi_leagues": leagues,
            "ball_format": "The100",
            "ball_format_locked": True,
            "teams": [],
            "team_codes": {},
            "turn": "host",
            "vs_bot": False,
            "host": {"user_id": 1, "tg_id": HOST_TG, "name": "Ana"},
            "target": {"user_id": 2, "tg_id": GUEST_TG, "name": "Bo"},
        }
        self.context = MagicMock()
        self.context.job_queue = None
        self.context.bot_data = {
            challenge._challenge_team_draft_key(self.draft["draft_id"]): self.draft}

    def _league(self, idx, from_id):
        q = _query(f"cl_mlg_{self.draft['draft_id']}_{idx}", from_id)
        asyncio.run(self.challenge.challenge_multi_league_callback(
            MagicMock(callback_query=q), self.context))
        return q

    def _team(self, name, from_id):
        idx = self.draft["teams"].index(name) if name in self.draft["teams"] else 0
        q = _query(f"cl_team_{self.draft['draft_id']}_{idx}", from_id)
        asyncio.run(self.challenge.challenge_team_callback(
            MagicMock(callback_query=q), self.context))
        return q

    def _buttons(self):
        markup = self.challenge._team_keyboard_for(self.draft, self.draft["draft_id"])
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    def test_host_sees_leagues_first(self):
        data = self._buttons()
        self.assertIn(f"cl_mlg_{self.draft['draft_id']}_0", data)
        self.assertFalse(any(d.startswith("cl_team_") for d in data))
        self.assertIn("pick a league", self.challenge._team_picker_prompt(self.draft, "host"))

    def test_team_before_league_is_refused(self):
        q = self._team("Strikers", HOST_TG)
        self.assertIsNone(self.draft.get("host_team"))
        self.assertTrue(q.answer.call_args.kwargs.get("show_alert"))

    def test_guest_cannot_pick_the_hosts_league(self):
        q = self._league(0, GUEST_TG)
        self.assertNotIn("host_league_key", self.draft)
        self.assertTrue(q.answer.call_args.kwargs.get("show_alert"))

    def test_full_flow_cross_league(self):
        self._league(0, HOST_TG)
        self.assertEqual(self.draft["host_league_name"], "Alpha League")
        self.assertEqual(sorted(self.draft["teams"]), ["Kings", "Strikers"])
        self.assertIn(f"cl_mlg_{self.draft['draft_id']}_back", self._buttons())
        self._team("Strikers", HOST_TG)
        self.assertEqual(self.draft["host_team"], "Strikers")
        self.assertEqual(self.draft["turn"], "target")
        # Guest starts on the league list again.
        self.assertFalse(any(d.startswith("cl_team_") for d in self._buttons()))
        self._league(1, GUEST_TG)
        self.assertEqual(self.draft["target_league_name"], "Beta League")
        # Same name, different league → allowed and not hidden.
        self.assertIn(f"cl_team_{self.draft['draft_id']}_0", self._buttons())
        self._team("Strikers", GUEST_TG)
        self.assertEqual(self.draft["target_team"], "Strikers")
        self.assertEqual(self.draft["turn"], "complete")

        from database import get_session
        s = get_session()
        try:
            self.assertEqual(self.challenge._resolve_team_id(s, self.draft, "host"),
                             IDS[("ALP", "Strikers")])
            self.assertEqual(self.challenge._resolve_team_id(s, self.draft, "target"),
                             IDS[("BET", "Strikers")])
            self.assertEqual(len(self.challenge._query_team_players(s, self.draft, "host")), 12)
            self.assertEqual(len(self.challenge._query_team_players(s, self.draft, "target")), 13)
        finally:
            s.close()

        # Each side keeps its own league's overseas rule.
        self.assertEqual(self.challenge._challenge_overseas_limits(self.draft, "host"), (0, 4))
        self.assertEqual(self.challenge._challenge_overseas_limits(self.draft, "target"), (1, 11))

        # Loading a squad must not replace the format chosen on the command.
        players, team_id, cfg = self.challenge._load_team_players_with_retry(self.draft, "host")
        self.assertEqual(cfg.get("overseas_max"), 4)
        self.assertEqual(self.draft["ball_format"], "The100")

    def test_back_returns_to_league_list(self):
        self._league(0, HOST_TG)
        self._league("back", HOST_TG)
        self.assertNotIn("host_league_key", self.draft)
        self.assertNotIn("host", self.draft.get("side_overseas") or {})
        self.assertFalse(any(d.startswith("cl_team_") for d in self._buttons()))

    def test_same_league_same_team_follows_league_rule(self):
        from database import get_session
        from models import ChallengeLeague
        s = get_session()
        try:
            s.get(ChallengeLeague, IDS["ALP"]).same_team_allowed = False
            s.commit()
            self._league(0, HOST_TG)
            self._team("Kings", HOST_TG)
            self._league(0, GUEST_TG)
            q = self._team("Kings", GUEST_TG)
            self.assertIsNone(self.draft.get("target_team"))
            self.assertTrue(q.answer.call_args.kwargs.get("show_alert"))
        finally:
            s.get(ChallengeLeague, IDS["ALP"]).same_team_allowed = True
            s.commit()
            s.close()


class ImpactSwapPerUserRulesTests(unittest.TestCase):
    def test_by_user_overrides_the_shared_limit(self):
        from services.impact_player import cipl_swap_error
        xi = [{"roster_id": i, "is_overseas": i < 4, "active": True} for i in range(11)]
        incoming = {"roster_id": 99, "is_overseas": True}
        state = {"bat_xi": xi, "bat_user_tg": HOST_TG,
                 "xi_rules": {"min_overseas": 0, "max_overseas": 11,
                              "by_user": {str(HOST_TG): {"min_overseas": 0,
                                                          "max_overseas": 4}}}}
        # Swapping a local out for an overseas player would make five.
        self.assertIn("overseas", cipl_swap_error(state, "bat", 10, incoming).lower())
        state["xi_rules"]["by_user"] = {}
        self.assertNotIn("overseas", cipl_swap_error(state, "bat", 10, incoming).lower())


if __name__ == "__main__":
    unittest.main()

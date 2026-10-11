"""/lpmulti — Lets Play Multi: your own roster vs a Challenge League team.

Pins:
  • "AI" argument parsing
  • the league team's XI: best legal eleven, negative roster ids (never
    colliding with a user's ``UserRoster.id``), the rest of the squad as bench
  • the League → Team picker: who may tap it in each mode, and that a picked
    team moves the draft on to the pitch with its XI cached
  • the Playing XI card and /change treat the league side as fixed
  • /cmulti routes to the Multi League flow; difficulty resumes /lpmulti AI

Follows the throwaway-sqlite pattern from ``tests/test_cipl_multi_league.py``.
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap  # noqa: E402  (sibling helper; see its docstring)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "handlers.challenge",
                 "handlers.letsplay", "handlers.lpmulti", "handlers.lpbot",
                 "handlers.botlevel", "services.bot_xi_builder")


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


HOST_TG = 8101
GUEST_TG = 8102
IDS = {}

# 14-man squad: 2 keepers, 5 batsmen, 2 all-rounders, 5 bowlers.
_SHAPE = (["Wicket Keeper"] * 2 + ["Batsman"] * 5 + ["All-rounder"] * 2
          + ["Bowler"] * 5)


def _seed():
    from database import get_session
    from models import ChallengeLeague, ChallengeMode, ChallengePlayer, ChallengeTeam

    s = get_session()
    try:
        mode = ChallengeMode(name="LP multi test mode")
        s.add(mode)
        s.flush()
        lg = ChallengeLeague(mode_id=mode.id, name="Gamma League",
                             short_code="GAM", sort_order=1, max_overseas=4)
        s.add(lg)
        s.flush()
        IDS["league"] = lg.id

        def team(name, roles):
            row = ChallengeTeam(league_id=lg.id, name=name)
            s.add(row)
            s.flush()
            IDS[name] = row.id
            for i, role in enumerate(roles):
                s.add(ChallengePlayer(
                    team_id=row.id, name=f"{name} P{i}", sort_order=i,
                    is_overseas=i < 6,
                    details_json=json.dumps({
                        "category": role, "rating": 60 + i,
                        "bat_rating": 60 + i, "bowl_rating": 50 + i})))

        team("Titans", _SHAPE)
        team("Minnows", _SHAPE[:9])
        # Twelve players and no keeper: enough bodies, never a legal XI.
        team("Gloveless", ["Batsman"] * 6 + ["Bowler"] * 6)
        s.commit()
    finally:
        s.close()


def _query(data, from_id):
    q = MagicMock()
    q.data = data
    q.from_user.id = from_id
    q.from_user.first_name = "Someone"
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    return q


def _context(bot_data=None):
    ctx = MagicMock()
    ctx.job_queue = None
    ctx.bot_data = bot_data if bot_data is not None else {}
    ctx.user_data = {}
    ctx.bot.send_message = AsyncMock()
    return ctx


class SplitAiArgTests(unittest.TestCase):
    def test_ai_any_case_is_pulled_out(self):
        from handlers.lpmulti import split_ai_arg
        self.assertEqual(split_ai_arg(["AI", "5"]), (True, ["5"]))
        self.assertEqual(split_ai_arg(["5", "ai", "app"]), (True, ["5", "app"]))
        self.assertEqual(split_ai_arg(["@bob"]), (False, ["@bob"]))
        self.assertEqual(split_ai_arg(None), (False, []))


class LeagueTeamXiTests(unittest.TestCase):
    def test_best_legal_xi_with_safe_roster_ids(self):
        from database import get_session
        from services import xi_rules
        from services.bot_xi_builder import LPMULTI_RID_BASE, build_league_team_xi
        s = get_session()
        try:
            xi, bench, error = build_league_team_xi(s, IDS["league"], "Titans")
        finally:
            s.close()
        self.assertIsNone(error)
        self.assertEqual(len(xi), 11)
        self.assertEqual(len(bench), 3)
        ids = [p["roster_id"] for p in xi + bench]
        self.assertEqual(len(set(ids)), 14)
        self.assertTrue(all(rid <= -LPMULTI_RID_BASE for rid in ids))
        self.assertTrue(all(p["is_league_player"] and p["traits"] == []
                            for p in xi))
        self.assertTrue(any(p["category"] == "Wicket Keeper" for p in xi))
        self.assertLessEqual(sum(1 for p in xi if p["is_overseas"]), 4)
        self.assertGreaterEqual(
            sum(1 for p in xi if p["category"] in ("Bowler", "All-rounder")), 5)
        del xi_rules

    def test_short_squad_is_refused(self):
        from database import get_session
        from services.bot_xi_builder import build_league_team_xi
        s = get_session()
        try:
            xi, bench, error = build_league_team_xi(s, IDS["league"], "Minnows")
        finally:
            s.close()
        self.assertEqual(xi, [])
        self.assertIn("Minnows", error)


    def test_a_squad_with_no_legal_xi_is_refused(self):
        from database import get_session
        from services.bot_xi_builder import build_league_team_xi
        s = get_session()
        try:
            xi, _bench, error = build_league_team_xi(s, IDS["league"], "Gloveless")
        finally:
            s.close()
        self.assertEqual(xi, [])
        self.assertIn("legal XI", error)

    def test_a_deleted_league_does_not_match_another_leagues_team(self):
        from database import get_session
        from services.bot_xi_builder import build_league_team_xi
        s = get_session()
        try:
            xi, _bench, error = build_league_team_xi(s, 987654, "Titans")
        finally:
            s.close()
        self.assertEqual(xi, [])
        self.assertIn("no longer exists", error)


class ImpactRulesTests(unittest.TestCase):
    """Only the league side is held to XI rules on an Impact swap."""

    def _state(self):
        xi = [{"roster_id": -(300000 + i), "name": f"L{i}",
               "category": "Bowler" if i >= 6 else "Batsman",
               "is_overseas": i < 4} for i in range(11)]
        xi[0]["category"] = "Wicket Keeper"
        host = [{"roster_id": 10 + i, "name": f"H{i}", "category": "Batsman"}
                for i in range(11)]
        return {"bat_user_tg": GUEST_TG, "bowl_user_tg": HOST_TG,
                "bat_xi": xi, "bowl_xi": host,
                "xi_rules": {"min_overseas": 0, "max_overseas": 11,
                             "by_user_only": True,
                             "by_user": {str(GUEST_TG): {
                                 "min_overseas": 0, "max_overseas": 4}}}}

    def test_league_side_cannot_exceed_its_overseas_cap(self):
        from services.impact_player import cipl_swap_error
        state = self._state()
        sub = {"roster_id": -300050, "name": "OS", "category": "Bowler",
               "is_overseas": True}
        self.assertIn("Overseas", cipl_swap_error(state, "bat", -300010, sub))

    def test_league_side_cannot_drop_its_only_keeper(self):
        from services.impact_player import cipl_swap_error
        state = self._state()
        sub = {"roster_id": -300051, "name": "Bat", "category": "Batsman",
               "is_overseas": False}
        self.assertTrue(cipl_swap_error(state, "bat", -300000, sub))

    def test_host_roster_side_is_not_checked(self):
        from services.impact_player import cipl_swap_error
        state = self._state()
        sub = {"roster_id": 99, "name": "Sub", "category": "Batsman"}
        self.assertEqual(cipl_swap_error(state, "bowl", 10, sub), "")


class PickerFlowTests(unittest.TestCase):
    def _draft(self, vs_bot):
        from handlers import letsplay as lp
        draft = {
            "invite_id": 9001, "chat_id": -555, "is_private": False,
            "status": "lpm_pick", "lpmulti": True, "vs_bot": vs_bot,
            "host": {"user_id": 1, "tg_id": HOST_TG, "name": "Ana"},
            "guest": {"user_id": 2, "tg_id": GUEST_TG, "name": "Bo"},
            "overs": 20,
        }
        self.ctx = _context({lp._dkey(9001): draft})
        return draft

    def _tap(self, data, from_id):
        from handlers import lpmulti
        q = _query(data, from_id)
        with patch.object(lpmulti, "_prompt_pitch", new=AsyncMock()) as pitch:
            asyncio.run(lpmulti.lpmulti_callback(
                MagicMock(callback_query=q), self.ctx))
        return q, pitch

    def _open(self, draft):
        from handlers import lpmulti
        asyncio.run(lpmulti.prompt_league_picker(self.ctx, draft))

    def test_vs_ai_host_picks_league_then_team(self):
        draft = self._draft(vs_bot=True)
        self._open(draft)
        names = [l["name"] for l in draft["lpm_leagues"]]
        idx = names.index("Gamma League")
        self._tap(f"lpm_lg_9001_{idx}", HOST_TG)
        self.assertEqual(draft["lpm_league_name"], "Gamma League")
        team_idx = draft["lpm_teams"].index("Titans")
        _q, pitch = self._tap(f"lpm_tm_9001_{team_idx}", HOST_TG)
        self.assertEqual(draft["status"], "pitch")
        self.assertEqual(draft["guest_team"], "Titans")
        self.assertEqual(len(draft["bot_xi"]), 11)
        self.assertEqual(len(draft["guest_bench"]), 3)
        self.assertEqual(draft["guest_overseas"], [0, 4])
        pitch.assert_awaited_once()

    def test_vs_user_only_the_guest_picks(self):
        draft = self._draft(vs_bot=False)
        self._open(draft)
        q, _ = self._tap("lpm_lg_9001_0", HOST_TG)
        self.assertTrue(q.answer.call_args.kwargs.get("show_alert"))
        self.assertNotIn("lpm_league", draft)
        self._tap("lpm_lg_9001_0", GUEST_TG)
        self.assertIn("lpm_league", draft)

    def test_a_squad_that_cannot_field_eleven_keeps_the_picker(self):
        draft = self._draft(vs_bot=True)
        self._open(draft)
        idx = [l["name"] for l in draft["lpm_leagues"]].index("Gamma League")
        self._tap(f"lpm_lg_9001_{idx}", HOST_TG)
        q, pitch = self._tap(
            f"lpm_tm_9001_{draft['lpm_teams'].index('Minnows')}", HOST_TG)
        self.assertEqual(draft["status"], "lpm_pick")
        self.assertTrue(q.answer.call_args.kwargs.get("show_alert"))
        pitch.assert_not_awaited()

    def _to_team_pick(self):
        draft = self._draft(vs_bot=True)
        self._open(draft)
        idx = [l["name"] for l in draft["lpm_leagues"]].index("Gamma League")
        self._tap(f"lpm_lg_9001_{idx}", HOST_TG)
        return draft, f"lpm_tm_9001_{draft['lpm_teams'].index('Titans')}"

    def test_a_second_team_tap_is_not_an_error(self):
        draft, data = self._to_team_pick()
        self._tap(data, HOST_TG)
        q, pitch = self._tap(data, HOST_TG)
        self.assertFalse(q.answer.call_args.kwargs.get("show_alert"))
        self.assertIn("already picked", q.answer.call_args.args[0])
        self.assertEqual(draft["status"], "pitch")
        self.assertEqual(draft["guest_team"], "Titans")
        pitch.assert_not_awaited()

    def test_a_tap_while_the_squad_loads_waits(self):
        draft, data = self._to_team_pick()
        draft["lpm_busy"] = True
        q, pitch = self._tap(data, HOST_TG)
        self.assertIn("Setting up", q.answer.call_args.args[0])
        self.assertFalse(q.answer.call_args.kwargs.get("show_alert"))
        self.assertEqual(draft["status"], "lpm_pick")
        pitch.assert_not_awaited()

    def test_a_tap_on_an_expired_picker_explains_and_clears_it(self):
        self.ctx = _context({})
        from handlers import lpmulti
        q = _query("lpm_tm_4242_0", HOST_TG)
        q.edit_message_reply_markup = AsyncMock()
        asyncio.run(lpmulti.lpmulti_callback(MagicMock(callback_query=q), self.ctx))
        self.assertIn("expired", q.answer.call_args.args[0])
        self.assertIn("/lpmulti", q.answer.call_args.args[0])
        q.edit_message_reply_markup.assert_awaited_once()

    def test_setup_timeout_names_lpmulti_and_clears_the_picker(self):
        from handlers import letsplay as lp
        draft = self._draft(vs_bot=True)
        draft["lpm_msg_id"] = 77
        self.ctx.bot.edit_message_reply_markup = AsyncMock()
        self.ctx.job = MagicMock(data={"invite_id": 9001})
        asyncio.run(lp._on_setup_timeout(self.ctx))
        text = self.ctx.bot.send_message.call_args.args[1]
        self.assertIn("/lpmulti", text)
        self.ctx.bot.edit_message_reply_markup.assert_awaited_once()
        self.assertNotIn(lp._dkey(9001), self.ctx.bot_data)

    def test_cancel_drops_the_draft(self):
        from handlers import letsplay as lp
        self._draft(vs_bot=False)
        self._tap("lpm_cancel_9001", GUEST_TG)
        self.assertNotIn(lp._dkey(9001), self.ctx.bot_data)


class FixedGuestSideTests(unittest.TestCase):
    def _draft(self):
        xi = [{"roster_id": -(300000 + i), "name": f"L{i}", "rating": 70,
               "category": "Batsman", "traits": []} for i in range(11)]
        bench = [{"roster_id": -300099, "name": "Sub", "rating": 60,
                  "category": "Bowler", "traits": []}]
        return {"invite_id": 77, "chat_id": -9, "status": "showxi",
                "lpmulti": True, "vs_bot": False, "pitch_type": "Hard",
                "host": {"user_id": 1, "tg_id": HOST_TG, "name": "Ana"},
                "guest": {"user_id": 2, "tg_id": GUEST_TG, "name": "Bo"},
                "bot_xi": xi, "guest_bench": bench,
                "guest_team": "Titans", "guest_league_name": "Gamma League"}

    def test_guest_xi_comes_from_the_draft_with_its_bench(self):
        from handlers import letsplay as lp
        draft = self._draft()
        xi, bench, errs = lp._bot_or_user_xi(None, draft, "guest")
        self.assertEqual(len(xi), 11)
        self.assertEqual([p["name"] for p in bench], ["Sub"])
        self.assertEqual(errs, [])

    def test_card_names_the_league_team_and_says_unranked(self):
        from handlers import letsplay as lp
        draft = self._draft()
        text = lp._show_xi_text(draft, draft["bot_xi"], draft["bot_xi"])
        self.assertIn("LETS PLAY MULTI", text)
        self.assertIn("Titans (Gamma League)", text)
        self.assertIn("Practice match", text)

    def test_change_is_refused_for_the_league_side(self):
        from handlers import letsplay as lp
        draft = self._draft()
        ctx = _context({lp._dkey(77): draft})
        ctx.args = ["1", "2"]
        msg = MagicMock()
        msg.reply_text = AsyncMock()
        update = MagicMock(effective_message=msg)
        update.effective_chat.id = -9
        update.effective_user.id = GUEST_TG
        handled = asyncio.run(lp.letsplay_change_handler(update, ctx))
        self.assertTrue(handled)
        self.assertIn("auto-picked", msg.reply_text.call_args.args[0])


class PitchDoubleTapTests(unittest.TestCase):
    def test_a_double_tap_on_the_pitch_shows_one_xi_card(self):
        from handlers import letsplay as lp
        draft = {"invite_id": 31, "chat_id": -1, "status": "pitch",
                 "host": {"user_id": 1, "tg_id": HOST_TG, "name": "Ana"},
                 "guest": {"user_id": 2, "tg_id": GUEST_TG, "name": "Bo"}}
        ctx = _context({lp._dkey(31): draft})
        pitch = lp.PITCH_TYPES[0]

        async def both():
            async def _yield(*_a, **_k):
                await asyncio.sleep(0)   # a real answer() goes to Telegram

            q1, q2 = (_query(f"lp_pitch_31_{pitch}", HOST_TG) for _ in range(2))
            q1.answer.side_effect = q2.answer.side_effect = _yield
            await asyncio.gather(
                lp.letsplay_pitch_callback(MagicMock(callback_query=q1), ctx),
                lp.letsplay_pitch_callback(MagicMock(callback_query=q2), ctx))

        with patch.object(lp, "_prompt_show_xi", new=AsyncMock()) as show, \
                patch.object(lp.R, "edit_rich", new=AsyncMock()):
            asyncio.run(both())
        show.assert_awaited_once()


class RoutingTests(unittest.TestCase):
    def test_cmulti_starts_the_multi_league_flow(self):
        import handlers.challenge as challenge
        msg = MagicMock()
        msg.text = "/cmulti 10"
        msg.reply_text = AsyncMock()
        update = MagicMock(effective_message=msg, message=msg)
        ctx = _context()
        ctx.args = None
        with patch.object(challenge, "_start_multi_challenge",
                          new=AsyncMock()) as start:
            asyncio.run(challenge.challenge_league_handler(update, ctx))
        start.assert_awaited_once()
        self.assertEqual(start.call_args.args[3:], (10, "T20"))

    def test_cmulti_hundred(self):
        import handlers.challenge as challenge
        msg = MagicMock()
        msg.text = "/cmulti 100B"
        update = MagicMock(effective_message=msg, message=msg)
        ctx = _context()
        ctx.args = None
        with patch.object(challenge, "_start_multi_challenge",
                          new=AsyncMock()) as start:
            asyncio.run(challenge.challenge_league_handler(update, ctx))
        self.assertEqual(start.call_args.args[3:], (None, "The100"))

    def test_difficulty_pick_resumes_lpmulti_ai_with_its_overs(self):
        import handlers.botlevel as botlevel
        import handlers.lpmulti as lpmulti
        q = _query(f"botdiff_hard_lpm_5_{HOST_TG}", HOST_TG)
        update = MagicMock(callback_query=q)
        update.effective_user.id = HOST_TG
        ctx = _context()
        with patch.object(lpmulti, "lpmulti_handler", new=AsyncMock()) as h:
            asyncio.run(botlevel.difficulty_callback(update, ctx))
        h.assert_awaited_once()
        self.assertEqual(ctx.args, ["ai", "5"])


if __name__ == "__main__":
    unittest.main()

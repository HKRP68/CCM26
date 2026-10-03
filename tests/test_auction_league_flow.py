"""Auction League end to end, through the real handler and a real database.

A new career from the league picker, retentions, a bid, /simset and
/simtolast, then the first fixture: the match setup must carry the
auction-built squads (not the league teams' rosters), the bot must field an
XI from its auction squad, and a finished match must land in the table.

Follows the throwaway-sqlite pattern from ``tests/test_ciplbot_team_pick.py``.
"""

import asyncio
import json
import os
import random
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap  # noqa: E402

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "handlers.challenge",
                 "handlers.auction_league", "services.auction_league_service",
                 "services.telegram_user_service", "handlers.vsbot")

TG_ID = 555
ROLES = ["Batsman"] * 7 + ["Wicket Keeper"] * 2 + ["All-rounder"] * 4 + ["Bowler"] * 7


def setUpModule():
    global _PREV_DATABASE_URL, _SAVED_MODULES, _TMP, _ENGINE
    _PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
    _SAVED_MODULES = _module_swap.save(_MODULE_NAMES)
    _module_swap.unload(_MODULE_NAMES)
    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"
    from database import Base, engine
    import models  # noqa: F401
    _ENGINE = engine
    Base.metadata.create_all(bind=engine)


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


def _seed_league():
    from database import get_session
    from models import ChallengeLeague, ChallengeMode, ChallengePlayer, ChallengeTeam, User
    s = get_session()
    try:
        existing = s.query(ChallengeLeague).filter(ChallengeLeague.name == "IPL").first()
        if existing is not None:
            return existing.id
        mode = ChallengeMode(name="Leagues AL")
        s.add(mode)
        s.flush()
        lg = ChallengeLeague(mode_id=mode.id, name="IPL", short_code="ipl",
                             command="/cipl", home_country="India",
                             min_overseas=0, max_overseas=4)
        s.add(lg)
        s.flush()
        rng = random.Random(3)
        for t in range(10):
            team = ChallengeTeam(league_id=lg.id, name=f"Team {chr(65 + t)}",
                                 short_name=f"T{chr(65 + t)}")
            s.add(team)
            s.flush()
            for i, role in enumerate(ROLES):
                r = rng.randint(66, 96)
                s.add(ChallengePlayer(
                    team_id=team.id, name=f"{team.short_name} P{i}", is_overseas=(i % 4 == 0),
                    details_json=json.dumps({
                        "category": role, "rating": r,
                        "bat_rating": r if role != "Bowler" else r - 25,
                        "bowl_rating": r if role in ("Bowler", "All-rounder") else 30})))
        if not s.query(User).filter(User.telegram_id == TG_ID).first():
            s.add(User(telegram_id=TG_ID, username="tester", first_name="Tess",
                       total_gems=250))
        s.commit()
        return lg.id
    finally:
        s.close()


def _buttons(markup):
    out = []
    for row in getattr(markup, "inline_keyboard", None) or []:
        for b in row:
            if b.callback_data:
                out.append((b.text, b.callback_data))
    return out


class _Msg:
    def __init__(self, log, mid=1):
        self.log, self.chat_id, self.message_id = log, TG_ID, mid
        self.chat = NS(id=TG_ID, type="private")

    async def reply_text(self, text, reply_markup=None, **kw):
        self.log.append((text, _buttons(reply_markup)))
        return _Msg(self.log, 7)


class _Bot:
    username = "cmubot"

    def __init__(self, log):
        self.log, self.n = log, 100

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.n += 1
        self.log.append((text, _buttons(reply_markup)))
        return _Msg(self.log, self.n)

    async def edit_message_text(self, text, reply_markup=None, **kw):
        self.log.append((text, _buttons(reply_markup)))
        return True

    async def edit_message_reply_markup(self, *a, **kw):
        return True


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.league_id = _seed_league()
        self.log = []
        self.tg = NS(id=TG_ID, username="tester", first_name="Tess", is_bot=False)
        self.chat = NS(id=TG_ID, type="private")
        self.ctx = NS(bot=_Bot(self.log), bot_data={}, user_data={}, args=[],
                      job_queue=None)

    def _update(self, data=None):
        q = None
        if data:
            log = self.log

            async def _ans(*a, **k):
                return None

            async def _edit(text, reply_markup=None, **k):
                log.append((text, _buttons(reply_markup)))

            async def _edrm(**k):
                return None

            q = NS(data=data, from_user=self.tg, message=_Msg(log), answer=_ans,
                   edit_message_text=_edit, edit_message_reply_markup=_edrm)
        return NS(effective_user=self.tg, effective_chat=self.chat,
                  effective_message=_Msg(self.log), callback_query=q)

    def _press(self, data):
        from handlers import auction_league as H
        asyncio.run(H.auction_league_callback(self._update(data), self.ctx))

    def _state(self):
        from database import get_session
        from services import auction_league_service as AL
        s = get_session()
        try:
            row = AL.latest_save(s, TG_ID)
            return row.id, AL.load(row)
        finally:
            s.close()

    def _gems(self):
        from database import get_session
        from models import User
        s = get_session()
        try:
            return s.query(User).filter(User.telegram_id == TG_ID).first().total_gems
        finally:
            s.close()

    def test_help_anywhere(self):
        from handlers import auction_league as H
        # In a group: the guide, not the "open the DM" pointer.
        group = NS(id=-100123, type="supergroup")
        upd = self._update()
        upd.effective_chat = group
        self.ctx.args = ["help"]
        asyncio.run(H.auction_league_handler(upd, self.ctx))
        self.ctx.args = []
        self.assertIn("Auction League — help", self.log[-1][0])
        self.assertIn("@lost_in_space14", self.log[-1][0])
        self.assertLess(len(self.log[-1][0]), 4096)
        # And from the ❓ button.
        self._press("al:help")
        self.assertIn("Auction League — help", self.log[-1][0])

    def test_entry_fee_and_direct_league_start(self):
        from database import get_session
        from handlers import auction_league as H
        from models import User
        from services import auction_league_service as AL
        s = get_session()
        try:
            row = AL.active_save(s, TG_ID)
            if row is not None:
                row.status = AL.PHASE_ABANDONED
            s.query(User).filter(User.telegram_id == TG_ID).update({"total_gems": 40})
            s.commit()
        finally:
            s.close()
        # /rcpl IPL goes straight to the franchise picker.
        self.ctx.args = ["IPL"]
        asyncio.run(H.auction_league_handler(self._update(), self.ctx))
        self.ctx.args = []
        self.assertIn("pick your franchise", self.log[-1][0])
        self._press("al:tm:1")
        self._press("al:sgo")
        # Not enough gems: nothing is charged and no career is created.
        self.assertEqual(self._gems(), 40)
        s = get_session()
        try:
            self.assertIsNone(AL.active_save(s, TG_ID))
            s.query(User).filter(User.telegram_id == TG_ID).update({"total_gems": 250})
            s.commit()
        finally:
            s.close()

    def test_career_to_first_fixture(self):
        from handlers import auction_league as H
        from services import auction_league_service as AL

        asyncio.run(H.auction_league_handler(self._update(), self.ctx))
        self.assertIn("Choose your league", self.log[-1][0])
        self._press(f"al:lg:{self.league_id}")
        self._press("al:tm:0")
        self._press("al:sgo")
        _rid, st = self._state()
        self.assertEqual(st["phase"], AL.PHASE_RETENTION)
        self.assertEqual(st["user_team"], "Team A")
        self.assertEqual(self._gems(), 250 - H.ENTRY_FEE_GEMS)

        # One career at a time: /rcpl IPL now only offers Continue / Discard.
        self.ctx.args = ["IPL"]
        asyncio.run(H.auction_league_handler(self._update(), self.ctx))
        self.ctx.args = []
        self.assertIn("already have an Auction League career", self.log[-1][0])
        self.assertIn(("🗑 Discard career", "al:quit"), self.log[-1][1])

        keep = AL.original_squad(st, "Team A")[:2]
        for c in keep:
            self._press(f"al:rt:{c['id']}")
        self._press("al:rtok")
        _rid, st = self._state()
        self.assertEqual(st["phase"], AL.PHASE_AUCTION)
        self.assertEqual(AL.user_team(st)["purse"], AL.PURSE_LAKH - 1800 - 1400)

        self._press("al:resume")
        _rid, st = self._state()
        lot = st["lot"]
        self.assertIsNotNone(lot)
        self.assertTrue(any(d.startswith("al:pass:") for _t, d in self.log[-1][1]))

        # A stale button from another lot is refused, not applied.
        self._press(f"al:pass:{lot['seq'] + 50}")
        _rid, st2 = self._state()
        self.assertEqual(st2["lot"]["seq"], lot["seq"])

        # Pause: lot buttons are refused and the clock is off until Resume.
        self._press(f"al:pause:{lot['seq']}")
        _rid, st = self._state()
        self.assertTrue(st.get("paused"))
        self._press(f"al:pass:{lot['seq']}")
        _rid, st2 = self._state()
        self.assertEqual(st2["lot"]["seq"], lot["seq"])
        self.assertEqual(st2["lot"]["status"], lot["status"])
        self._press("al:resume")
        _rid, st = self._state()
        self.assertFalse(st.get("paused"))

        # Skip player: exactly this lot is decided, and the next one comes up.
        self._press(f"al:skip:{lot['seq']}")
        _rid, st = self._state()
        self.assertEqual(st["last_lot"]["pid"], lot["pid"])
        self.assertIn(st["last_lot"]["status"], ("sold", "unsold"))
        lot = st["lot"]
        self.assertGreater(lot["seq"], st["last_lot"]["seq"])

        self._press(f"al:simset:{lot['seq']}")
        _rid, st = self._state()
        self.assertEqual(st["lot"]["set_no"], 2)

        asyncio.run(H.simtolast_handler(self._update(), self.ctx))
        rid, st = self._state()
        self.assertEqual(st["phase"], AL.PHASE_SEASON)
        for name in st["team_order"]:
            self.assertTrue(AL.squad_is_legal(st, name), name)

        # The trade window is open after the auction, with its buttons.
        self.assertIsNotNone(AL.trade_window(st))
        window = next(m for m in reversed(self.log) if "Trade window" in m[0])
        self.assertIn(("✅ Close window", "al:trx"), window[1])
        self._press("al:trade")
        self.assertIn("Trade window", self.log[-1][0])

        self._press(f"al:play:{rid}")
        go = next(d for _t, d in self.log[-1][1] if d.startswith("al:go:"))
        fx_no = int(go.split(":")[2])
        self._press(go)
        drafts = [v for v in self.ctx.bot_data.values()
                  if isinstance(v, dict) and v.get("mode") == "auction_league"]
        self.assertEqual(len(drafts), 1)
        draft = drafts[0]
        _rid, st = self._state()
        me = st["user_team"]
        fx = AL.fixture_by_no(st, fx_no)
        # The fixture's own pitch and venue — nobody is asked to pick.
        self.assertEqual(draft["pitch_type"], fx["pitch"])
        self.assertEqual(draft["stadium"], fx["venue"])
        self.assertFalse(any(d.startswith("cl_pitch_") for _t, b in self.log for _l, d in b))
        # Starting the first match closed the trade window.
        self.assertIsNone(AL.trade_window(st))
        # The AI fields its 4 BAT · 1 WK · 3 AR · 3 BOWL, best batters first.
        from handlers.challenge import _challenge_xi_selection
        bot_ids = _challenge_xi_selection(draft, "target")["player_ids"]
        self.assertEqual(len(bot_ids), 11)
        bot_xi = [AL.card(st, i) for i in bot_ids]
        opp = draft["auction_league"]["opp_team"]
        picked = AL.ai_playing_xi(AL.match_cards(st, opp), 4, draft["pitch_type"])
        self.assertEqual(sorted(bot_ids), sorted(c["id"] for c in picked))
        counts = {r: sum(1 for c in bot_xi if c["category"] == r) for r in AL.ROLES}
        squad = AL.match_cards(st, opp)
        dom = {r: sum(1 for c in squad if c["category"] == r and not c["is_overseas"])
               for r in AL.ROLES}
        if all(dom[r] >= n for r, n in AL.XI_TARGET.items()):
            self.assertEqual(counts, {"Batsman": 4, "Wicket Keeper": 1,
                                      "All-rounder": 3, "Bowler": 3})
        self.assertGreaterEqual(counts["All-rounder"] + counts["Bowler"],
                                AL.XI_BOWLING_OPTIONS)
        bats = [c["bat_rating"] for c in bot_xi]
        self.assertEqual(bats, sorted(bats, reverse=True))
        self.assertEqual(draft["host_team"], me)
        self.assertEqual(sorted(c["id"] for c in draft["inline_squads"]["host"]),
                         sorted(e["pid"] for e in st["teams"][me]["squad"]))

        # The XI picker reads the auction squad, not Team A's league roster.
        from database import get_session
        from handlers.challenge import _query_team_players, _resolve_team_id
        s = get_session()
        try:
            players = _query_team_players(s, draft, "host")
            self.assertEqual({p.id for p in players},
                             {e["pid"] for e in st["teams"][me]["squad"]})
            # …and never saves an XI over the league team's remembered one.
            self.assertIsNone(_resolve_team_id(s, draft, "host"))
        finally:
            s.close()

        # A finished match lands in the table, once.
        tag = draft["auction_league"]
        ms = {"auction_league": tag, "match_id": 4242, "inn1_bat_team": me,
              "inn1_runs": 190, "inn1_wickets": 5, "inn1_balls": 120,
              "bat_team_name": tag["opp_team"], "total_runs": 170,
              "total_wickets": 9, "balls": 120, "inn1_bat_stats": {},
              "bat_stats": {}, "inn1_bowl_stats": {}, "bowl_stats": {}}
        s = get_session()
        try:
            self.assertIsNotNone(AL.record_user_result(s, ms, winner_user_id=tag["user_id"]))
            s.commit()
            self.assertIsNone(AL.record_user_result(s, ms, winner_user_id=tag["user_id"]))
        finally:
            s.close()
        _rid, st = self._state()
        self.assertEqual(st["table"][me]["pts"], 2)


class NextSeasonFlowTest(FlowTest):
    """Season N+1 from a finished career (FlowTest's helpers, its own test)."""

    def test_help_anywhere(self):
        pass

    def test_entry_fee_and_direct_league_start(self):
        pass

    def test_career_to_first_fixture(self):
        pass

    def test_start_next_season_charges_once(self):
        from database import get_session
        from models import AuctionLeagueSave, User
        from services import auction_league_service as AL
        from test_auction_league import new_state, play_season
        st = new_state(seed=4)
        AL.apply_retentions(st, [])
        AL.simulate_to_last(st)
        AL.finish_auction(st)
        play_season(st)
        s = get_session()
        try:
            for row in s.query(AuctionLeagueSave).filter(
                    AuctionLeagueSave.user_tg_id == TG_ID).all():
                row.status = AL.PHASE_ABANDONED
            row = AuctionLeagueSave(user_tg_id=TG_ID, league_id=1, league_name="IPL",
                                    user_team_name="Team A", status="completed",
                                    state_json="{}", version=0, reward_paid=True)
            AL.store(row, st)
            s.add(row)
            s.query(User).filter(User.telegram_id == TG_ID).update({"total_gems": 250})
            s.commit()
            old_id = row.id
        finally:
            s.close()
        self._press("al:hub")
        self.assertIn("al:nexts", [d for _t, d in self.log[-1][1]])
        self._press("al:nexts")
        self.assertEqual(self._gems(), 150)
        rid, nxt = self._state()
        self.assertNotEqual(rid, old_id)
        self.assertEqual(nxt["phase"], AL.PHASE_RETENTION)
        self.assertEqual(AL.season_no(nxt), 2)
        self.assertIn("Retention", self.log[-1][0])
        # A second tap never charges again.
        self._press("al:nexts")
        self.assertEqual(self._gems(), 150)
        s = get_session()
        try:
            self.assertTrue(AL.load(s.get(AuctionLeagueSave, old_id))["next_season_started"])
        finally:
            s.close()


class RewardCooldownTest(unittest.TestCase):
    """Only a season that actually paid starts the 48-hour cooldown."""

    def _row(self, s, finish):
        from services import auction_league_service as AL
        from test_auction_league import make_league
        league, teams = make_league(seed=5)
        st = AL.new_state(league, teams, "Team A", seed=5)
        st["phase"] = AL.PHASE_COMPLETED
        st["champion"] = "Team A" if finish == "champion" else "Team B"
        st["runner_up"] = "Team C"
        from models import AuctionLeagueSave
        row = AuctionLeagueSave(user_tg_id=777, league_id=1, league_name="IPL",
                                user_team_name="Team A", status="completed",
                                state_json="{}", version=0)
        AL.store(row, st)
        s.add(row)
        s.flush()
        return row, st

    def test_cooldown_counts_only_paid_seasons(self):
        from database import get_session
        from models import User
        from services import auction_league_service as AL
        s = get_session()
        try:
            user = User(telegram_id=777, username="cool", first_name="Cool",
                        total_coins=0, total_gems=0)
            s.add(user)
            s.flush()
            row1, st1 = self._row(s, "champion")
            self.assertEqual(AL.pay_season_reward(s, row1, st1, user)[:2], AL.REWARD_CHAMPION)
            row2, st2 = self._row(s, "league")      # paid nothing
            self.assertEqual(AL.pay_season_reward(s, row2, st2, user)[:2], (0, 0))
            self.assertIsNone(row2.reward_paid_at)
            row3, st3 = self._row(s, "champion")    # inside 48h of row1
            coins, gems, note = AL.pay_season_reward(s, row3, st3, user)
            self.assertEqual((coins, gems), (0, 0))
            self.assertIn("48", note)
            self.assertIsNone(row3.reward_paid_at)
            # Once row1's window has passed, the refused row3 does not extend it.
            from datetime import datetime, timedelta
            row1.reward_paid_at = datetime.utcnow() - timedelta(hours=49)
            s.flush()
            row4, st4 = self._row(s, "champion")
            self.assertEqual(AL.pay_season_reward(s, row4, st4, user)[:2], AL.REWARD_CHAMPION)
            self.assertEqual(user.total_coins, 2 * AL.REWARD_CHAMPION[0])
        finally:
            s.rollback()
            s.close()


if __name__ == "__main__":
    unittest.main()

"""Ranked ladder, rivalries, spectator predictions, the post-match hook and the
Hall of Fame harvest — against a real (temporary SQLite) database.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap  # noqa: E402

_MODULE_NAMES = (
    "database", "models", "config",
    "services.activity_service", "services.season_service",
    "services.club_service", "services.config_service",
    "services.ranked_service", "services.rivalry_service",
    "services.prediction_service", "services.post_match",
    "services.hall_of_fame",
)
_PREV = None
_SAVED = {}
_TMP = None
_ENGINE = None


def setUpModule():
    global _PREV, _SAVED, _TMP, _ENGINE
    _PREV = os.environ.get("DATABASE_URL")
    _SAVED = _module_swap.save(_MODULE_NAMES)
    _module_swap.unload(_MODULE_NAMES)
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
    if _PREV is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _PREV
    _module_swap.restore(_SAVED)
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


_TG = [880_000_000]


class Case(unittest.TestCase):
    def setUp(self):
        from database import get_session
        self.s = get_session()

    def tearDown(self):
        self.s.rollback()
        self.s.close()

    def user(self, coins=10_000, tg=None, name=None):
        from models import User
        _TG[0] += 1
        u = User(telegram_id=tg if tg is not None else _TG[0], first_name=name or f"U{_TG[0]}",
                 total_coins=coins, total_gems=0)
        self.s.add(u)
        self.s.flush()
        return u

    def match(self, a, b, winner=None, status="completed", margin_type=None,
              margin=None, when=None, chat_id=-100):
        from models import Match
        from services.match_outcome import END_COMPLETED
        m = Match(user1_id=a.id, user2_id=b.id, status=status, chat_id=chat_id,
                  winner_id=winner.id if winner else None,
                  loser_id=(b.id if winner is a else a.id) if winner else None,
                  margin_type=margin_type or ("runs" if winner else "tie"),
                  margin_value=margin if margin is not None else (10 if winner else 0),
                  completed_at=when or datetime.utcnow() if status == "completed" else None,
                  end_reason=END_COMPLETED if status == "completed" else None)
        self.s.add(m)
        self.s.flush()
        return m


# ── ranked ───────────────────────────────────────────────────────────

class RankedMathTests(unittest.TestCase):
    def test_upset_pays_more_than_expected_win(self):
        from services import ranked_service as rs
        upset = rs.rating_delta(1000, 1300, 1, played=20)
        expected = rs.rating_delta(1300, 1000, 1, played=20)
        self.assertGreater(upset, expected)
        self.assertGreaterEqual(expected, 1)

    def test_placement_k_is_bigger(self):
        from services import ranked_service as rs
        self.assertGreater(rs.rating_delta(1000, 1000, 1, played=0),
                           rs.rating_delta(1000, 1000, 1, played=50))

    def test_divisions(self):
        from services import ranked_service as rs
        self.assertEqual(rs.division_for(1000)[0], "Bronze")
        self.assertEqual(rs.division_for(1150)[0], "Gold")
        self.assertEqual(rs.division_for(1600)[0], "Legend")
        self.assertIsNone(rs.next_division(1500))
        self.assertEqual(rs.next_division(1100)[2], 50)
        self.assertEqual(rs.soft_reset(1400), 1200)
        self.assertEqual(rs.soft_reset(800), 900)


class RankedTests(Case):
    def test_win_moves_both_ratings_zero_sum_at_equal_ratings(self):
        from services import ranked_service as rs
        a, b = self.user(), self.user()
        out = rs.apply_result(self.s, a.id, b.id, winner_id=a.id)
        self.assertTrue(out["rated"])
        pa, pb = out["players"][a.id], out["players"][b.id]
        self.assertGreater(pa["delta"], 0)
        self.assertEqual(pa["delta"], -pb["delta"])
        self.assertEqual(rs.get_rating(self.s, a.id).wins, 1)
        self.assertEqual(rs.get_rating(self.s, b.id).losses, 1)

    def test_tie_between_equals_changes_nothing(self):
        from services import ranked_service as rs
        a, b = self.user(), self.user()
        out = rs.apply_result(self.s, a.id, b.id, tie=True)
        self.assertEqual(out["players"][a.id]["delta"], 0)
        self.assertEqual(rs.get_rating(self.s, a.id).draws, 1)

    def test_daily_pair_cap(self):
        from services import post_match
        a, b = self.user(), self.user()
        rated = []
        for _ in range(rs_cap() + 2):
            m = self.match(a, b, winner=a)
            summary = post_match.process_completed_match(self.s, m)
            rated.append(summary["ranked"]["rated"])
        self.assertEqual(rated, [True] * rs_cap() + [False, False])

    def test_season_soft_reset_and_finalize_pays_by_division(self):
        from models import RankedRating, RankedSeasonResult
        from services import ranked_service as rs
        a = self.user(coins=0)
        row = RankedRating(user_id=a.id, season_key="2001-01", rating=1400,
                           peak_rating=1400, career_peak=1400, played=6, wins=5,
                           losses=1, draws=0)
        self.s.add(row)
        self.s.flush()
        rs.finalize_season(self.s, "2001-01")
        res = (self.s.query(RankedSeasonResult)
               .filter(RankedSeasonResult.user_id == a.id).one())
        self.assertEqual(res.division, "Diamond")
        self.assertEqual(a.total_coins, 10000)
        self.assertEqual(a.total_gems, 5)
        # Idempotent.
        rs.finalize_season(self.s, "2001-01")
        self.assertEqual(a.total_coins, 10000)
        # Next touch rolls it into the live season, halfway back to the start.
        live = rs.get_rating(self.s, a.id)
        self.assertEqual(live.rating, 1200)
        self.assertEqual(live.played, 0)
        self.assertEqual(live.career_peak, 1400)


class RankedFinalizeRetryTests(Case):
    def test_a_stale_row_is_paid_before_it_is_reset(self):
        from models import RankedRating, RankedSeasonResult
        from services import ranked_service as rs
        a = self.user(coins=0)
        self.s.add(RankedRating(user_id=a.id, season_key="2002-02", rating=1460,
                                peak_rating=1460, career_peak=1460, played=8,
                                wins=6, losses=2, draws=0))
        self.s.flush()
        # The monthly rollover's payout never happened — the next read retries it.
        row = rs.get_rating(self.s, a.id)
        self.assertEqual(row.rating, 1230)
        res = (self.s.query(RankedSeasonResult)
               .filter(RankedSeasonResult.season_key == "2002-02",
                       RankedSeasonResult.user_id == a.id).one())
        self.assertEqual(res.division, "Legend")
        self.assertEqual(a.total_coins, 20000)


class RankedFinalizeFailureTests(Case):
    def test_a_failing_payout_leaves_the_row_unreset_and_the_match_unrated(self):
        from unittest import mock
        from models import RankedRating
        from services import ranked_service as rs
        a, b = self.user(coins=0), self.user(coins=0)
        self.s.add(RankedRating(user_id=a.id, season_key="2003-03", rating=1400,
                                peak_rating=1400, career_peak=1400, played=7,
                                wins=5, losses=2, draws=0))
        self.s.flush()
        with mock.patch.object(rs, "finalize_season",
                               side_effect=RuntimeError("db down")):
            row = rs.get_rating(self.s, a.id)
            self.assertEqual((row.season_key, row.rating, row.played),
                             ("2003-03", 1400, 7))
            out = rs.apply_result(self.s, a.id, b.id, winner_id=a.id)
            self.assertFalse(out["rated"])
            self.assertIn("pending", out["reason"])
        self.assertEqual(a.total_coins, 0)
        # Once the payout works again, the next read pays and resets.
        row = rs.get_rating(self.s, a.id)
        self.assertEqual(row.rating, 1200)
        self.assertEqual(a.total_coins, 10000)


def rs_cap():
    from services import ranked_service
    return ranked_service.PAIR_DAILY_CAP


# ── rivalry ──────────────────────────────────────────────────────────

class RivalryTests(Case):
    def test_backfill_then_formation_then_round_bonus(self):
        from unittest import mock
        from services import rivalry_service as rv
        # A whole round in one sitting — lift the daily cap for this test.
        cap = mock.patch.object(rv, "BONUS_MATCHES_PER_DAY", 99)
        cap.start()
        self.addCleanup(cap.stop)
        a, b = self.user(coins=0), self.user(coins=0)
        # Four matches of history before the feature saw them.
        for w in (a, a, b, a):
            self.match(a, b, winner=w, when=datetime.utcnow() - timedelta(days=3))
        m5 = self.match(a, b, winner=b)
        out = rv.record_match(self.s, m5)
        self.assertTrue(out["just_formed"])
        self.assertEqual(out["played"], 5)
        self.assertTrue(out["name"])
        lo, hi = sorted([a.id, b.id])
        wins = {lo: out["a_wins"], hi: out["b_wins"]}
        self.assertEqual(wins[a.id], 3)
        self.assertEqual(wins[b.id], 2)
        # The forming match is the first rivalry match: b gets the win bonus.
        self.assertEqual(b.total_coins, rv.WIN_BONUS_COINS)
        # Idempotent on the match id.
        again = rv.record_match(self.s, m5)
        self.assertEqual(again["played"], 5)
        self.assertEqual(b.total_coins, rv.WIN_BONUS_COINS)
        # Four more (a wins 3 of the round's 5) completes round 1.
        last = None
        for w in (a, a, b, a):
            last = rv.record_match(self.s, self.match(a, b, winner=w))
        rr = last["round_result"]
        self.assertIsNotNone(rr)
        self.assertEqual(rr["winner_id"], a.id)
        self.assertEqual(a.total_gems, rv.ROUND_BONUS_GEMS)
        self.assertEqual(a.total_coins, 3 * rv.WIN_BONUS_COINS + rv.ROUND_BONUS_COINS)
        self.assertEqual(last["round"]["round_no"], 2)

    def _rival_pair(self):
        a, b = self.user(coins=0), self.user(coins=0)
        for _ in range(4):
            self.match(a, b, winner=a, when=datetime.utcnow() - timedelta(days=3))
        return a, b

    def test_daily_cap_on_bonuses_but_series_keeps_counting(self):
        from services import rivalry_service as rv
        a, b = self._rival_pair()
        for _ in range(rv.BONUS_MATCHES_PER_DAY + 2):
            last = rv.record_match(self.s, self.match(a, b, winner=a))
        self.assertEqual(a.total_coins, rv.BONUS_MATCHES_PER_DAY * rv.WIN_BONUS_COINS)
        self.assertEqual(last["played"], 4 + rv.BONUS_MATCHES_PER_DAY + 2)
        self.assertEqual(last["round"]["played"], rv.BONUS_MATCHES_PER_DAY)

    def test_short_matches_pay_no_rivalry_bonus(self):
        from services import rivalry_service as rv
        a, b = self._rival_pair()
        m = self.match(a, b, winner=a)
        m.overs = 1
        out = rv.record_match(self.s, m)
        self.assertTrue(out["is_rivalry"])
        self.assertIsNone(out["win_bonus"])
        self.assertEqual(a.total_coins, 0)
        self.assertEqual(out["round"]["played"], 0)

    def test_ai_matches_are_ignored(self):
        from services import rivalry_service as rv
        a, bot = self.user(), self.user(tg=-1 if not self._bot_exists() else -2)
        self.assertIsNone(rv.record_match(self.s, self.match(a, bot, winner=a)))

    def _bot_exists(self):
        from models import User
        return self.s.query(User).filter(User.telegram_id == -1).first() is not None


# ── predictions ──────────────────────────────────────────────────────

LIVE = {"innings": 1}


class PredictionTests(Case):
    def setUp(self):
        super().setUp()
        self.a, self.b = self.user(), self.user()
        self.m = self.match(self.a, self.b, status="playing")

    def test_pool_is_shared_by_stake_plus_bonus(self):
        from services import prediction_service as ps
        x, y, z = self.user(coins=1000), self.user(coins=1000), self.user(coins=1000)
        ps.place(self.s, self.m, x, self.a.id, 100, state=LIVE)
        ps.place(self.s, self.m, y, self.a.id, 300, state=LIVE)
        ps.place(self.s, self.m, z, self.b.id, 400, state=LIVE)
        self.assertEqual(z.total_coins, 600)
        self.m.status, self.m.winner_id, self.m.loser_id = "completed", self.a.id, self.b.id
        self.m.margin_type = "runs"
        out = ps.settle(self.s, self.m)
        self.assertEqual(out["winners"], 2)
        # Pool 800, winning side 400: x gets 200 + 10 bonus, y 600 + 30.
        self.assertEqual(x.total_coins, 900 + 210)
        self.assertEqual(y.total_coins, 700 + 630)
        self.assertEqual(z.total_coins, 600)
        self.assertIsNone(ps.settle(self.s, self.m))   # idempotent

    def test_refunds(self):
        from services import prediction_service as ps
        x = self.user(coins=1000)
        ps.place(self.s, self.m, x, self.b.id, 500, state=LIVE)
        self.m.status, self.m.winner_id, self.m.loser_id = "completed", self.a.id, self.b.id
        self.m.margin_type = "runs"
        out = ps.settle(self.s, self.m)
        self.assertEqual(out["reason"], "nobody backed the winner")
        self.assertEqual(x.total_coins, 1000)

    def test_one_sided_pool_gets_no_bonus(self):
        from services import prediction_service as ps
        x = self.user(coins=5000)
        ps.place(self.s, self.m, x, self.a.id, 5000, state=LIVE)
        self.m.status, self.m.winner_id, self.m.loser_id = "completed", self.a.id, self.b.id
        self.m.margin_type = "runs"
        ps.settle(self.s, self.m)
        self.assertEqual(x.total_coins, 5000)
        self.assertEqual(ps.payout_for(100, 100, 100), 100)
        self.assertEqual(ps.payout_for(100, 100, 300), 310)

    def test_refusals(self):
        from services import prediction_service as ps
        x = self.user(coins=50)
        with self.assertRaises(ps.PredictionError):
            ps.place(self.s, self.m, self.a, self.a.id, 100, state=LIVE)   # own match
        with self.assertRaises(ps.PredictionError):
            ps.place(self.s, self.m, x, self.a.id, 100, state=LIVE)        # too poor
        rich = self.user(coins=10_000)
        with self.assertRaises(ps.PredictionError):
            ps.place(self.s, self.m, rich, self.a.id, 100, state={"innings": 2})
        with self.assertRaises(ps.PredictionError):
            ps.place(self.s, self.m, rich, self.a.id, 100, state=None)     # match over
        ps.place(self.s, self.m, rich, self.a.id, 100, state=LIVE)
        with self.assertRaises(ps.PredictionError):
            ps.place(self.s, self.m, rich, self.b.id, 100, state=LIVE)     # twice

    def test_sweep_refunds_an_abandoned_match(self):
        from services import prediction_service as ps
        x = self.user(coins=1000)
        ps.place(self.s, self.m, x, self.a.id, 1000, state=LIVE)
        self.m.status = "abandoned"
        out = dict((m.id, s) for m, s in ps.sweep(self.s))
        self.assertEqual(out[self.m.id]["refunded"], 1)
        self.assertEqual(x.total_coins, 1000)


# ── post-match hook ──────────────────────────────────────────────────

class PostMatchTests(Case):
    def test_process_is_idempotent_and_renders(self):
        from models import MatchPostResult, User
        from services import post_match
        a, b = self.user(name="Asha"), self.user(name="Bala")
        m = self.match(a, b, winner=a)
        first = post_match.process_completed_match(self.s, m, state={
            "bat_team_name": "Kings", "bat_team_id": a.id,
            "bowl_team_name": "Royals", "bowl_team_id": b.id})
        self.assertTrue(first["ranked"]["rated"])
        before = a.total_coins
        second = post_match.process_completed_match(self.s, m)
        self.assertEqual(second["match_id"], m.id)
        self.assertEqual(a.total_coins, before)
        row = self.s.query(MatchPostResult).filter(MatchPostResult.match_id == m.id).one()
        self.assertTrue(row.rated)
        self.assertEqual(json.loads(row.payload_json)["team_owner"]["Kings"], a.id)
        users = {u.id: u for u in self.s.query(User).filter(User.id.in_([a.id, b.id]))}
        card = post_match.render_card(json.loads(row.payload_json), users)
        self.assertIn("Ranked", card)
        self.assertIn("Asha", card)

    def test_short_custom_match_is_unrated_but_counts_for_the_rivalry(self):
        from services import post_match
        a, b = self.user(), self.user()
        m = self.match(a, b, winner=a)
        m.overs = 3
        out = post_match.process_completed_match(self.s, m)
        self.assertFalse(out["ranked"]["rated"])
        self.assertIn("under 5 overs", out["ranked"]["reason"])
        self.assertEqual(out["rivalry"]["played"], 1)
        self.assertIn("Unrated", post_match.render_card(out, {}))

    def test_uncounted_match_is_not_rated(self):
        from services import post_match
        a, b = self.user(), self.user()
        out = post_match.process_completed_match(self.s, self.match(a, b, winner=a),
                                                 count_result=False)
        self.assertIsNone(out["ranked"])
        self.assertIsNone(out["rivalry"])


# ── Hall of Fame ─────────────────────────────────────────────────────

def _scorecard(bat_team="Kings", bowl_team="Royals"):
    return {"innings": [
        {"number": 1, "bat_team": bat_team, "bowl_team": bowl_team, "runs": 212,
         "wickets": 4, "overs": "20",
         "batting": [{"name": "Ace", "runs": 104, "balls": 50, "sixes": 7, "out": False},
                     {"name": "Low", "runs": 5, "balls": 9, "sixes": 0, "out": True}],
         "bowling": [{"name": "Spin", "overs": "4", "runs": 22, "wickets": 3}]},
        {"number": 2, "bat_team": bowl_team, "bowl_team": bat_team, "runs": 150,
         "wickets": 10, "overs": "18.2", "batting": [], "bowling": []},
        {"number": 3, "super_over": True, "bat_team": bowl_team, "runs": 30,
         "batting": [{"name": "SO", "runs": 99, "balls": 6, "sixes": 9}], "bowling": []},
    ]}


class HallOfFameTests(Case):
    def test_entries_from_scorecard(self):
        from services import hall_of_fame as hof
        rows = hof.entries_from_scorecard(_scorecard(), team_owner={"Kings": 7})
        cats = {r["category"] for r in rows}
        self.assertTrue({"bat_score", "strike_rate", "inn_sixes", "bowl_figures",
                         "team_total"} <= cats)
        bat = next(r for r in rows if r["category"] == "bat_score")
        self.assertEqual(bat["label"], "104* (50)")
        self.assertEqual(bat["user_id"], 7)
        self.assertFalse(any(r.get("player_name") == "SO" for r in rows))

    def test_same_team_name_keeps_each_owner(self):
        from services import hall_of_fame as hof
        sc = _scorecard(bat_team="Kings", bowl_team="Kings")
        rows = hof.entries_from_scorecard(sc, team_owner={"Kings": 1},
                                          innings_owner=[7, 8])
        bat = next(r for r in rows if r["category"] == "bat_score")
        bowl = next(r for r in rows if r["category"] == "bowl_figures")
        self.assertEqual(bat["user_id"], 7)      # innings 1 batted first
        self.assertEqual(bowl["user_id"], 8)     # the other side bowled it
        # Without per-innings ids, identical names are not guessed at.
        rows = hof.entries_from_scorecard(sc, team_owner={"Kings": 1})
        self.assertTrue(all(r["user_id"] is None for r in rows
                            if r["category"] != "win_runs"))

    def test_scan_harvests_once_and_skips_uncounted(self):
        from models import HallOfFameEntry, MatchPostResult, MatchScorecard
        from services import hall_of_fame as hof, post_match
        a, b = self.user(), self.user()
        m = self.match(a, b, winner=a, margin=62)
        post_match.process_completed_match(self.s, m, state={
            "bat_team_name": "Kings", "bat_team_id": a.id,
            "bowl_team_name": "Royals", "bowl_team_id": b.id})
        self.s.add(MatchScorecard(match_id=m.id, scorecard_json=json.dumps(_scorecard())))
        bad = self.match(a, b, winner=b)
        post_match.process_completed_match(self.s, bad, count_result=False)
        self.s.add(MatchScorecard(match_id=bad.id, scorecard_json=json.dumps(_scorecard())))
        self.s.flush()
        hof.scan(self.s)
        hof.scan(self.s)
        entries = self.s.query(HallOfFameEntry).filter(
            HallOfFameEntry.match_id.in_([m.id, bad.id])).all()
        self.assertTrue(entries)
        self.assertTrue(all(e.match_id == m.id for e in entries))
        self.assertEqual(len([e for e in entries if e.category == "bat_score"]), 1)
        win = next(e for e in entries if e.category == "win_runs")
        self.assertEqual(win.value, 62)
        self.assertEqual(win.team_name, "Kings")
        top = hof.top_entries(self.s, "bat_score", limit=1)[0]
        self.assertEqual(top.user_id, a.id)
        self.assertTrue(self.s.query(MatchPostResult)
                        .filter(MatchPostResult.match_id == bad.id).one().hof_done)

    def test_career_boards_render(self):
        from services import hall_of_fame as hof
        u = self.user()
        u.best_streak, u.matches_won = 9, 40
        self.s.flush()
        boards = hof.career_boards(self.s)
        titles = [t for t, _e, _r in boards]
        self.assertIn("Longest win streak", titles)
        streak = dict((t, r) for t, _e, r in boards)["Longest win streak"]
        self.assertTrue(any(uid == u.id for _v, _n, uid in streak))


if __name__ == "__main__":
    unittest.main()

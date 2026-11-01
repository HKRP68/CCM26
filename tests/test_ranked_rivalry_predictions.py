"""Ranked ladder, rivalries, the retired predictions' refund, the post-match hook and the
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
    "services.match_rewards", "services.login_streak_service",
    "services.quest_service", "services.quick_match_service",
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

    def test_repeat_wins_shrink_but_losses_keep_full_cost(self):
        from services import ranked_service as rs
        self.assertEqual([rs.rating_delta(1000, 1000, 1, 20, n)
                          for n in range(4)], [12, 6, 3, 0])
        self.assertEqual([rs.rating_delta(1000, 1000, 0, 20, n)
                          for n in range(3)], [-12, -12, -12])

    def test_season_points_reward_difficulty_and_reduce_repeats(self):
        from services import ranked_service as rs
        self.assertEqual([rs.season_win_points(1000, 1000, n)
                          for n in range(4)], [25, 12, 6, 0])
        self.assertGreater(rs.season_win_points(1000, 1200), 25)
        self.assertLess(rs.season_win_points(1200, 1000), 25)

    def test_low_opponent_cutoff_has_no_minimum_point_loophole(self):
        from services import ranked_service as rs
        self.assertGreater(rs.rating_delta(1399, 1000, 1, 20), 0)
        self.assertGreater(rs.season_win_points(1399, 1000), 0)
        for gap in (400, 401, 800):
            self.assertEqual(rs.rating_delta(1000 + gap, 1000, 1, 20), 0)
            self.assertEqual(rs.season_win_points(1000 + gap, 1000), 0)
        self.assertGreater(rs.rating_delta(1000, 1400, 1, 20), 12)
        self.assertLess(rs.rating_delta(1400, 1000, 0, 20), -12)

    def test_repeated_unequal_draws_cannot_farm_full_rating_gains(self):
        from services import ranked_service as rs
        gains = [rs.rating_delta(1000, 1300, 0.5, 20, n) for n in range(3)]
        self.assertGreater(gains[0], gains[1])
        self.assertGreater(gains[1], gains[2])

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


# ── predictions (retired) ────────────────────────────────────────────

class PredictionRefundTests(Case):
    """/predict is gone; any stake still open is handed back once."""

    def test_open_stakes_are_refunded_once_and_settled_ones_untouched(self):
        from models import MatchPrediction
        from services import prediction_service as ps
        a, b = self.user(), self.user()
        m = self.match(a, b, status="playing")
        x, y = self.user(coins=0), self.user(coins=0)
        self.s.add(MatchPrediction(match_id=m.id, user_id=x.id,
                                   pick_user_id=a.id, stake=500, status="open"))
        self.s.add(MatchPrediction(match_id=m.id, user_id=y.id,
                                   pick_user_id=b.id, stake=100, status="won",
                                   payout=210))
        self.s.flush()
        self.assertEqual(ps.refund_all_open(self.s), 1)
        self.assertEqual(x.total_coins, 500)
        self.assertEqual(y.total_coins, 0)
        self.assertEqual(ps.refund_all_open(self.s), 0)
        self.assertEqual(x.total_coins, 500)


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

    def test_ranked_season_points_are_paid_once_and_only_to_the_winner(self):
        from services import post_match
        a, b = self.user(), self.user()
        m = self.match(a, b, winner=a)
        result = post_match.process_completed_match(self.s, m)
        self.assertEqual(result["ranked"]["players"][a.id]["season_points"], 25)
        self.assertEqual((a.season_points, a.season_wins), (25, 1))
        self.assertEqual((b.season_points, b.season_wins), (0, 0))
        post_match.process_completed_match(self.s, m)
        self.assertEqual((a.season_points, a.season_wins), (25, 1))
        self.assertIn("Season points", post_match.render_card(result, {}))

    def test_repeat_points_decline_and_stop_at_the_pair_cap(self):
        from services import post_match
        a, b = self.user(), self.user()
        awards = []
        for n in range(4):
            # Deliberately swap sides: it must still be the same pair.
            sides = (a, b) if n % 2 == 0 else (b, a)
            m = self.match(*sides, winner=a)
            result = post_match.process_completed_match(self.s, m)
            awards.append(result["ranked"]["players"].get(a.id, {}).get("season_points", 0))
            if n in (1, 2):
                self.assertIn("Repeat opponent", post_match.render_card(result, {}))
        self.assertGreater(awards[0], awards[1])
        self.assertGreater(awards[1], awards[2])
        self.assertEqual(awards[3], 0)
        self.assertEqual((a.season_points, a.season_wins), (sum(awards), 3))

    def test_new_opponent_receives_full_gain_after_repeat_cap(self):
        from services import post_match
        a, b, c = self.user(), self.user(), self.user()
        for _ in range(3):
            post_match.process_completed_match(self.s, self.match(a, b, winner=a))
        out = post_match.process_completed_match(self.s, self.match(a, c, winner=a))
        self.assertTrue(out["ranked"]["rated"])
        self.assertEqual(out["ranked"]["gain_multiplier"], 1)

    def test_rolling_limit_survives_midnight_and_expires_after_24_hours(self):
        from services import post_match, ranked_service as rs
        a, b = self.user(), self.user()
        now = datetime.utcnow()
        prior = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(minutes=1)
        for _ in range(3):
            post_match.process_completed_match(
                self.s, self.match(a, b, winner=a, when=prior))
        out = rs.apply_result(self.s, b.id, a.id, winner_id=a.id, now=prior + timedelta(minutes=2))
        self.assertFalse(out["rated"])
        self.assertIn("24 hours", out["reason"])
        out = rs.apply_result(self.s, a.id, b.id, winner_id=a.id, now=prior + timedelta(hours=24))
        self.assertTrue(out["rated"])
        self.assertEqual(out["gain_multiplier"], 1)

    def test_weak_opponent_win_pays_nothing_and_explains_why(self):
        from services import post_match, ranked_service as rs
        a, b = self.user(), self.user()
        rs.get_rating(self.s, a.id, create=True).rating = 1400
        out = post_match.process_completed_match(self.s, self.match(a, b, winner=a))
        self.assertTrue(out["ranked"]["rated"])
        self.assertEqual(out["ranked"]["players"][a.id]["delta"], 0)
        self.assertEqual((a.season_points, a.season_wins), (0, 0))
        self.assertIn("400+", post_match.render_card(out, {}))

    def test_draw_does_not_earn_season_points_or_wins(self):
        from services import post_match
        a, b = self.user(), self.user()
        out = post_match.process_completed_match(self.s, self.match(a, b))
        self.assertTrue(out["ranked"]["rated"])
        self.assertEqual((a.season_points, b.season_points), (0, 0))
        self.assertEqual((a.season_wins, b.season_wins), (0, 0))

    def test_unranked_state_and_modes_never_earn_season_points(self):
        from services import post_match
        from services.match_outcome import TYPE_SUPER_OVER, TYPE_VSBOT
        for state, mode in (({"unranked": True}, None),
                            ({"stats_disabled": True}, None),
                            ({"is_vsbot": True}, None),
                            ({}, TYPE_SUPER_OVER), ({}, TYPE_VSBOT),
                            ({}, "casual")):
            with self.subTest(state=state, mode=mode):
                a, b = self.user(), self.user()
                m = self.match(a, b, winner=a)
                m.match_type = mode
                out = post_match.process_completed_match(self.s, m, state=state)
                self.assertFalse(out["ranked"]["rated"])
                self.assertEqual((a.season_points, a.season_wins), (0, 0))

    def test_bot_opponent_never_earns_ranked_or_season_points(self):
        from services import post_match
        a, bot = self.user(), self.user(tg=-1)
        out = post_match.process_completed_match(self.s, self.match(a, bot, winner=a))
        self.assertIsNone(out["ranked"])
        self.assertEqual((a.season_points, a.season_wins), (0, 0))

    def test_unfinished_and_resultless_matches_are_not_treated_as_ranked_draws(self):
        from services import post_match
        a, b = self.user(), self.user()
        m = self.match(a, b, status="playing")
        self.assertIsNone(post_match.process_completed_match(self.s, m))
        m.status = "completed"
        m.margin_type = "cancelled"
        m.end_reason = "cleared_by_user"
        self.assertFalse(post_match.process_completed_match(self.s, m)["ranked"]["rated"])
        m = self.match(a, b)
        m.margin_type = None
        self.assertFalse(post_match.process_completed_match(self.s, m)["ranked"]["rated"])
        self.assertEqual((a.season_points, b.season_points), (0, 0))

    def test_season_write_failure_rolls_back_rating_and_can_be_retried(self):
        from unittest import mock
        from services import post_match, ranked_service as rs, season_service as ss
        a, b = self.user(), self.user()
        m = self.match(a, b, winner=a)
        with mock.patch.object(ss, "add_ranked_season_points", side_effect=RuntimeError("db down")):
            self.assertIsNone(post_match.process_completed_match(self.s, m))
        self.assertIsNone(rs.get_rating(self.s, a.id))
        self.assertEqual(a.season_points, 0)
        out = post_match.process_completed_match(self.s, m)
        self.assertTrue(out["ranked"]["rated"])
        self.assertEqual((a.season_points, a.season_wins), (25, 1))

    def test_ranked_win_resets_last_seasons_counters_before_crediting(self):
        from services import post_match, season_service as ss
        a, b = self.user(), self.user()
        a.season_key, a.season_points, a.season_wins = "2001-01", 900, 40
        post_match.process_completed_match(self.s, self.match(a, b, winner=a))
        self.assertEqual(a.season_key, ss.current_season_key())
        self.assertEqual((a.season_points, a.season_wins), (25, 1))

    def test_generic_match_rewards_pay_currency_without_season_points(self):
        from services.match_rewards import award_match_rewards_core
        a, b = self.user(coins=0), self.user(coins=0)
        payout = award_match_rewards_core(self.s, a.id, b.id, overs=20)
        self.assertGreater(payout[0], 0)
        self.assertEqual(a.total_coins, payout[0])
        self.assertEqual((a.season_points, a.season_wins), (0, 0))
        self.assertEqual((b.season_points, b.season_wins), (0, 0))

    def test_login_reward_adds_activity_points_without_season_wins(self):
        from models import UserStats
        from services.login_streak_service import claim_login_reward
        a = self.user(coins=0)
        stats = UserStats(user_id=a.id)
        self.s.add(stats)
        self.s.flush()
        out = claim_login_reward(self.s, a, stats)
        self.assertTrue(out["ok"])
        self.assertGreater(a.total_coins, 0)
        self.assertEqual((a.season_points, a.season_wins), (3, 0))
        self.assertFalse(claim_login_reward(self.s, a, stats)["ok"])
        self.assertEqual(a.season_points, 3)

    def test_quest_claim_adds_activity_points_without_season_wins(self):
        from models import Quest, UserQuestProgress
        from services.quest_service import claim_quest_reward, period_key_for
        a = self.user(coins=0)
        quest = Quest(name="Ranked-only season regression", description="Win one match",
                      quest_type="daily",
                      event_key="match_won", target_count=1, reward_points=10,
                      reward_coins=50, reward_gems=0)
        self.s.add(quest)
        self.s.flush()
        self.s.add(UserQuestProgress(
            user_id=a.id, quest_id=quest.id, period_key=period_key_for("daily"),
            progress=1, completed=True, claimed=False))
        self.s.flush()
        ok, _message, reward = claim_quest_reward(self.s, a.id, quest.id)
        self.assertTrue(ok)
        self.assertEqual(reward["points"], 10)
        self.assertEqual(a.quest_points, 10)
        self.assertGreater(a.total_coins, 0)
        self.assertEqual((a.season_points, a.season_wins), (3, 0))
        self.assertFalse(claim_quest_reward(self.s, a.id, quest.id)[0])
        self.assertEqual(a.season_points, 3)

    def test_quick_match_win_pays_currency_without_season_points(self):
        from unittest import mock
        from services import quick_match_service as qm
        a = self.user(coins=0)
        state = {
            "innings_1": {"runs": 150, "wickets": 5, "balls": 120, "phase_history": []},
            "innings_2": {"runs": 100, "wickets": 10, "balls": 100, "phase_history": []},
            "target": 151, "user_bats_first": True,
            "user_xi": [], "bot_xi": [], "user_rating": 80, "bot_rating": 80}
        with mock.patch.object(qm, "_generate_batting_card", return_value=[]), \
                mock.patch.object(qm, "_generate_bowling_card", return_value=[]):
            result = qm._finalize_match(self.s, a, state, target_chased=False)
        self.assertTrue(result["user_won"])
        self.assertGreater(a.total_coins, 0)
        self.assertEqual(a.quick_matches_won, 1)
        self.assertEqual((a.season_points, a.season_wins), (0, 0))

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
        self.assertEqual((a.season_points, a.season_wins), (0, 0))

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

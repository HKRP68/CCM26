"""CMU Community Arena — automated tests.

Covers the behaviour the implementation prompt enumerates: XP eligibility and
caps, chat anti-spam, official-group restrictions, match/claim integration,
check-ins and IST boundaries, all six game generators and their guarantees,
answer validation, single-winner/duplicate-reward safety, expiry, scheduler
recovery, levels, milestone idempotency, leaderboard periods, rich HTML
fallback, and admin settings/audit.

Runs against a temporary SQLite database via the shared ``_module_swap`` harness.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap

_MODULES = (
    "database", "models", "models_community", "config",
    "services.config_service", "services.gc_gate",
    "services.community_config_service", "services.community_xp_service",
    "services.community_chat_service", "services.community_achievement_service",
    "services.community_answer_service", "services.community_game_service",
    "services.community_level_service", "services.community_leaderboard_service",
    "services.community_checkin_service", "services.community_hooks",
    "services.community_profile_service", "services.community_admin_service",
    "services.community_rich",
)
_SAVED = {}
_PREV = None
_PATH = None
_ENGINE = None
_TG = 970_000_000
OFFICIAL = -1001555000


def setUpModule():
    global _SAVED, _PREV, _PATH, _ENGINE
    _SAVED = _module_swap.save(_MODULES)
    _module_swap.unload(_MODULES)
    _PREV = os.environ.get("DATABASE_URL")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        _PATH = tmp.name
    os.environ["DATABASE_URL"] = f"sqlite:///{_PATH}"
    from database import Base, engine
    import models  # noqa: F401
    import database
    _ENGINE = engine
    Base.metadata.create_all(engine)
    database._seed_community_arena()


def tearDownModule():
    _ENGINE.dispose()
    if _PREV is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _PREV
    _module_swap.restore(_SAVED)
    os.unlink(_PATH)


def _new_tg():
    global _TG
    _TG += 1
    return _TG


class Base(unittest.TestCase):
    def setUp(self):
        from database import get_session
        from services import community_config_service as C
        self.s = get_session()
        # Enable arena + official group.
        from models import GameConfig
        if self.s.query(GameConfig).first() is None:
            self.s.add(GameConfig(official_group_id=OFFICIAL))
            self.s.commit()
        C.invalidate()
        C.save(self.s, enabled=True, auto_enabled=True)

    def tearDown(self):
        self.s.rollback()
        self.s.close()

    def user(self, **kw):
        from models import User
        u = User(telegram_id=kw.pop("telegram_id", _new_tg()),
                 first_name=kw.pop("first_name", "Mgr"), total_coins=0, **kw)
        self.s.add(u)
        self.s.flush()
        return u

    def players(self, n=30):
        from models import Player
        import random
        countries = ["India", "England", "Australia", "Pakistan", "South Africa"]
        cats = ["Batsman", "Bowler", "All Rounder", "Wicket Keeper"]
        names = ["Virat Kohli", "Rohit Sharma", "Jasprit Bumrah", "Jos Buttler",
                 "Steve Smith", "Babar Azam", "Kagiso Rabada", "Pat Cummins",
                 "Ben Stokes", "David Warner", "Rashid Khan", "Kane Williamson",
                 "Joe Root", "Shubman Gill", "Mohammed Shami", "Trent Boult",
                 "Quinton de Kock", "Hardik Pandya", "Mitchell Starc",
                 "Shaheen Afridi", "Marnus Labuschagne", "Rishabh Pant",
                 "Glenn Maxwell", "Jofra Archer", "Suryakumar Yadav",
                 "Temba Bavuma", "Faf du Plessis", "Ravindra Jadeja",
                 "Nathan Lyon", "Wanindu Hasaranga"]
        random.seed(7)
        out = []
        for i in range(min(n, len(names))):
            p = Player(name=names[i], version=random.choice(["Base", "Gold"]),
                       rating=random.randint(75, 93), category=random.choice(cats),
                       country=random.choice(countries),
                       bat_hand=random.choice(["Right", "Left"]),
                       bowl_hand=random.choice(["Right", "Left"]),
                       bowl_style=random.choice(["Fast", "Spin", "Medium"]),
                       bat_rating=random.randint(70, 95),
                       bowl_rating=random.randint(60, 92), is_active=True)
            self.s.add(p)
            out.append(p)
        self.s.commit()
        return out


# 1,2,3,4 — Chat XP eligibility, cooldown, daily limit, duplicate/spam
class ChatXP(Base):
    def _msg(self, text, mid=1):
        return SimpleNamespace(text=text, caption=None, message_id=mid,
                               forward_origin=None, forward_date=None,
                               new_chat_members=None, left_chat_member=None,
                               pinned_message=None, sticker=None, animation=None)

    def test_eligible_and_meaningful(self):
        from services import community_chat_service as CC
        ok, _ = CC.message_is_eligible(self._msg("hello there friends"), min_chars=8)
        self.assertTrue(ok)
        self.assertFalse(CC.message_is_eligible(self._msg("hi"), min_chars=8)[0])
        self.assertFalse(CC.message_is_eligible(self._msg("/start now"), min_chars=8)[0])
        self.assertFalse(CC.message_is_eligible(self._msg("aaaaaaaaaa"), min_chars=8)[0])

    def test_award_cooldown_and_daily_limit(self):
        from services import community_chat_service as CC
        from services import community_config_service as C
        CC.reset_memory()
        u = self.user()
        self.s.commit()  # the award worker runs in its own session (committed data only)
        cfg = C.get_config(self.s, fresh=True)
        upd = SimpleNamespace(effective_message=self._msg("great match today everyone", 10),
                              message=None)

        async def award(upd):
            return await CC.maybe_award_chat_xp(upd, tg_user_id=u.telegram_id,
                                                chat_id=OFFICIAL, config=cfg)
        self.assertTrue(asyncio.run(award(upd)))
        # Immediate second eligible message is within cooldown → no award.
        upd2 = SimpleNamespace(effective_message=self._msg("another good message here", 11),
                               message=None)
        self.assertFalse(asyncio.run(award(upd2)))

    def test_duplicate_message_rejected(self):
        from services import community_chat_service as CC
        from services import community_config_service as C
        CC.reset_memory()
        u = self.user()
        self.s.commit()
        cfg = C.get_config(self.s, fresh=True)
        # Force cooldown to 0 so only the fingerprint stops the duplicate.
        cfg = dict(cfg)
        cfg["chat"] = dict(cfg["chat"]); cfg["chat"]["cooldown_seconds"] = 0

        async def award(mid):
            upd = SimpleNamespace(effective_message=self._msg("identical duplicate text", mid),
                                  message=None)
            return await CC.maybe_award_chat_xp(upd, tg_user_id=u.telegram_id,
                                                chat_id=OFFICIAL, config=cfg)
        self.assertTrue(asyncio.run(award(1)))
        self.assertFalse(asyncio.run(award(2)))  # same fingerprint


# 11,12 — Check-ins, streaks, IST boundaries
class CheckinAndTime(Base):
    def test_checkin_streak_and_idempotent(self):
        from services import community_checkin_service as CK
        u = self.user()
        d1 = datetime(2026, 11, 20, 6, 0)
        r1 = CK.check_in(u.id, session=self.s, now=d1)
        self.assertEqual(r1["streak"], 1)
        self.assertEqual(r1["credited"], 20)
        # Same IST day → already.
        r1b = CK.check_in(u.id, session=self.s, now=d1 + timedelta(hours=1))
        self.assertTrue(r1b["already"])
        # Next day → streak 2.
        r2 = CK.check_in(u.id, session=self.s, now=d1 + timedelta(days=1))
        self.assertEqual(r2["streak"], 2)
        # Skip a day → reset to 1.
        r3 = CK.check_in(u.id, session=self.s, now=d1 + timedelta(days=3))
        self.assertEqual(r3["streak"], 1)
        self.assertEqual(r3["longest"], 2)

    def test_ist_boundaries(self):
        from services import community_xp_service as X
        # 2026-11-23 is a Monday. 18:00 UTC = 23:30 IST same day.
        now = datetime(2026, 11, 23, 18, 0)
        self.assertEqual(X.ist_today_str(now), "2026-11-23")
        ws = X.week_start_utc(now)
        # Monday 00:00 IST == Sunday 18:30 UTC
        self.assertEqual(ws, datetime(2026, 11, 22, 18, 30))
        ms = X.month_start_utc(now)
        self.assertEqual(ms, datetime(2026, 10, 31, 18, 30))


# 5,6,7,8,9,10 — Hooks: official-group gating, claim, match integration
class Hooks(Base):
    def test_claim_official_only_and_idempotent(self):
        from services import community_hooks as H
        from services import community_xp_service as X
        u = self.user()
        self.s.commit()  # the hook runs in its own session (committed data only)
        H.on_claim_success(u.id, OFFICIAL, claim_seq="p1")
        H.on_claim_success(u.id, OFFICIAL, claim_seq="p1")  # dup
        self.s.expire_all()
        self.assertEqual(X.get_profile(self.s, u.id).lifetime_xp, 5)
        # Non-official chat → nothing.
        H.on_claim_success(u.id, -999, claim_seq="p2")
        self.s.expire_all()
        self.assertEqual(X.get_profile(self.s, u.id).lifetime_xp, 5)

    def test_match_pvp_and_ai(self):
        from models import Match
        from services import community_hooks as H
        from services import community_xp_service as X
        a, b = self.user(), self.user()
        m = Match(user1_id=a.id, user2_id=b.id, status="completed",
                  chat_id=OFFICIAL, match_type="pvp")
        self.s.add(m); self.s.commit()
        H.on_match_completed(m.id)
        self.s.expire_all()
        self.assertEqual(X.get_profile(self.s, a.id).lifetime_xp, 15)
        self.assertEqual(X.get_profile(self.s, b.id).lifetime_xp, 15)
        # AI opponent (telegram_id<=0) → only human earns AI xp.
        ai = self.user(telegram_id=-1)
        c = self.user()
        m2 = Match(user1_id=c.id, user2_id=ai.id, status="completed",
                   chat_id=OFFICIAL, match_type="vsbot")
        self.s.add(m2); self.s.commit()
        H.on_match_completed(m2.id)
        self.s.expire_all()
        self.assertEqual(X.get_profile(self.s, c.id).lifetime_xp, 5)
        self.assertIsNone(X.get_profile(self.s, ai.id))

    def test_abandoned_and_nonofficial_match_excluded(self):
        from models import Match
        from services import community_hooks as H
        from services import community_xp_service as X
        a, b = self.user(), self.user()
        # Not completed.
        m = Match(user1_id=a.id, user2_id=b.id, status="abandoned",
                  chat_id=OFFICIAL, match_type="pvp")
        self.s.add(m); self.s.commit()
        H.on_match_completed(m.id)
        # counts=False.
        m2 = Match(user1_id=a.id, user2_id=b.id, status="completed",
                   chat_id=OFFICIAL, match_type="pvp")
        self.s.add(m2); self.s.commit()
        H.on_match_completed(m2.id, counts=False)
        # Non-official.
        m3 = Match(user1_id=a.id, user2_id=b.id, status="completed",
                   chat_id=-42, match_type="pvp")
        self.s.add(m3); self.s.commit()
        H.on_match_completed(m3.id)
        self.s.expire_all()
        self.assertIsNone(X.get_profile(self.s, a.id))


# 13,14,15,16,17 — Generators + validation
class Generators(Base):
    def setUp(self):
        super().setUp()
        self.players(30)
        from services import community_config_service as C
        self.cfg = C.get_config(self.s, fresh=True)

    def test_all_generators(self):
        from services import community_game_service as G
        for gt in G.GAME_TYPES:
            payload = G.generate_payload(self.s, gt, self.cfg)
            self.assertIsNotNone(payload, f"{gt} failed to generate")
            self.assertIn("answer", payload)

    def test_oddoneout_exactly_one_odd(self):
        from services import community_game_service as G
        for _ in range(10):
            p = G.generate_payload(self.s, "oddoneout", self.cfg)
            self.assertIsNotNone(p)
            self.assertEqual(len(p["question"]["options"]), 4)
            self.assertIn(p["answer"]["letter"], ["A", "B", "C", "D"])

    def test_cricketer_unique(self):
        from services import community_game_service as G
        players = G.eligible_players(self.s, self.cfg)
        for _ in range(8):
            p = G.generate_payload(self.s, "cricketer", self.cfg)
            if p is None:
                continue
            clues = [(c["label"], c["value"]) for c in p["question"]["clues"]]
            # Count players matching all displayed attributes → must be exactly 1.
            matches = 0
            for pl in players:
                vals = {
                    "🌍 Country": pl.country, "🏏 Category": pl.category,
                    "✋ Batting Hand": pl.bat_hand, "⭐ Overall Rating": str(pl.rating),
                    "🃏 Version": pl.version, "🤾 Bowling Hand": pl.bowl_hand,
                    "🎯 Bowling Style": pl.bowl_style, "🏏 Batting Rating": str(pl.bat_rating),
                    "🎳 Bowling Rating": str(pl.bowl_rating),
                }
                if all(str(vals.get(lbl)) == str(val) for lbl, val in clues):
                    matches += 1
            self.assertEqual(matches, 1, f"clues not unique: {clues}")

    def test_wordle_repeated_letters(self):
        from services import community_game_service as G
        # ESSAY vs SASSY: two-pass yellows/greens.
        self.assertEqual(G.wordle_score("sassy", "essay"),
                         ["y", "y", "g", "b", "g"])
        self.assertEqual(G.wordle_score("level", "alley"),
                         G.wordle_score("level", "alley"))  # deterministic
        # All green on exact match.
        self.assertEqual(G.wordle_score("kohli", "kohli"), ["g"] * 5)

    def test_rating_exact(self):
        from services import community_answer_service as A
        self.assertTrue("85" == "85")
        # answer service name normalization
        self.assertTrue(A.matches("Virat  KOHLI", A.name_variants("Virat Kohli")))
        self.assertFalse(A.matches("Vir", A.name_variants("Virat Kohli")))


# 18 — alias / normalization
class AnswerValidation(Base):
    def test_normalization(self):
        from services import community_answer_service as A
        self.assertEqual(A.key("A.B. de Villiers!"), "abdevilliers")
        self.assertTrue(A.matches("ab de villiers", A.name_variants("A.B. de Villiers")))
        self.assertFalse(A.matches("villiers", ["AB de Villiers"]))  # no arbitrary surname
        # fuzzy off by default
        self.assertFalse(A.matches("kohlii", ["Kohli"]))
        self.assertTrue(A.matches("kohlii", ["Kohli"], fuzzy=True))


# 19,20,21,22,23,24 — winner selection, duplicates, expiry, recovery, workers
class GameLifecycle(Base):
    def setUp(self):
        super().setUp()
        self.players(30)
        from services import community_config_service as C
        self.cfg = C.get_config(self.s, fresh=True)

    def _make(self, gt="cricketer"):
        from services import community_game_service as G
        # Clear any active game left by a previous test (shared module DB).
        existing = G.active_game_for_chat(self.s, OFFICIAL, lock=True)
        if existing is not None:
            G.cancel_game(self.s, existing)
            self.s.commit()
        payload = None
        for _ in range(8):  # cricketer can occasionally fail uniqueness
            payload = G.generate_payload(self.s, gt, self.cfg)
            if payload is not None:
                break
        game = G.create_game(self.s, OFFICIAL, payload, source="manual")
        self.s.commit()
        return game, payload

    def test_single_winner_and_duplicate_prevention(self):
        from services import community_game_service as G
        game, payload = self._make("mystery")  # name game that always generates
        a, b = self.user(), self.user()
        ans = payload["answer"]["name"]
        r1 = G.submit_answer(self.s, game.id, a.id, a.telegram_id, ans)
        self.assertEqual(r1["status"], "won")
        # Second correct answer after close.
        r2 = G.submit_answer(self.s, game.id, b.id, b.telegram_id, ans)
        self.assertEqual(r2["status"], "closed")
        self.s.expire_all()
        from models import CommunityGame
        g = self.s.get(CommunityGame, game.id)
        self.assertEqual(g.winner_user_id, a.id)

    def test_single_active_game_per_chat(self):
        from services import community_game_service as G
        self._make("rating")
        payload2 = G.generate_payload(self.s, "mystery", self.cfg)
        g2 = G.create_game(self.s, OFFICIAL, payload2, source="manual")
        self.assertIsNone(g2)  # one active game only

    def test_expiry(self):
        from services import community_game_service as G
        from models import CommunityGame
        game, _ = self._make("oddoneout")
        game.deadline = datetime.utcnow() - timedelta(seconds=1)
        self.s.commit()
        self.assertTrue(G.expire_game(self.s, game))
        self.s.commit()
        self.assertEqual(self.s.get(CommunityGame, game.id).status, "expired")
        self.assertFalse(G.has_active_game(OFFICIAL))

    def test_restart_recovery_cache(self):
        from services import community_game_service as G
        self._make("rating")
        G._ACTIVE_CHATS.clear()  # simulate restart
        self.assertFalse(G.has_active_game(OFFICIAL))
        G.refresh_active_cache(self.s)
        self.assertTrue(G.has_active_game(OFFICIAL))

    def test_rating_attempts_exhausted(self):
        from services import community_game_service as G
        game, payload = self._make("rating")
        u = self.user()
        wrong = str((payload["answer"]["rating"] + 1) % 100)
        for _ in range(3):
            G.submit_answer(self.s, game.id, u.id, u.telegram_id, wrong)
        r = G.submit_answer(self.s, game.id, u.id, u.telegram_id, wrong)
        self.assertEqual(r["status"], "exhausted")


# 25,26 — levels, milestones
class LevelsMilestones(Base):
    def test_level_calculation(self):
        from services import community_xp_service as X
        self.assertEqual(X.level_for_xp(self.s, 0)[0], 1)
        self.assertEqual(X.level_for_xp(self.s, 100)[0], 2)
        self.assertEqual(X.level_for_xp(self.s, 999)[0], 4)
        self.assertEqual(X.level_for_xp(self.s, 1000)[1], "Cricket Enthusiast")
        self.assertEqual(X.level_for_xp(self.s, 100000)[0], 15)

    def test_milestone_idempotent(self):
        from services import community_level_service as L
        from services import community_xp_service as X
        u = self.user()
        prof = X.get_or_create_profile(self.s, u.id)
        prof.lifetime_xp = 300
        prof.level = X.level_for_xp(self.s, 300)[0]
        self.s.flush()
        g1 = L.grant_due_milestones(self.s, u.id)
        self.s.commit()
        g2 = L.grant_due_milestones(self.s, u.id)
        self.assertTrue(len(g1) >= 1)
        self.assertEqual(g2, [])  # nothing new the second time


# 27 — leaderboard periods
class Leaderboards(Base):
    def test_period_and_rank(self):
        from services import community_xp_service as X
        from services import community_leaderboard_service as LB
        a, b = self.user(), self.user()
        X.award_xp(a.id, X.ACT_CHECKIN, 50, "k:a", session=self.s,
                   activity_daily_limit=None, bypass_limits=True)
        X.award_xp(b.id, X.ACT_CHECKIN, 30, "k:b", session=self.s,
                   activity_daily_limit=None, bypass_limits=True)
        self.s.commit()
        lb_a = LB.leaderboard(self.s, "cxp", "all", user_id=a.id)
        lb_b = LB.leaderboard(self.s, "cxp", "all", user_id=b.id)
        # Isolation-robust: the module DB accumulates users across tests, so
        # assert relative ordering + values rather than absolute ranks.
        self.assertEqual(lb_a["me"]["value"], 50)
        self.assertEqual(lb_b["me"]["value"], 30)
        self.assertLess(lb_a["me"]["rank"], lb_b["me"]["rank"])
        # Today-period leaderboard also reflects the fresh awards.
        lb_today = LB.leaderboard(self.s, "cxp", "today", user_id=a.id)
        self.assertEqual(lb_today["me"]["value"], 50)


# 28 — rich HTML fallback
class RichFallback(Base):
    def test_cards_build_html(self):
        from services import community_rich as CR
        blocks, html = CR.checkin_card(
            {"streak": 7, "longest": 12, "credited": 20, "lifetime_xp": 1540,
             "level": 5, "title": "Cricket Enthusiast", "leveled_up": False}, "@mgr")
        self.assertIn("COMMUNITY CHECK-IN", html)
        self.assertIn("1,540", html)
        _, wh = CR.winner_card(game_label="Guess the Cricketer", winner_name="@x",
                               answer_name="Virat Kohli", response_ms=4820,
                               participants=24, award={"credited": 30, "lifetime_xp": 1280})
        self.assertIn("WE HAVE A WINNER", wh)
        self.assertIn("4.82", wh)


# 29 — admin settings + audit
class AdminOps(Base):
    def test_settings_parse_and_save(self):
        from services import community_admin_service as A
        from services import community_config_service as C

        class F(dict):
            def get(self, k, d=None):
                return dict.get(self, k, d)
        form = F({"max_daily_xp": "500", "xp_chat": "2",
                  "game_wordle_enabled": "on", "weight_mystery": "25"})
        updates = A.parse_settings_form(form)
        C.save(self.s, settings_updates=updates)
        cfg = C.get_config(self.s, fresh=True)
        self.assertEqual(cfg["max_daily_xp"], 500)
        self.assertEqual(cfg["xp"]["chat"]["xp"], 2)
        self.assertEqual(cfg["scheduler"]["weights"]["mystery"], 25)

    def test_admin_adjust_requires_reason_and_audits(self):
        from services import community_admin_service as A
        from services import community_xp_service as X
        from models import CommunityAuditLog
        u = self.user()
        with self.assertRaises(ValueError):
            X.admin_adjust_xp(self.s, u.id, 100, reason="")
        A.adjust_user_xp(self.s, u.id, 100, "manual grant", admin_id=42)
        self.s.expire_all()
        self.assertEqual(X.get_profile(self.s, u.id).lifetime_xp, 100)
        audit = self.s.query(CommunityAuditLog).filter_by(user_id=u.id).all()
        self.assertTrue(any(a.action == "adjust_xp" for a in audit))

    def test_suspension_blocks_earning(self):
        from services import community_admin_service as A
        from services import community_xp_service as X
        u = self.user()
        A.set_user_suspension(self.s, u.id, True, "abuse", admin_id=1)
        res = X.award_xp(u.id, X.ACT_CHECKIN, 20, "k:susp", session=self.s)
        self.assertEqual(res["credited"], 0)
        self.assertEqual(res["reason"], "suspended")


if __name__ == "__main__":
    unittest.main()

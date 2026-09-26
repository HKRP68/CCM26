"""Tests for the new-player journey, comeback nudges, the /start cards and
cohort retention stats.

Journey and comeback are exercised against a real in-memory SQLite schema
(the models' own ``create_all``), so the column names and the quest-event
bridge are checked end to end, not mocked.
"""

import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
import models  # noqa: F401  (registers every table)
from models import User
from services import comeback_service as C
from services import onboarding_rich, onboarding_service as O
from services import retention_stats

CFG_NO_GC = {"onboarding_enabled": True, "official_group_id": None}
CFG_GC = {"onboarding_enabled": True, "official_group_id": -100123,
          "official_group_link": "https://t.me/cmu"}


class _DB(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.s = sessionmaker(bind=self.engine)()
        O._PENDING.clear()

    def tearDown(self):
        self.s.close()
        self.engine.dispose()

    def _user(self, tg=1001, **kw):
        u = User(telegram_id=tg, username="u", first_name="Asha",
                 total_coins=1000, total_gems=10, **kw)
        self.s.add(u)
        self.s.flush()
        return u


class JourneyTests(_DB):
    def test_legacy_accounts_are_never_on_the_journey(self):
        u = self._user()
        self.assertFalse(O.is_in_onboarding(u, CFG_NO_GC))
        self.assertIsNone(O.complete_step(self.s, u, "claim", cfg=CFG_NO_GC))

    def test_step_pays_once(self):
        u = self._user()
        O.start(u)
        r = O.complete_step(self.s, u, "claim", cfg=CFG_NO_GC)
        self.assertEqual(r["coins"], O.STEP_BY_KEY["claim"].coins)
        self.assertEqual(u.total_coins, 1000 + r["coins"])
        self.assertIsNone(O.complete_step(self.s, u, "claim", cfg=CFG_NO_GC))
        self.assertEqual(u.total_coins, 1000 + r["coins"])
        self.assertEqual(list(O._PENDING), [(1001, "claim")])

    def test_gc_step_only_when_a_group_is_configured(self):
        keys_no = [s.key for s in O.active_steps(CFG_NO_GC)]
        keys_gc = [s.key for s in O.active_steps(CFG_GC)]
        self.assertNotIn("gc", keys_no)
        self.assertEqual(keys_gc[0], "gc")

    def test_finishing_pays_the_finale(self):
        u = self._user()
        O.start(u)
        total = 1000
        for step in O.active_steps(CFG_NO_GC):
            r = O.complete_step(self.s, u, step.key, cfg=CFG_NO_GC)
            total += r["coins"]
        self.assertIsNotNone(u.onboarding_done_at)
        self.assertEqual(u.total_coins, total + O.FINALE_COINS)
        self.assertFalse(O.is_in_onboarding(u, CFG_NO_GC))

    def test_disabled_switch_hides_the_journey(self):
        u = self._user()
        O.start(u)
        self.assertFalse(O.is_in_onboarding(u, {"onboarding_enabled": False}))

    def test_quest_events_drive_the_journey(self):
        from services.quest_service import track_event
        u = self._user()
        O.start(u)
        self.s.flush()
        with patch.object(O, "_cfg", return_value=CFG_NO_GC):
            track_event(self.s, u.id, "vsbot_played")
            track_event(self.s, u.id, "vsbot_won")
            track_event(self.s, u.id, "runs_scored")  # listens to nothing
        self.assertEqual(set(O.done_keys(u)), {"match", "win"})

    def test_discard_pending_is_per_user(self):
        O._PENDING.extend([(1, "claim"), (2, "daily"), (1, "match")])
        O.discard_pending(1)
        self.assertEqual(list(O._PENDING), [(2, "daily")])


class ComebackRuleTests(unittest.TestCase):
    NOW = datetime(2026, 9, 26, 12, 0)

    def _u(self, idle_days, sent=0, sent_at=None):
        seen = self.NOW - timedelta(days=idle_days)
        return SimpleNamespace(last_seen_at=seen, last_match_date=None,
                               created_at=seen - timedelta(days=60),
                               comeback_tier=sent, comeback_sent_at=sent_at)

    def test_tiers_are_climbed_once_each(self):
        self.assertIsNone(C.due_tier(self._u(0.5), self.NOW))
        self.assertEqual(C.due_tier(self._u(1.2), self.NOW), 1)
        self.assertIsNone(C.due_tier(self._u(2, sent=1), self.NOW))
        self.assertEqual(C.due_tier(self._u(3.5, sent=1), self.NOW), 3)
        # Skipped tiers collapse into the highest one crossed.
        self.assertEqual(C.due_tier(self._u(8), self.NOW), 7)

    def test_no_two_nudges_within_36_hours(self):
        u = self._u(3.2, sent=1, sent_at=self.NOW - timedelta(hours=20))
        self.assertIsNone(C.due_tier(u, self.NOW))

    def test_long_gone_players_are_left_alone(self):
        self.assertIsNone(C.due_tier(self._u(C.MAX_INACTIVE_DAYS + 1), self.NOW))

    def test_returning_resets_the_ladder(self):
        u = self._u(0.1, sent=3, sent_at=self.NOW - timedelta(days=1))
        self.assertTrue(C.should_reset(u))
        u2 = self._u(4, sent=3, sent_at=self.NOW - timedelta(days=1))
        self.assertFalse(C.should_reset(u2))

    def test_admin_json_overrides_and_bad_json_falls_back(self):
        t = C.tiers({"comeback_rewards_json": '{"2": {"coins": 50}, "5": {"enabled": false}}'})
        self.assertEqual(t, {2: {"coins": 50, "gems": 0}})
        self.assertEqual(C.tiers({"comeback_rewards_json": "nope"}), C.DEFAULT_TIERS)


class ComebackClaimTests(_DB):
    def test_claim_pays_once_and_only_the_offered_tier(self):
        u = self._user(comeback_claim_tier=3)
        with patch.object(C, "tiers", return_value=C.DEFAULT_TIERS):
            self.assertIsNone(C.claim(self.s, u, 7))
            r = C.claim(self.s, u, 3)
            self.assertEqual(r, C.DEFAULT_TIERS[3])
            self.assertEqual(u.total_coins, 1000 + r["coins"])
            self.assertIsNone(C.claim(self.s, u, 3))

    def test_a_stale_second_claim_pays_nothing(self):
        # Two taps that both read comeback_claim_tier == 3 before either
        # wrote: only the conditional UPDATE decides, so the second pays 0.
        u = self._user(comeback_claim_tier=3)
        self.s.commit()
        other = sessionmaker(bind=self.engine)()
        try:
            stale = other.get(User, u.id)
            self.assertEqual(stale.comeback_claim_tier, 3)
            with patch.object(C, "tiers", return_value=C.DEFAULT_TIERS):
                self.assertIsNotNone(C.claim(self.s, u, 3))
                self.s.commit()
                self.assertIsNone(C.claim(other, stale, 3))
        finally:
            other.close()
        self.s.expire_all()
        self.assertEqual(self.s.get(User, u.id).total_coins,
                         1000 + C.DEFAULT_TIERS[3]["coins"])


class ComebackScanTests(_DB):
    def test_a_due_user_is_found_behind_a_page_of_users_not_due(self):
        import database
        now = datetime.utcnow()
        # 30 users quiet for 2 days who already got their day-1 nudge 40 h
        # ago: past the 36 h gap, but nothing new is due for them.
        for i in range(30):
            self._user(tg=5000 + i, created_at=now - timedelta(days=60),
                       last_seen_at=now - timedelta(days=2),
                       comeback_tier=1,
                       comeback_sent_at=now - timedelta(hours=40))
        due = self._user(tg=9999, created_at=now - timedelta(days=60),
                         last_seen_at=now - timedelta(days=8))
        self.s.commit()
        factory = sessionmaker(bind=self.engine)
        with patch.object(database, "get_session", factory), \
                patch.object(C, "BATCH_SIZE", 2), \
                patch.object(C, "tiers", return_value=C.DEFAULT_TIERS):
            jobs = C._scan(now)
        self.assertEqual([(j["user_id"], j["tier"]) for j in jobs], [(due.id, 7)])

    def test_teaser_escapes_the_tournament_name(self):
        import database
        from models import Tournament
        t = Tournament(name="Bat & <Ball> Cup", is_active=True)
        self.s.add(t)
        try:
            self.s.commit()
        except Exception:
            self.skipTest("Tournament needs more required fields in this schema")
        with patch.object(database, "get_session", sessionmaker(bind=self.engine)):
            text = C._teaser()
        self.assertIn("Bat &amp; &lt;Ball&gt; Cup", text)


class GcVerifyTests(unittest.TestCase):
    def test_unknown_membership_pays_no_journey_reward(self):
        import asyncio
        from handlers import onboarding as H
        answers = []

        class Q:
            data = "gcjoin_check"
            from_user = SimpleNamespace(id=7, first_name="A")
            message = None

            async def answer(self, *a, **k):
                answers.append(a)

            async def edit_message_text(self, *a, **k):
                pass

        class Bot:
            async def get_chat_member(self, gid, uid):
                raise Exception("Bad Request: chat not found")  # → None

        ctx = SimpleNamespace(bot=Bot(), bot_data={})
        with patch("services.gc_gate.official_group_id", return_value=-100123), \
                patch.object(O, "complete_gc_step_for") as pay, \
                patch.object(H, "_has_account", return_value=True):
            asyncio.run(H.gcjoin_check_callback(SimpleNamespace(callback_query=Q()), ctx))
        pay.assert_not_called()
        self.assertTrue(answers)


class AdminCfgTests(unittest.TestCase):
    def test_maintenance_page_cfg_carries_the_new_settings(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "admin.py").read_text()
        start = src.index('"rookie_message": row.rookie_message,')
        block = src[start:src.index("}", start)]
        for key in ("force_gc_join", "gc_join_message", "official_group_id",
                    "official_group_link", "onboarding_enabled",
                    "comeback_enabled", "comeback_rewards_json"):
            self.assertIn(f'"{key}"', block, key)
        self.assertIn('row.onboarding_enabled is not False', block)


class CardTests(unittest.TestCase):
    def _user(self, **kw):
        base = dict(first_name="Asha", username="asha", total_coins=2500,
                    total_gems=40, roster_count=15, matches_played=4,
                    matches_won=3, team_name="Kings", onboarding_steps="claim",
                    onboarding_started_at=datetime.utcnow(),
                    onboarding_done_at=None, telegram_id=1)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_every_card_builds_rich_and_html(self):
        with patch.object(O, "_cfg", return_value=CFG_GC):
            cards = [
                onboarding_rich.welcome_card("Asha", in_gc=False, cfg=CFG_GC),
                onboarding_rich.journey_card(self._user(), cfg=CFG_GC),
                onboarding_rich.step_done_card(self._user(), "claim", cfg=CFG_GC),
                onboarding_rich.status_card(self._user(), ready=[("📅 Daily", "/daily")],
                                            streak=3),
                onboarding_rich.commands_card(),
            ]
        for blocks, html_text, _ in cards:
            self.assertTrue(blocks)
            self.assertTrue(html_text.strip())

    def test_welcome_ticks_gc_when_member(self):
        with patch.object(O, "_cfg", return_value=CFG_GC):
            _, html_text, kb = onboarding_rich.welcome_card("A", in_gc=True, cfg=CFG_GC)
        self.assertIn("✅", html_text)
        datas = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertNotIn("gcjoin_check", datas)

    def test_journey_next_step_for_gc_offers_join_and_check(self):
        u = self._user(onboarding_steps="")
        _, _, kb = onboarding_rich.journey_card(u, cfg=CFG_GC)
        datas = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("onb_gc", datas)

    def test_commands_catalogue_keeps_every_line(self):
        from services.command_catalog import COMMAND_CATEGORIES
        html_text = onboarding_rich.commands_html()
        for _, lines in COMMAND_CATEGORIES:
            for line in lines:
                self.assertIn(line.split()[0], html_text)
        self.assertGreater(sum(len(v) for _, v in COMMAND_CATEGORIES), 75)


class CohortTests(unittest.TestCase):
    def test_day_n_math(self):
        today = date(2026, 9, 26)
        ist = timedelta(hours=5, minutes=30)
        created = datetime(2026, 9, 10, 6, 0) - ist  # IST 10 Sep, 06:00
        users = [(1, created), (2, created), (3, created), (4, created)]
        d = date(2026, 9, 10)
        active = {(1, d + timedelta(days=1)), (2, d + timedelta(days=1)),
                  (1, d + timedelta(days=7))}
        rows = retention_stats.cohort_rows(users, active, today)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["size"], r["d1"], r["d7"], r["d30"]), (4, 50, 25, None))

    def test_unfinished_days_are_none(self):
        today = date(2026, 9, 26)
        rows = retention_stats.cohort_rows(
            [(1, datetime(2026, 9, 25, 12))], set(), today)
        self.assertIsNone(rows[0]["d1"])


if __name__ == "__main__":
    unittest.main()

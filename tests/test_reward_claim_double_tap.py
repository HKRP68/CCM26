"""Regression coverage: every Mini App reward button pays out at most once.

`/api/webapp/daily` was hardened first, but it is only one button on the Daily
screen. The login-streak ladder and the quest claims sat next to it with *no*
protection at all — no busy flag on the client, no in-flight guard on the
server, and a plain read-then-write in the service. Three taps on the ladder
button were three lots of coins and gems.

Every claim now has the same three layers the pack shop uses:

  1. Service — the "have you already had this" flag is taken with a conditional
     UPDATE *before* any currency moves, so whoever flips it owns the reward and
     a racing caller matches no row. This is the layer that holds even when the
     other two don't: it needs no lock and no shared process state.
  2. Endpoint — ``claim_once``/``release`` allow one claim per user in flight;
     the duplicate gets a 409. Plus a `SELECT … FOR UPDATE` on the deciding row,
     the same one ``pack_service._lock_buyer_row`` takes on a purchase.
  3. UI — the button disables itself and a module-level busy flag drops taps
     that are already queued.

Layers 2 and 3 are asserted against source text: the Mini App is a single Jinja
template with no JS test harness, and importing ``admin`` needs Flask, which the
lean test env doesn't have.
"""

import os
import unittest
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _func(src, signature):
    """Slice out one function body by brace matching from its signature."""
    start = src.index(signature)
    depth = 0
    for j in range(src.index("{", start), len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f"unterminated function: {signature}")


class _DBCase(unittest.TestCase):
    """A real user + stats row on an in-memory engine."""

    def setUp(self):
        from database import Base
        from models import User, UserStats
        self.User, self.UserStats = User, UserStats
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.user = User(telegram_id=5001, first_name="Tapper",
                         total_coins=0, total_gems=0)
        self.db.add(self.user)
        self.db.flush()
        self.stats = UserStats(user_id=self.user.id)
        self.db.add(self.stats)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()


# ════════════════════════════════════════════════════════════════════
# 1. Login-streak ladder — the button the report was about
# ════════════════════════════════════════════════════════════════════

class LoginRewardSingleClaimTests(_DBCase):
    def _claim(self, session=None, stats=None):
        from services.login_streak_service import claim_login_reward
        session = session or self.db
        return claim_login_reward(session, self.user, stats or self.stats)

    def test_first_claim_pays(self):
        result = self._claim()
        self.db.commit()
        self.assertTrue(result["ok"], result)
        self.assertGreater(self.user.total_coins or 0, 0)

    def test_second_claim_the_same_day_is_refused(self):
        first = self._claim()
        self.db.commit()
        coins_after_one = self.user.total_coins

        second = self._claim()
        self.assertFalse(second["ok"])
        self.assertEqual(second["error"], "already_claimed")
        self.assertEqual(self.user.total_coins, coins_after_one)
        self.assertGreater(first["reward"]["coins"], 0)

    def test_three_taps_pay_exactly_once(self):
        """The reported bug: tapped 3 times, rewarded 3 times."""
        paid = 0
        for _ in range(3):
            if self._claim().get("ok"):
                paid += 1
                self.db.commit()
        self.assertEqual(paid, 1, "only one of three taps may pay out")

    def test_a_second_request_holding_a_stale_row_still_loses(self):
        """The layer that survives a stale read.

        Modelled the way it actually happens: a second request loads its own
        UserStats *before* the first claim commits, so its in-memory row says
        the day is unclaimed and it walks straight past the read check — exactly
        what an unrefreshed row lock hands you. The conditional UPDATE is what
        stops it.
        """
        from services.login_streak_service import claim_login_reward
        other = self.Session()
        try:
            other_user = other.query(self.User).get(self.user.id)
            other_stats = other.query(self.UserStats).get(self.user.id)
            self.assertIsNone(other_stats.login_reward_claimed_date)

            self.assertTrue(self._claim()["ok"])
            self.db.commit()
            coins_after_one = self.user.total_coins

            second = claim_login_reward(other, other_user, other_stats)
            self.assertFalse(second["ok"], "the compare-and-set must refuse it")
            self.assertEqual(second["error"], "already_claimed")
            other.rollback()
        finally:
            other.close()

        self.db.expire_all()
        self.assertEqual(
            self.db.query(self.User).get(self.user.id).total_coins,
            coins_after_one)

    def test_the_row_is_stamped_before_the_payout(self):
        """Ordering is the whole point — the flag must not trail the coins."""
        from services import login_streak_service as svc
        seen = {}
        original = svc._claim_today

        def spy(session, user, today):
            seen["coins_when_claimed"] = user.total_coins or 0
            return original(session, user, today)

        svc._claim_today = spy
        try:
            self.assertTrue(self._claim()["ok"])
        finally:
            svc._claim_today = original
        self.assertEqual(seen["coins_when_claimed"], 0,
                         "the day must be claimed before any coins are paid")

    def test_sessionless_callers_still_work(self):
        """The ladder-maths unit tests pass no session; they must not break."""
        from services.login_streak_service import claim_login_reward

        class _Stats:
            login_streak = 3
            login_best_streak = 3
            last_login_date = datetime.utcnow().strftime("%Y-%m-%d")
            login_reward_claimed_date = None

        result = claim_login_reward(None, self.user, _Stats())
        self.assertTrue(result["ok"], result)


# ════════════════════════════════════════════════════════════════════
# 2. Quest claims
# ════════════════════════════════════════════════════════════════════

class QuestClaimSingleClaimTests(_DBCase):
    def setUp(self):
        super().setUp()
        from models import Quest, UserQuestProgress
        from services.quest_service import period_key_for
        self.UserQuestProgress = UserQuestProgress
        self.quest = Quest(name="Play a match", description="Play one match",
                           quest_type="daily",
                           event_key="match_played", target_count=1,
                           reward_coins=2500, reward_gems=3, reward_points=10,
                           is_active=True)
        self.db.add(self.quest)
        self.db.flush()
        self.progress = UserQuestProgress(
            user_id=self.user.id, quest_id=self.quest.id,
            period_key=period_key_for("daily"),
            progress=1, completed=True, claimed=False)
        self.db.add(self.progress)
        self.db.commit()

    def _claim(self):
        from services.quest_service import claim_quest_reward
        return claim_quest_reward(self.db, self.user.id, self.quest.id)

    def test_first_claim_pays(self):
        ok, _msg, reward = self._claim()
        self.db.commit()
        self.assertTrue(ok)
        self.assertEqual(reward["coins"], 2500)
        self.assertEqual(self.user.total_coins, 2500)

    def test_three_taps_pay_exactly_once(self):
        paid = 0
        for _ in range(3):
            ok, _msg, _reward = self._claim()
            if ok:
                paid += 1
                self.db.commit()
        self.assertEqual(paid, 1)
        self.assertEqual(self.user.total_coins, 2500)
        self.assertEqual(self.user.total_gems, 3)

    def test_a_second_request_holding_a_stale_row_still_loses(self):
        """Second request loaded the progress row before the first committed."""
        from services.quest_service import claim_quest_reward
        other = self.Session()
        try:
            stale = (other.query(self.UserQuestProgress)
                     .filter(self.UserQuestProgress.user_id == self.user.id,
                             self.UserQuestProgress.quest_id == self.quest.id)
                     .first())
            self.assertFalse(stale.claimed, "loaded before the first claim")

            ok, _msg, _r = self._claim()
            self.assertTrue(ok)
            self.db.commit()

            ok_again, msg, _r2 = claim_quest_reward(
                other, self.user.id, self.quest.id)
            self.assertFalse(ok_again, "the compare-and-set must refuse it")
            self.assertEqual(msg, "Already claimed.")
            other.rollback()
        finally:
            other.close()

        self.db.expire_all()
        self.assertEqual(
            self.db.query(self.User).get(self.user.id).total_coins, 2500)

    def test_an_incomplete_quest_is_still_refused(self):
        self.progress.completed = False
        self.progress.claimed = False
        self.db.commit()
        ok, msg, _r = self._claim()
        self.assertFalse(ok)
        self.assertIn("Not yet complete", msg)
        self.assertEqual(self.user.total_coins or 0, 0)

    def test_claim_all_pays_each_quest_once(self):
        from services.quest_service import claim_all_completed
        count, total = claim_all_completed(self.db, self.user.id, "daily")
        self.db.commit()
        self.assertEqual(count, 1)
        self.assertEqual(total["coins"], 2500)

        count2, total2 = claim_all_completed(self.db, self.user.id, "daily")
        self.assertEqual(count2, 0, "nothing left to claim")
        self.assertEqual(total2["coins"], 0)
        self.assertEqual(self.user.total_coins, 2500)


# ════════════════════════════════════════════════════════════════════
# 3. Endpoints — guarded and locked like the pack shop
# ════════════════════════════════════════════════════════════════════

class WebappClaimEndpointTests(unittest.TestCase):
    # (route, guard key expression) for every reward-granting claim endpoint
    CLAIMS = [
        ("/api/webapp/daily", 'f"webapp_daily_{user.id}"'),
        ("/api/webapp/spin", 'f"webapp_spin_{user.id}"'),
        ("/api/webapp/login-streak/claim", 'f"webapp_loginclaim_{user.id}"'),
        ("/api/webapp/quests/claim", 'f"webapp_questclaim_{user.id}"'),
        ("/api/webapp/quests/claim_all", 'f"webapp_questclaim_{user.id}"'),
        ("/api/webapp/freepack/open", 'f"webapp_freepack_{user.id}"'),
        ("/api/webapp/redeem", 'f"webapp_redeem_{user.id}"'),
    ]

    @classmethod
    def setUpClass(cls):
        cls.src = _read("admin.py")

    def _endpoint(self, route):
        """Source of the view function registered for `route`."""
        start = self.src.index(f'@app.route("{route}", methods=["POST"])')
        nxt = self.src.find("\n@app.route(", start + 1)
        return self.src[start:nxt if nxt != -1 else len(self.src)]

    def test_every_claim_endpoint_takes_an_in_flight_guard(self):
        for route, key in self.CLAIMS:
            with self.subTest(route=route):
                body = self._endpoint(route)
                self.assertIn(f"guard_key = {key}", body)
                self.assertIn("if not claim_once(guard_key", body)

    def test_every_claim_endpoint_rejects_the_duplicate_with_409(self):
        for route, _key in self.CLAIMS:
            with self.subTest(route=route):
                body = self._endpoint(route)
                guard = body.index("if not claim_once(guard_key")
                tail = body[guard:guard + 400]
                self.assertIn('"in_progress"', tail)
                self.assertIn("409", tail)
                self.assertIn("db.close()", tail,
                              "a rejected request must not leak a session")

    def test_every_claim_endpoint_releases_its_guard(self):
        for route, _key in self.CLAIMS:
            with self.subTest(route=route):
                body = self._endpoint(route)
                self.assertRegex(body, r"finally:\s*\n\s*release\(guard_key\)")

    def test_quest_claims_share_one_key_with_claim_all(self):
        """Otherwise Claim all and a per-quest tap run over the same rows."""
        one = self._endpoint("/api/webapp/quests/claim")
        every = self._endpoint("/api/webapp/quests/claim_all")
        self.assertIn('f"webapp_questclaim_{user.id}"', one)
        self.assertIn('f"webapp_questclaim_{user.id}"', every)

    def test_claims_lock_their_deciding_row_before_deciding(self):
        for route, call in (
            ("/api/webapp/login-streak/claim", "_locked_stats(db, user.id)"),
            ("/api/webapp/freepack/open", "_locked_stats(db, user.id)"),
            ("/api/webapp/quests/claim", "_lock_user_row(db, user.id)"),
            ("/api/webapp/quests/claim_all", "_lock_user_row(db, user.id)"),
        ):
            with self.subTest(route=route):
                self.assertIn(call, self._endpoint(route))

    def test_the_user_row_lock_refreshes_what_it_locks(self):
        body = self.src[self.src.index("def _lock_user_row("):]
        body = body[:body.index("\ndef ", 1)]
        self.assertIn(".populate_existing()", body)
        self.assertIn(".with_for_update()", body)
        self.assertLess(body.index(".populate_existing()"),
                        body.index(".with_for_update()"))
        self.assertIn("db.flush()", body,
                      "autoflush is off — pending writes must be flushed first")

    def test_a_refused_quest_claim_rolls_back(self):
        """The claim flag is taken with an UPDATE that already hit the DB."""
        body = self._endpoint("/api/webapp/quests/claim")
        refusal = body.index('"claim_failed"')
        self.assertIn("db.rollback()", body[:refusal])


# ════════════════════════════════════════════════════════════════════
# 4. UI — the buttons lock themselves
# ════════════════════════════════════════════════════════════════════

class MiniAppClaimButtonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = _read("templates/webapp.html")

    def test_login_claim_drops_taps_already_in_flight(self):
        fn = _func(self.html, "async function claimLoginReward(")
        request = fn.index("/api/webapp/login-streak/claim")
        self.assertLess(fn.index("if (loginClaimBusy) return;"), request)
        self.assertLess(fn.index("loginClaimBusy = true"), request)
        self.assertLess(fn.index("btn.disabled = true"), request,
                        "the button must be dead before the round trip starts")

    def test_login_claim_re_arms_after_a_refusal(self):
        fn = _func(self.html, "async function claimLoginReward(")
        self.assertIn("loginClaimBusy = false", fn)
        self.assertIn("btn.disabled = false", fn)

    def test_quest_claims_share_one_busy_flag(self):
        one = _func(self.html, "async function claimQuest(")
        every = _func(self.html, "async function claimAllQuests(")
        for fn, route in ((one, "/api/webapp/quests/claim"),
                          (every, "/api/webapp/quests/claim_all")):
            with self.subTest(route=route):
                request = fn.index(route)
                self.assertLess(fn.index("if (questClaimBusy) return;"), request)
                self.assertLess(fn.index("setQuestButtonsBusy(true)"), request)

    def test_the_busy_flag_is_cleared_even_if_the_request_throws(self):
        for sig in ("async function claimLoginReward(",
                    "async function claimQuest(",
                    "async function claimAllQuests("):
            with self.subTest(fn=sig):
                fn = _func(self.html, sig)
                self.assertIn("finally {", fn,
                              "a thrown request must not wedge the button")


if __name__ == "__main__":
    unittest.main()

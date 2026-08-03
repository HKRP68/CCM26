"""Regression coverage for the Mini App "tap Daily twice = claim twice" bug.

Two POSTs to ``/api/webapp/daily`` that overlapped both passed the quota check
and both paid out coins, gems and player cards — from one free slot. Tapping
fast enough (a laggy WebView, a second tab, a replayed request) was a repeatable
way to farm the daily.

The three layers of the fix, one section each:

  1. Data — ``_locked_stats`` re-reads UserStats with ``populate_existing()``
     as well as ``with_for_update()``. This was the actual hole: the row lock
     did serialise the two requests, but SQLAlchemy answered the locked re-read
     out of the identity map without refreshing the attributes it already had,
     so the second request decided against pre-commit values and was granted
     the free slot a second time.
  2. Endpoint — ``claim_once``/``release`` allow one daily claim (and one spin)
     per user in flight; the duplicate gets a 409.
  3. UI — the claim button adopts the quota the server returned before anything
     re-enables it, instead of staying on the stale cached one until
     ``refreshDashboard()`` comes back.

Layers 2 and 3 are asserted against source text: the Mini App is a single Jinja
template with no JS test harness, and importing ``admin`` needs Flask, which the
lean test env doesn't have.
"""

import logging
import os
import re
import unittest

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


# ════════════════════════════════════════════════════════════════════
# 1. Data — the locked re-read has to actually re-read
# ════════════════════════════════════════════════════════════════════

class LockedStatsRefreshTests(unittest.TestCase):
    """`with_for_update()` alone hands back the stale in-memory row.

    Reproduced on an in-memory SQLite engine, where every session in the thread
    shares the one pooled connection — so the "other request" here commits to
    the same connection rather than a separate one. That is enough, because what
    made the lock useless is an ORM property (the identity map is not refreshed
    on a re-read) rather than anything about connections or dialects.
    """

    def setUp(self):
        from database import Base
        from models import User, UserStats
        self.UserStats = UserStats
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        setup = self.Session()
        u = User(telegram_id=2001, first_name="Farmer")
        setup.add(u)
        setup.flush()
        self.user_id = u.id
        setup.add(UserStats(user_id=u.id, daily_free_used=False))
        setup.commit()
        setup.close()

    def tearDown(self):
        self.engine.dispose()

    def _claim_free_slot_in_another_request(self):
        """A concurrent request takes the free daily and commits."""
        other = self.Session()
        row = (other.query(self.UserStats)
               .filter(self.UserStats.user_id == self.user_id).first())
        row.daily_free_used = True
        other.commit()
        other.close()

    def test_a_plain_locked_reread_is_stale(self):
        """Why the bug happened — kept as the counter-example."""
        db = self.Session()
        first = (db.query(self.UserStats)
                 .filter(self.UserStats.user_id == self.user_id).first())
        self.assertFalse(first.daily_free_used)

        self._claim_free_slot_in_another_request()

        relocked = (db.query(self.UserStats)
                    .filter(self.UserStats.user_id == self.user_id)
                    .with_for_update()
                    .first())
        self.assertIs(relocked, first, "same identity-map object")
        self.assertFalse(relocked.daily_free_used,
                         "without populate_existing the lock reads stale state")
        db.close()

    def test_populate_existing_sees_the_committed_claim(self):
        db = self.Session()
        db.query(self.UserStats).filter(
            self.UserStats.user_id == self.user_id).first()

        self._claim_free_slot_in_another_request()

        relocked = (db.query(self.UserStats)
                    .filter(self.UserStats.user_id == self.user_id)
                    .populate_existing()
                    .with_for_update()
                    .first())
        self.assertTrue(relocked.daily_free_used,
                        "the second claim must see the first one's commit")
        db.close()

    def test_the_helper_in_admin_uses_both(self):
        src = _read("admin.py")
        body = src[src.index("def _locked_stats("):]
        body = body[:body.index("\ndef ", 1)]
        self.assertIn(".populate_existing()", body)
        self.assertIn(".with_for_update()", body)
        self.assertLess(body.index(".populate_existing()"),
                        body.index(".with_for_update()"),
                        "populate_existing modifies the load, so it goes first")


# ════════════════════════════════════════════════════════════════════
# 2. Quota — one free slot per cycle, and it stays spent
# ════════════════════════════════════════════════════════════════════

class DailyQuotaSlotTests(unittest.TestCase):
    """The invariant the stale read was breaking, checked directly."""

    def setUp(self):
        from models import UserStats
        # quota_service falls back to defaults when it has no session to read
        # GameConfig from, and logs the lookup it couldn't make. That fallback
        # is the path under test; the traceback is just noise here.
        logging.getLogger("services.command_config_service").setLevel(
            logging.CRITICAL)
        self.stats = UserStats(user_id=1, daily_free_used=False,
                               daily_ad_count=0, daily_nofill_used=0)

    def test_free_slot_is_refused_once_consumed(self):
        from services import quota_service
        allowed, slot, _ = quota_service.can_use(self.stats, "daily", False)
        self.assertTrue(allowed)
        self.assertEqual(slot, "free")

        quota_service.consume_slot(self.stats, "daily", "free")

        allowed, slot, reason = quota_service.can_use(self.stats, "daily", False)
        self.assertFalse(allowed, "a second free claim must be refused")
        self.assertIsNone(slot)
        self.assertEqual(reason, "ad_required")

    def test_consuming_twice_does_not_hand_back_a_slot(self):
        from services import quota_service
        quota_service.consume_slot(self.stats, "daily", "free")
        quota_service.consume_slot(self.stats, "daily", "free")
        self.assertTrue(self.stats.daily_free_used)
        self.assertFalse(
            quota_service.get_quota_status(self.stats, "daily")["free_available"])


# ════════════════════════════════════════════════════════════════════
# 3. Endpoint — one claim per user in flight
# ════════════════════════════════════════════════════════════════════

class WebappClaimGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read("admin.py")

    def _endpoint(self, route):
        """Source of the view function registered for `route`.

        The slice runs to the next route decorator, or to the end of the file
        for the last route in it — `index` would raise there, and a ValueError
        out of the helper reads as a broken test rather than the assertion the
        caller was actually making.
        """
        start = self.src.index(f'@app.route("{route}", methods=["POST"])')
        nxt = self.src.find("\n@app.route(", start + 1)
        return self.src[start:nxt if nxt != -1 else len(self.src)]

    def test_daily_claims_the_guard_before_doing_any_work(self):
        body = self._endpoint("/api/webapp/daily")
        self.assertIn('guard_key = f"webapp_daily_{user.id}"', body)
        self.assertLess(body.index("claim_once(guard_key"),
                        body.index("claim_daily(db, user"),
                        "the guard must be taken before the reward is granted")

    def test_daily_rejects_the_duplicate_with_409_and_closes_the_session(self):
        body = self._endpoint("/api/webapp/daily")
        guard = body.index("if not claim_once(guard_key")
        tail = body[guard:guard + 400]
        self.assertIn("db.close()", tail, "the rejected request must not leak a session")
        self.assertIn('"in_progress"', tail)
        self.assertIn("409", tail)

    def test_daily_releases_the_guard_in_finally(self):
        body = self._endpoint("/api/webapp/daily")
        self.assertRegex(body, r"finally:\s*\n\s*release\(guard_key\)")

    def test_spin_is_guarded_the_same_way(self):
        body = self._endpoint("/api/webapp/spin")
        self.assertIn('guard_key = f"webapp_spin_{user.id}"', body)
        self.assertIn("if not claim_once(guard_key", body)
        self.assertRegex(body, r"finally:\s*\n\s*release\(guard_key\)")

    def test_guard_ttl_is_defined_and_bounded(self):
        m = re.search(r"_WEBAPP_CLAIM_GUARD_TTL = ([\d.]+)", self.src)
        self.assertIsNotNone(m, "the claim guard TTL must be named, not inline")
        self.assertLessEqual(float(m.group(1)), 30.0,
                             "a stuck claim button must not outlast a request")


# ════════════════════════════════════════════════════════════════════
# 4. UI — the button can't be re-enabled on a stale quota
# ════════════════════════════════════════════════════════════════════

class MiniAppDailyButtonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = _read("templates/webapp.html")
        cls.click = _func(cls.html, "async function onDailyClick(")

    def test_a_tap_while_claiming_is_dropped(self):
        guard = self.click.index("if (isDailyClaiming) return;")
        request = self.click.index("/api/webapp/daily")
        self.assertLess(guard, request)
        self.assertLess(self.click.index("isDailyClaiming = true"), request)

    def test_the_button_disables_itself_while_a_claim_is_in_flight(self):
        update = _func(self.html, "function updateDailyButton(")
        head = update[:update.index("lastInitData")]
        self.assertIn("if (isDailyClaiming) {", head)
        self.assertIn("btn.disabled = true;", head)

    def test_the_server_quota_is_adopted_before_the_button_comes_back(self):
        adopt = self.click.index("lastInitData.daily.quota = data.quota")
        response = self.click.index("await api('/api/webapp/daily'")
        self.assertLess(response, adopt)
        self.assertLess(adopt, self.click.index("updateDailyButton();", adopt),
                        "the cached quota must be replaced before a redraw")


if __name__ == "__main__":
    unittest.main()

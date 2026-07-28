"""Regression coverage for the Mini App "double-tapped Buy = multiple packs" bug.

Tapping Buy twice in the Telegram WebView fired two POSTs to
``/api/webapp/packs/buy`` before the first one had committed. Each request read
the same pre-deduction balance, passed the cost / inventory / daily-limit
checks, charged the user and inserted another ``UnopenedPack`` — so an impatient
tap cost real currency and handed out extra packs.

The fix locks at three levels, and each one is covered here:

  1. UI — the Buy/Open buttons disable themselves on the first click and a
     module-level ``packActionBusy`` flag drops clicks already queued.
  2. Endpoint — ``claim_once``/``release`` allow one pack buy (and one pack
     open) per user in flight, returning 409 for the duplicate.
  3. Data — ``buy_pack`` takes a ``SELECT … FOR UPDATE`` on the buyer before
     validating, so overlapping transactions can't spend the same balance.

The UI and endpoint layers are asserted against source text: the Mini App is a
single Jinja template with no JS test harness, and importing ``admin`` needs
Flask, which the lean test env doesn't have.
"""

import os
import re
import threading
import unittest

from utils.idempotency import claim_once, release, _GUARD

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


# ════════════════════════════════════════════════════════════════════
# 1. Mini App UI — the button locks itself
# ════════════════════════════════════════════════════════════════════

class MiniAppBuyButtonLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = _read("templates/webapp.html")
        cls.buy = cls._func(cls.html, "async function buyPack(")
        cls.open = cls._func(cls.html, "async function openPack(")

    @staticmethod
    def _func(src, signature):
        """Slice out one function body by brace matching from its signature."""
        start = src.index(signature)
        depth, i = 0, src.index("{", start)
        for j in range(i, len(src)):
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
                if depth == 0:
                    return src[start:j + 1]
        raise AssertionError(f"unterminated function: {signature}")

    def test_buy_button_passes_itself_to_the_handler(self):
        """Without the element the handler has nothing to disable."""
        self.assertIn('onclick="buyPack(${p.id}, this)"', self.html)
        self.assertIn('onclick="openPack(${inv.inventory_id}, this)"', self.html)

    def test_buy_drops_taps_that_arrive_while_a_purchase_is_in_flight(self):
        guard = self.buy.index("if (packActionBusy) return;")
        request = self.buy.index("/api/webapp/packs/buy")
        self.assertLess(guard, request,
                        "the busy check must run before the purchase request")
        self.assertLess(self.buy.index("packActionBusy = true"), request)

    def test_buy_disables_every_pack_button_before_the_request(self):
        self.assertLess(self.buy.index("setPackButtonsBusy(true)"),
                        self.buy.index("/api/webapp/packs/buy"),
                        "buttons must be locked before the round trip starts")

    def test_buy_re_enables_the_shop_after_a_failed_purchase(self):
        """A rejected buy (not enough coins) must leave a usable button."""
        failure = self.buy[self.buy.index("if (!ok)"):]
        self.assertIn("setPackButtonsBusy(false)", failure)
        self.assertIn("btn.innerHTML = label", failure)

    def test_buy_always_clears_the_busy_flag(self):
        self.assertIn("finally", self.buy)
        self.assertIn("packActionBusy = false", self.buy)

    def test_open_is_guarded_the_same_way(self):
        for fragment in ("if (packActionBusy) return;", "packActionBusy = true",
                         "setPackButtonsBusy(true)", "packActionBusy = false"):
            self.assertIn(fragment, self.open)

    def test_sold_out_buttons_stay_disabled_when_the_lock_lifts(self):
        """setPackButtonsBusy must not resurrect a daily-limited pack."""
        self.assertIn('data-locked="1"', self.html)
        helper = self._func(self.html, "function setPackButtonsBusy(")
        self.assertIn("b.dataset.locked === '1'", helper)

    def test_disabled_pack_buttons_swallow_taps(self):
        css = self.html[:self.html.index("</style>")]
        rule = re.search(r"\.pack-buy-btn:disabled,\s*\n\s*\.pack-open-btn:disabled\s*\{[^}]*\}",
                         css)
        self.assertIsNotNone(rule, "no shared disabled rule for the pack buttons")
        self.assertIn("pointer-events: none", rule.group(0))

    def test_reveal_animation_ignores_repeat_taps(self):
        reveal = self._func(self.html, "function revealPackCards(")
        self.assertIn("packRevealing", reveal)
        self.assertLess(reveal.index("packRevealing"),
                        reveal.index("classList.add('bursting')"))


# ════════════════════════════════════════════════════════════════════
# 2. Endpoint — one pack buy per user in flight
# ════════════════════════════════════════════════════════════════════

class WebappPackEndpointGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin_src = _read("admin.py")

    def _endpoint(self, name):
        start = self.admin_src.index(f"def {name}(")
        end = self.admin_src.index("\n@app.route", start)
        return self.admin_src[start:end]

    def test_buy_endpoint_claims_before_doing_any_work(self):
        src = self._endpoint("webapp_packs_buy")
        self.assertIn("claim_once(guard_key", src)
        self.assertLess(src.index("claim_once(guard_key"), src.index("buy_pack("),
                        "the duplicate must be rejected before the purchase runs")

    def test_buy_guard_is_per_user(self):
        src = self._endpoint("webapp_packs_buy")
        self.assertIn('f"webapp_packbuy_{user.id}"', src)

    def test_duplicate_buy_is_rejected_with_409_and_closes_the_session(self):
        src = self._endpoint("webapp_packs_buy")
        rejection = src[src.index("if not claim_once"):src.index("try:", src.index("if not claim_once"))]
        self.assertIn("409", rejection)
        self.assertIn('"in_progress"', rejection)
        self.assertIn("db.close()", rejection,
                      "the rejected request must not leak its DB session")

    def test_buy_releases_the_claim_in_finally(self):
        """Otherwise a legitimate second purchase is blocked for the whole TTL."""
        src = self._endpoint("webapp_packs_buy")
        tail = src[src.index("finally:"):]
        self.assertIn("release(guard_key)", tail)

    def test_open_endpoint_is_guarded_and_released_too(self):
        src = self._endpoint("webapp_packs_open")
        self.assertIn('f"webapp_packopen_{user.id}"', src)
        self.assertIn("409", src)
        self.assertIn("release(guard_key)", src[src.index("finally:"):])

    def test_guard_ttl_is_defined_and_bounded(self):
        match = re.search(r"_WEBAPP_PACK_GUARD_TTL\s*=\s*([\d.]+)", self.admin_src)
        self.assertIsNotNone(match, "guard TTL constant missing")
        self.assertLessEqual(float(match.group(1)), 60.0,
                             "a long TTL strands the user if a worker dies mid-buy")


class GuardSemanticsTests(unittest.TestCase):
    """The behaviour the endpoint relies on, exercised against the real guard."""

    def setUp(self):
        _GUARD.clear()

    def test_second_concurrent_buy_for_the_same_user_is_rejected(self):
        key = "webapp_packbuy_42"
        self.assertTrue(claim_once(key))
        self.assertFalse(claim_once(key))

    def test_a_different_user_is_never_blocked(self):
        self.assertTrue(claim_once("webapp_packbuy_42"))
        self.assertTrue(claim_once("webapp_packbuy_43"))

    def test_sequential_purchases_work_once_the_first_commits(self):
        key = "webapp_packbuy_42"
        self.assertTrue(claim_once(key))
        release(key)                       # request finished (finally block)
        self.assertTrue(claim_once(key))   # user buys a second pack — allowed

    def test_a_burst_of_taps_yields_exactly_one_purchase(self):
        """32 threads model the WebView firing a click storm at one endpoint."""
        purchases = []
        barrier = threading.Barrier(32)

        def request():
            barrier.wait()
            if claim_once("webapp_packbuy_42"):
                purchases.append(1)

        threads = [threading.Thread(target=request) for _ in range(32)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(purchases), 1)


# ════════════════════════════════════════════════════════════════════
# 3. Data layer — the buyer row is locked before the balance is trusted
# ════════════════════════════════════════════════════════════════════

try:
    import sqlalchemy  # noqa: F401
    _HAVE_SQLALCHEMY = True
except Exception:
    _HAVE_SQLALCHEMY = False


class BuyPackRowLockTests(unittest.TestCase):
    """Source-level: nothing may be validated before the lock is taken."""

    def test_lock_is_the_first_thing_buy_pack_does(self):
        src = _read("services/pack_service.py")
        body = src[src.index("def buy_pack("):src.index("def list_user_inventory(")]
        self.assertLess(body.index("_lock_buyer_row(session, user)"),
                        body.index("pack.cost_coins and user.total_coins"),
                        "the balance is checked before the buyer row is locked")

    def test_lock_helper_uses_for_update_and_refreshes_the_row(self):
        src = _read("services/pack_service.py")
        helper = src[src.index("def _lock_buyer_row("):src.index("def buy_pack(")]
        self.assertIn("with_for_update()", helper)
        self.assertIn("populate_existing()", helper,
                      "an identity-mapped User would keep the stale balance")
        self.assertIn("session.flush()", helper,
                      "autoflush is off — pending writes must be flushed first")

    @unittest.skipUnless(_HAVE_SQLALCHEMY, "sqlalchemy not installed")
    def test_lock_failure_does_not_break_buying(self):
        """A session that can't lock (test double, odd backend) still sells."""
        from services import pack_service

        class _Boom:
            def flush(self):
                raise RuntimeError("no locking here")

        pack_service._lock_buyer_row(_Boom(), type("U", (), {"id": 1})())


if __name__ == "__main__":
    unittest.main()

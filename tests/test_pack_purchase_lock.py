"""Mini App pack buttons and shared wallet serialization."""

import os
import re
import unittest


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
        self.assertIn('onclick="openPack(${inv.inventory_id})"', self.html)

    def test_buy_drops_taps_that_arrive_while_a_purchase_is_in_flight(self):
        guard = self.buy.index("if (packActionBusy) return;")
        request = self.buy.index("/api/webapp/economy/quote")
        self.assertLess(guard, request,
                        "the busy check must run before the purchase request")
        self.assertLess(self.buy.index("packActionBusy = true"), request)

    def test_buy_disables_every_pack_button_before_the_request(self):
        self.assertLess(self.buy.index("setPackButtonsBusy(true)"),
                        self.buy.index("/api/webapp/economy/quote"),
                        "buttons must be locked before the round trip starts")

    def test_buy_re_enables_the_shop_after_a_failed_purchase(self):
        """A rejected buy (not enough coins) must leave a usable button."""
        failure = self.buy[self.buy.index("if (!ok)"):]
        self.assertIn("setPackButtonsBusy(false)", failure)
        self.assertIn("finally", failure)

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
        self.assertLess(body.index("lock_user_for_update(session, user)"),
                        body.index("prices = {currency:"),
                        "the balance is checked before the buyer row is locked")

    def test_lock_helper_uses_for_update_and_refreshes_the_row(self):
        helper = _read("services/economy_transactions.py")
        self.assertIn("with_for_update()", helper)
        self.assertIn("populate_existing()", helper,
                      "an identity-mapped User would keep the stale balance")
        self.assertIn("session.flush()", helper,
                      "autoflush is off — pending writes must be flushed first")

    @unittest.skipUnless(_HAVE_SQLALCHEMY, "sqlalchemy not installed")
    def test_lock_failure_stops_buying(self):
        """A lock failure must occur before a purchase changes anything."""
        from services import pack_service

        class _Boom:
            def flush(self):
                raise RuntimeError("no locking here")

        with self.assertRaisesRegex(RuntimeError, "no locking here"):
            pack_service.buy_pack(_Boom(), type("U", (), {"id": 1})(), None)


if __name__ == "__main__":
    unittest.main()

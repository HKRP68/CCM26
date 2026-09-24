"""One watched ad is one reward, however many times the client asks.

The Mini App used to cap ``AdController.show()`` at twelve seconds. A rewarded
ad runs fifteen to thirty, so the cap fired *while the player was watching*: the
promise was abandoned, ``/api/webapp/ad-completed`` was never called, and
someone who had just sat through a full ad was told no ad was available. That
is the "ads run but the reward doesn't work" report.

The client fix has two halves, and both of them lean on a promise this module
has to keep:

  • **the late ad.** A ``show()`` that finishes after the client gave up still
    registers, because the player watched it. It must not become a *second* ad.
  • **the retry.** ``/ad-completed`` is now retried when the response is lost —
    the ad is already spent by then, so a dropped request would otherwise be a
    watched ad turning into nothing. A retry must bind to the same receipt.

So: repeated registrations inside the repeat window are one ad, a registration
that lands on top of the network's own postback is one ad, and neither can be
spent twice. The client is free to ask as often as it needs to.
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap  # noqa: E402  (sibling helper; see its docstring)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "services.ad_service")

_TG = 770_000_111_222      # comfortably past 2^31, like a real Telegram id


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


def tearDownModule():
    try:
        _ENGINE.dispose()
    except Exception:
        pass
    if _PREV_DATABASE_URL is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _PREV_DATABASE_URL
    _module_swap.restore(_SAVED_MODULES)
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


class AdCompletedRetryCase(unittest.TestCase):
    """A clean ledger for one telegram id per test."""

    def setUp(self):
        from database import get_session
        from services import ad_service
        from models import AdReward

        self.ads = ad_service
        self.session = get_session()
        self.tg = _TG + id(self) % 100_000
        (self.session.query(AdReward)
         .filter(AdReward.telegram_id == self.tg).delete())
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def rows(self):
        from models import AdReward
        return (self.session.query(AdReward)
                .filter(AdReward.telegram_id == self.tg)
                .order_by(AdReward.received_at, AdReward.id).all())


class RepeatedRegistrationTests(AdCompletedRetryCase):
    """The client may call /ad-completed more than once for one ad."""

    def test_a_retry_binds_to_the_same_receipt(self):
        first = self.ads.register_client_ad(self.session, self.tg)
        second = self.ads.register_client_ad(self.session, self.tg)
        third = self.ads.register_client_ad(self.session, self.tg)
        self.assertIsNotNone(first)
        self.assertEqual(second, first)
        self.assertEqual(third, first)
        self.assertEqual(len(self.rows()), 1,
                         "three retries for one ad wrote more than one ad")

    def test_the_player_is_owed_exactly_one_ad_after_the_retries(self):
        for _ in range(3):
            self.ads.register_client_ad(self.session, self.tg)
        self.assertEqual(self.ads.count_credits(self.session, self.tg), 1)

    def test_that_one_ad_can_only_be_spent_once(self):
        for _ in range(3):
            self.ads.register_client_ad(self.session, self.tg)
        self.assertTrue(self.ads.claim_credit(self.session, self.tg))
        self.assertFalse(self.ads.claim_credit(self.session, self.tg),
                         "one ad paid out twice")

    def test_a_token_from_the_retry_spends_the_same_row(self):
        # Each attempt mints its own token; whichever one reaches the reward
        # endpoint must spend the single receipt, and the others must then be
        # worthless — not a second free spin each.
        tokens = [self.ads.issue_client_token(self.tg, session=self.session)
                  for _ in range(3)]
        self.assertTrue(self.ads.consume_client_token(
            tokens[1], self.tg, session=self.session))
        for spent in (tokens[0], tokens[2]):
            self.assertFalse(
                self.ads.consume_client_token(spent, self.tg,
                                              session=self.session),
                "a second token paid for the same ad again")


class LateAdTests(AdCompletedRetryCase):
    """An ad that finishes after the client stopped waiting is still theirs."""

    def test_a_late_ad_is_banked_for_the_next_round(self):
        # This is the whole fix seen from the server: the round it was watched
        # for is over, so the ad has to survive to the next one.
        self.ads.register_client_ad(self.session, self.tg)
        self.assertEqual(self.ads.count_credits(self.session, self.tg), 1)
        self.assertTrue(self.ads.claim_credit(self.session, self.tg))

    def test_a_late_ad_does_not_double_up_with_its_own_postback(self):
        # Adsgram fires its server postback for the same ad. Whichever lands
        # first is the receipt; the other must not add a second one.
        self.ads.record_postback(self.session, self.tg, source_ip="203.0.113.7")
        self.ads.register_client_ad(self.session, self.tg)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.ads.count_credits(self.session, self.tg), 1)

    def test_the_postback_arriving_second_is_stored_already_spent(self):
        self.ads.register_client_ad(self.session, self.tg)
        self.ads.record_postback(self.session, self.tg, source_ip="203.0.113.7")
        self.assertEqual(len(self.rows()), 2, "the postback should still be logged")
        self.assertEqual(self.ads.count_credits(self.session, self.tg), 1,
                         "one ad was counted twice")


class SeparateAdsTests(AdCompletedRetryCase):
    """Two genuinely separate ads are still two rewards.

    The repeat window is there to collapse retries, not to swallow a real
    second ad — a player who watches two must be paid for two.
    """

    def test_two_ads_past_the_repeat_window_are_two_credits(self):
        from models import AdReward
        self.ads.register_client_ad(self.session, self.tg)
        # Age the first receipt past MIN_CLIENT_AD_INTERVAL_SECONDS rather than
        # sleeping through it.
        old = self.rows()[0]
        old.received_at = datetime.utcnow() - timedelta(
            seconds=self.ads.MIN_CLIENT_AD_INTERVAL_SECONDS + 5)
        self.session.commit()

        self.ads.register_client_ad(self.session, self.tg)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.ads.count_credits(self.session, self.tg), 2)
        self.assertTrue(self.ads.claim_credit(self.session, self.tg))
        self.assertTrue(self.ads.claim_credit(self.session, self.tg))
        self.assertFalse(self.ads.claim_credit(self.session, self.tg))


if __name__ == "__main__":
    unittest.main()

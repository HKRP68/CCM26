"""Adsgram configuration — the block id decides init(), never the SDK.

The Mini App gates spins, the daily claim and free packs behind a rewarded ad.
These tests pin the two things that keep a misconfiguration recoverable:

  • the SDK script URL is a constant the page always has, so no env value can
    take the SDK off the page — a block id that is empty or wrong falls back to
    mock mode, where every reward is still claimable, instead of an app whose
    ad buttons all fail with nothing to explain why;
  • the one-shot ad tokens survive being redeemed from two request threads.

No Flask, no database — this layer is env reading and an in-process token store.
"""

import os
import threading
import time
import unittest
from contextlib import contextmanager

from services import ad_service


@contextmanager
def ad_env(block_id=None):
    """Run with ADSGRAM_BLOCK_ID set to exactly this (or unset)."""
    saved = os.environ.get("ADSGRAM_BLOCK_ID")
    try:
        if block_id is None:
            os.environ.pop("ADSGRAM_BLOCK_ID", None)
        else:
            os.environ["ADSGRAM_BLOCK_ID"] = block_id
        yield
    finally:
        if saved is None:
            os.environ.pop("ADSGRAM_BLOCK_ID", None)
        else:
            os.environ["ADSGRAM_BLOCK_ID"] = saved


class ConfigTests(unittest.TestCase):

    def test_a_block_id_turns_real_ads_on(self):
        with ad_env("int-1234"):
            cfg = ad_service.client_config()
        self.assertTrue(cfg["configured"])
        self.assertEqual(cfg["block_id"], "int-1234")
        self.assertEqual(cfg["sdk_url"], ad_service.ADSGRAM_SDK_URL)

    def test_no_block_id_is_mock_mode(self):
        with ad_env(None):
            cfg = ad_service.client_config()
        self.assertFalse(cfg["configured"])
        self.assertIsNone(cfg["block_id"])

    def test_a_placeholder_is_not_a_block_id(self):
        for placeholder in ("", "none", "mock", "disabled", "off", "0"):
            with self.subTest(placeholder=placeholder):
                with ad_env(placeholder):
                    self.assertFalse(ad_service.is_configured())

    def test_the_sdk_url_never_depends_on_configuration(self):
        """The regression this file exists for.

        The SDK tag was briefly gated on "is a block id set", so one wrong env
        var removed the script from the page and every ad in the app failed
        with nothing in the config to suggest the SDK was simply missing. The
        URL is a constant: configuration decides whether we can init(), never
        whether the SDK is there to init.
        """
        for block_id in (None, "", "mock", "int-1234"):
            with self.subTest(block_id=block_id):
                with ad_env(block_id):
                    self.assertEqual(ad_service.client_config()["sdk_url"],
                                     ad_service.ADSGRAM_SDK_URL)

    def test_whitespace_around_a_block_id_is_forgiven(self):
        """Env vars pasted into a dashboard field pick up spaces."""
        with ad_env("  int-1234  "):
            self.assertEqual(ad_service.get_block_id(), "int-1234")


class TokenConcurrencyTests(unittest.TestCase):
    """A single-use token must survive being redeemed from two threads at once.

    Flask serves requests on threads, so two spin requests carrying the same
    token really do race. Validation passing is not what spends a token —
    removing it is — or both callers are told it was theirs and one ad pays out
    twice.
    """

    def test_only_one_of_two_racing_redemptions_wins(self):
        for _ in range(200):
            token = ad_service.issue_client_token(4242)
            results = []
            barrier = threading.Barrier(2)

            def redeem():
                barrier.wait()          # line both threads up on the same token
                results.append(ad_service.consume_client_token(token, 4242))

            threads = [threading.Thread(target=redeem) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(sorted(results), [False, True])

    def test_collecting_expired_tokens_survives_concurrent_issuing(self):
        """_gc_tokens used to scan the live dict — an insert mid-scan raises
        'dictionary changed size during iteration' on an unrelated request."""
        stop = threading.Event()
        errors = []

        def issue_forever():
            try:
                while not stop.is_set():
                    ad_service.issue_client_token(99)
            except Exception as exc:      # pragma: no cover - the bug this pins
                errors.append(exc)

        def collect_forever():
            try:
                while not stop.is_set():
                    ad_service._gc_tokens()
            except Exception as exc:      # pragma: no cover - the bug this pins
                errors.append(exc)

        workers = [threading.Thread(target=issue_forever),
                   threading.Thread(target=collect_forever)]
        for t in workers:
            t.start()
        time.sleep(0.3)
        stop.set()
        for t in workers:
            t.join()
        self.assertEqual(errors, [])


class LegacyImportTests(unittest.TestCase):
    """``services.adsgram_service`` is imported from a lot of call sites and
    from tests that reach into the token store; it must stay the same module,
    not a second copy with its own tokens."""

    def test_the_old_module_shares_one_token_store(self):
        from services import adsgram_service
        token = adsgram_service.issue_client_token(777)
        self.assertIn(token, ad_service._CLIENT_TOKENS)
        self.assertTrue(ad_service.consume_client_token(token, 777))


if __name__ == "__main__":
    unittest.main()

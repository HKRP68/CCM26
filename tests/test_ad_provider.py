"""Ad-network selection — switching provider must be an env var, not a deploy.

The Mini App gates spins, the daily claim and free packs behind a rewarded ad.
Which network serves it is a business decision that changes (rates change,
approvals lapse, a network stops filling a country), so the code has to treat
the provider as configuration. These tests pin the three things that makes
true:

  • the active provider is chosen by AD_PROVIDER, and auto-detected from
    whichever id is present when it isn't set — so an Adsgram deployment that
    predates the switch keeps working with no new env var;
  • ``client_config()`` hands the frontend the same shape for every provider,
    because the whole ad phase in webapp.html is written against that shape;
  • a half-configured provider reads as "not configured" (mock mode) rather
    than shipping a broken SDK tag to players.

No Flask, no database — the provider layer is pure env reading.
"""

import os
import threading
import time
import unittest
from contextlib import contextmanager

from services import ad_service


AD_ENV_VARS = ("AD_PROVIDER", "ADSGRAM_BLOCK_ID",
               "MONETAG_ZONE_ID", "MONETAG_SDK_URL", "MONETAG_SDK_FUNCTION",
               "MONETAG_SDK_ATTRS",
               "ONCLICKA_ZONE_ID", "ONCLICKA_SDK_URL", "ONCLICKA_SDK_FUNCTION",
               "ONCLICKA_SDK_ATTRS",
               "AD_ZONE_ID", "AD_SDK_URL", "AD_SDK_FUNCTION", "AD_SDK_ATTRS")


@contextmanager
def ad_env(**overrides):
    """Run with exactly the given ad env vars set and no others."""
    saved = {k: os.environ.get(k) for k in AD_ENV_VARS}
    try:
        for key in AD_ENV_VARS:
            os.environ.pop(key, None)
        for key, value in overrides.items():
            os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class ProviderSelectionTests(unittest.TestCase):

    def test_monetag_is_selected_and_fully_described(self):
        with ad_env(AD_PROVIDER="monetag", MONETAG_ZONE_ID="1234567"):
            cfg = ad_service.client_config()
        self.assertEqual(cfg["provider"], "monetag")
        self.assertTrue(cfg["configured"])
        self.assertEqual(cfg["zone_id"], "1234567")
        self.assertEqual(cfg["placement_id"], "1234567")
        # The SDK tag's data-sdk attribute and the global the page later calls
        # come from this one value, so they cannot drift apart.
        self.assertEqual(cfg["sdk_function"], "show_1234567")
        self.assertEqual(cfg["sdk_url"], ad_service.MONETAG_SDK_URL_DEFAULT)

    def test_monetag_sdk_host_is_overridable(self):
        """Monetag serves the SDK from a per-publisher host; a wrong one just
        fails to load and turns every ad into a no-fill."""
        with ad_env(AD_PROVIDER="monetag", MONETAG_ZONE_ID="42",
                    MONETAG_SDK_URL="https://example-cdn.test/sdk.js"):
            self.assertEqual(ad_service.get_sdk_url(),
                             "https://example-cdn.test/sdk.js")

    def test_adsgram_is_selected_and_fully_described(self):
        with ad_env(AD_PROVIDER="adsgram", ADSGRAM_BLOCK_ID="int-1234"):
            cfg = ad_service.client_config()
        self.assertEqual(cfg["provider"], "adsgram")
        self.assertTrue(cfg["configured"])
        self.assertEqual(cfg["block_id"], "int-1234")
        self.assertEqual(cfg["placement_id"], "int-1234")
        self.assertEqual(cfg["sdk_url"], ad_service.ADSGRAM_SDK_URL)
        self.assertIsNone(cfg["sdk_function"])   # Adsgram inits by object

    def test_an_existing_adsgram_deployment_needs_no_new_env_var(self):
        """AD_PROVIDER unset must not silently turn ads off on upgrade."""
        with ad_env(ADSGRAM_BLOCK_ID="int-1234"):
            self.assertEqual(ad_service.get_provider(), "adsgram")
            self.assertTrue(ad_service.is_configured())

    def test_setting_a_monetag_zone_is_enough_to_switch(self):
        with ad_env(MONETAG_ZONE_ID="1234567"):
            self.assertEqual(ad_service.get_provider(), "monetag")

    def test_an_explicit_provider_beats_auto_detection(self):
        """Both ids can be set at once during a switch — AD_PROVIDER decides,
        so the old block can stay in place as a one-env-var rollback."""
        with ad_env(AD_PROVIDER="adsgram", ADSGRAM_BLOCK_ID="int-1234",
                    MONETAG_ZONE_ID="1234567"):
            self.assertEqual(ad_service.get_provider(), "adsgram")
            self.assertEqual(ad_service.get_placement_id(), "int-1234")
            self.assertIsNone(ad_service.get_zone_id())

    def test_the_inactive_provider_leaks_nothing_to_the_client(self):
        with ad_env(AD_PROVIDER="monetag", ADSGRAM_BLOCK_ID="int-1234",
                    MONETAG_ZONE_ID="1234567"):
            cfg = ad_service.client_config()
        self.assertIsNone(cfg["block_id"])
        self.assertEqual(cfg["zone_id"], "1234567")


class GlobalFunctionProviderTests(unittest.TestCase):
    """Monetag, OnClicka and friends share one SDK shape.

    A `<script>` tag whose attributes name a zone, which then defines a global
    `show_<zone>()` returning a promise. The client branches on `sdk_kind`, not
    on the provider name, so a network of that shape is configuration — these
    tests pin that a provider we have never special-cased still comes out fully
    described.
    """

    def test_onclicka_is_described_like_any_other_tag_provider(self):
        with ad_env(AD_PROVIDER="onclicka", ONCLICKA_ZONE_ID="778899",
                    ONCLICKA_SDK_URL="https://cdn.onclicka.test/sdk.js"):
            cfg = ad_service.client_config()
        self.assertEqual(cfg["provider"], "onclicka")
        self.assertTrue(cfg["configured"])
        self.assertEqual(cfg["sdk_kind"], "global-function")
        self.assertEqual(cfg["zone_id"], "778899")
        self.assertEqual(cfg["sdk_url"], "https://cdn.onclicka.test/sdk.js")
        self.assertEqual(cfg["sdk_function"], "show_778899")
        self.assertEqual(cfg["sdk_attrs"],
                         {"data-zone": "778899", "data-sdk": "show_778899"})

    def test_an_unknown_network_needs_no_code_at_all(self):
        with ad_env(AD_PROVIDER="custom", AD_ZONE_ID="XY1",
                    AD_SDK_URL="https://ads.example.test/loader.js",
                    AD_SDK_FUNCTION="playRewarded",
                    AD_SDK_ATTRS="data-spot=XY1,data-callback=playRewarded"):
            cfg = ad_service.client_config()
        self.assertEqual(cfg["sdk_kind"], "global-function")
        self.assertEqual(cfg["sdk_function"], "playRewarded")
        self.assertEqual(cfg["sdk_attrs"],
                         {"data-spot": "XY1", "data-callback": "playRewarded"})

    def test_adsgram_keeps_its_own_shape(self):
        """Adsgram is the odd one out — an init() object, not a global."""
        with ad_env(AD_PROVIDER="adsgram", ADSGRAM_BLOCK_ID="int-1"):
            cfg = ad_service.client_config()
        self.assertEqual(cfg["sdk_kind"], "adsgram-object")
        self.assertIsNone(cfg["sdk_function"])
        self.assertEqual(cfg["sdk_attrs"], {})

    def test_a_zone_without_an_sdk_url_is_not_configured(self):
        """The id alone loads nothing. Rendering a tag with an empty src fails
        on every device while the admin page reports the network as live."""
        with ad_env(AD_PROVIDER="onclicka", ONCLICKA_ZONE_ID="778899"):
            self.assertFalse(ad_service.is_configured())
            self.assertFalse(ad_service.client_config()["configured"])

    def test_monetag_still_has_its_documented_default_host(self):
        with ad_env(AD_PROVIDER="monetag", MONETAG_ZONE_ID="1234567"):
            self.assertTrue(ad_service.is_configured())
            self.assertEqual(ad_service.get_sdk_url(),
                             ad_service.MONETAG_SDK_URL_DEFAULT)

    def test_a_junk_attribute_name_is_dropped_not_rendered(self):
        """Attr names go into a <script> tag unescaped, so they are constrained."""
        with ad_env(AD_PROVIDER="custom", AD_ZONE_ID="Z",
                    AD_SDK_URL="https://x.test/s.js",
                    AD_SDK_ATTRS='data-ok=1,on click=x,"><b>=2'):
            attrs = ad_service.get_sdk_attrs()
        self.assertEqual(attrs, {"data-ok": "1"})

    def test_a_new_zone_alone_switches_provider(self):
        with ad_env(ONCLICKA_ZONE_ID="778899",
                    ONCLICKA_SDK_URL="https://cdn.onclicka.test/sdk.js"):
            self.assertEqual(ad_service.get_provider(), "onclicka")


class MockModeTests(unittest.TestCase):

    def test_nothing_configured_is_mock_mode(self):
        with ad_env():
            cfg = ad_service.client_config()
        self.assertEqual(cfg["provider"], "none")
        self.assertFalse(cfg["configured"])
        self.assertIsNone(cfg["sdk_url"])
        self.assertIsNone(cfg["placement_id"])

    def test_ads_can_be_turned_off_while_ids_stay_set(self):
        with ad_env(AD_PROVIDER="none", MONETAG_ZONE_ID="1234567"):
            self.assertFalse(ad_service.is_configured())
            self.assertIsNone(ad_service.get_zone_id())

    def test_a_placeholder_id_is_not_a_configuration(self):
        """A half-filled env var must degrade to mock, not ship a broken tag."""
        for placeholder in ("", "none", "mock", "disabled"):
            with self.subTest(placeholder=placeholder):
                with ad_env(AD_PROVIDER="monetag",
                            MONETAG_ZONE_ID=placeholder):
                    self.assertFalse(ad_service.is_configured())
                with ad_env(AD_PROVIDER="adsgram",
                            ADSGRAM_BLOCK_ID=placeholder):
                    self.assertFalse(ad_service.is_configured())

    def test_a_typo_in_the_provider_falls_back_to_what_is_configured(self):
        """A misspelled AD_PROVIDER must not black out ads for everyone."""
        with ad_env(AD_PROVIDER="monetagg", MONETAG_ZONE_ID="1234567"):
            self.assertEqual(ad_service.get_provider(), "monetag")
            self.assertTrue(ad_service.is_configured())


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

    def test_the_old_module_reports_the_new_providers(self):
        from services import adsgram_service
        with ad_env(AD_PROVIDER="monetag", MONETAG_ZONE_ID="1234567"):
            self.assertTrue(adsgram_service.is_configured())
            self.assertEqual(adsgram_service.get_provider(), "monetag")
            # Adsgram's own accessor must go quiet when Adsgram isn't active,
            # or the Mini App would try to init a block that doesn't exist.
            self.assertIsNone(adsgram_service.get_block_id())


if __name__ == "__main__":
    unittest.main()

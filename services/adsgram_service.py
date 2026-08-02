"""Backwards-compatible alias for :mod:`services.ad_service`.

Rewarded ads used to be Adsgram-only, so this module name is referenced from a
lot of call sites (and from tests that reach into ``_CLIENT_TOKENS``). The
implementation now lives in ``services.ad_service`` and is provider-agnostic —
Adsgram, Monetag, or none — selected by the ``AD_PROVIDER`` env var.

Every name below is the *same object* as in ``ad_service``, not a copy, so the
in-memory token store is shared and either import path works identically.
New code should import ``services.ad_service`` directly.
"""

from services.ad_service import (  # noqa: F401
    ADSGRAM_SDK_URL,
    CLIENT_TOKEN_PREFIX,
    CLIENT_TOKEN_TTL,
    MONETAG_SDK_URL_DEFAULT,
    NOFILL_TOKEN_PREFIX,
    POSTBACK_WINDOW_SECONDS,
    PROVIDER_ADSGRAM,
    PROVIDER_MONETAG,
    PROVIDER_NONE,
    SUPPORTED_PROVIDERS,
    _CLIENT_TOKENS,
    _consume_token,
    _gc_tokens,
    _issue_token,
    claim_postback,
    client_config,
    consume_client_token,
    consume_nofill_token,
    get_block_id,
    get_placement_id,
    get_provider,
    get_sdk_function,
    get_sdk_url,
    get_zone_id,
    is_configured,
    issue_client_token,
    issue_nofill_token,
    record_postback,
)

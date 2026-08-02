"""Backwards-compatible alias for :mod:`services.ad_service`.

The implementation lives in ``services.ad_service``; this name is kept because
a lot of call sites (and tests that reach into ``_CLIENT_TOKENS``) import it.

Every name below is the *same object* as in ``ad_service``, not a copy, so the
in-memory token store is shared and either import path works identically.
New code should import ``services.ad_service`` directly.
"""

from services.ad_service import (  # noqa: F401
    ADSGRAM_SDK_URL,
    CLIENT_TOKEN_PREFIX,
    CLIENT_TOKEN_TTL,
    CREDIT_PROVIDER,
    CREDIT_TTL_SECONDS,
    NOFILL_TOKEN_PREFIX,
    POSTBACK_WINDOW_SECONDS,
    _CLIENT_TOKENS,
    _consume_token,
    _gc_tokens,
    _issue_token,
    bank_credit,
    claim_credit,
    claim_postback,
    count_credits,
    client_config,
    consume_client_token,
    consume_nofill_token,
    get_block_id,
    is_configured,
    issue_client_token,
    issue_nofill_token,
    record_postback,
)

"""Rewarded-ad integration, provider-agnostic.

The Mini App gates spins, the daily claim and free packs behind a rewarded ad.
Which network serves that ad is a deployment decision, not a code decision:
set ``AD_PROVIDER`` (plus that provider's id) and redeploy.

────────────────────────────────────────────────────────────────────────
Supported providers
────────────────────────────────────────────────────────────────────────
``adsgram``   ADSGRAM_BLOCK_ID=<block id>
              SDK: https://sad.adsgram.ai/js/sad.min.js
              Client: ``Adsgram.init({blockId}).show()`` — resolves ``{done:true}``
              Postback: GET /api/ads/reward?userid=[userId]

``monetag``   MONETAG_ZONE_ID=<zone id>
              MONETAG_SDK_URL=https://libtl.com/sdk.js   (the exact host is
              shown in your Monetag dashboard — copy it from the SDK tag there)
              Client: the SDK tag defines a global ``show_<zone>()`` which
              returns a promise that resolves once the ad has been watched.
              Postback: GET /api/ads/reward?ymid={ymid}&event_type={event_type}

``none``      no network configured → mock mode (dev only)

``AD_PROVIDER`` unset is auto-detected: Monetag if MONETAG_ZONE_ID is set,
else Adsgram if ADSGRAM_BLOCK_ID is set, else mock. So an existing Adsgram
deployment keeps working untouched, and switching is done purely by setting
MONETAG_ZONE_ID (+ optionally AD_PROVIDER=monetag to be explicit).

────────────────────────────────────────────────────────────────────────
How a reward is proven
────────────────────────────────────────────────────────────────────────
Every provider gives the publisher the same two signals, so the verification
logic below is deliberately provider-independent:

  1. CLIENT-SIDE: the SDK's show() promise resolves in the user's browser.
     The client then calls /api/webapp/ad-completed, which mints a one-shot,
     short-lived ``CT-`` token that the spin/daily endpoint spends.
  2. SERVER-SIDE (optional): the network fires a GET at our reward URL with
     the user's telegram id. We insert an ``AdReward`` row; the spin endpoint
     claims the most recent unconsumed one inside POSTBACK_WINDOW_SECONDS.

Server-side evidence is preferred when present, client-side is the fallback.
Both networks describe the S2S postback as optional for small publishers, so
the client path has to remain acceptable — the short TTL, single use and the
per-cycle ad quota are what keep it honest.

────────────────────────────────────────────────────────────────────────
No-fill passes
────────────────────────────────────────────────────────────────────────
An ad network is not a guarantee. Every network routinely answers "no banner"
for a request — no inventory for this user, this country, this minute — and a
Telegram WebView on a phone network drops the SDK often enough that a session
can spend its whole life with no ads at all. Neither is the player's fault, and
neither is a reason to lock them out of the feature the ad was gating: the old
flow left them tapping a button that spun a wheel for a minute and a half and
then said "no ad available", which reads as a broken app.

So when the client has genuinely exhausted its ad attempts, it asks for a
**no-fill pass** (``NF-`` token) instead. The pass spends an ad slot exactly as
a watched ad would, but it is separately capped per cycle
(``GameConfig.spin_nofill_grace``, default 2) so it can never become the cheap
route past every ad — a client that always claims no-fill still gets no more
free value than the grace allows, and a real ad remains the only way to use the
rest of the quota.
"""

import logging
import os
import secrets
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Window after the network fires its postback during which it can be claimed
POSTBACK_WINDOW_SECONDS = 300  # 5 minutes

# Client-side ad tokens are stored in-memory for replay protection.
# Map of token → (telegram_id, expires_at, scope). `scope` is None for a
# watched-ad token and the quota name ("spin"/"daily") for a no-fill pass.
# Cleared periodically.
_CLIENT_TOKENS = {}
CLIENT_TOKEN_TTL = 120  # 2 minutes for the user to claim after ad finishes

# Token prefixes. The prefix is what tells the spin endpoint which kind of
# evidence it is holding, so they must stay distinct.
CLIENT_TOKEN_PREFIX = "CT-"    # a real ad, watched to the end
NOFILL_TOKEN_PREFIX = "NF-"    # no ad was available to watch

PROVIDER_ADSGRAM = "adsgram"
PROVIDER_MONETAG = "monetag"
PROVIDER_NONE = "none"

SUPPORTED_PROVIDERS = (PROVIDER_ADSGRAM, PROVIDER_MONETAG)

ADSGRAM_SDK_URL = "https://sad.adsgram.ai/js/sad.min.js"
# Monetag serves the SDK from a per-publisher host; libtl.com is the current
# default in their docs. Always override with whatever the dashboard's SDK tag
# shows for your account — a wrong host just fails to load and every ad becomes
# a no-fill.
MONETAG_SDK_URL_DEFAULT = "https://libtl.com/sdk.js"

# Values that mean "not configured" rather than a real id.
_PLACEHOLDERS = ("", "none", "mock", "disabled", "off", "false", "0")


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _real(value: str) -> str:
    """Return ``value`` if it looks like a real id, else ''."""
    return value if value and value.lower() not in _PLACEHOLDERS else ""


def get_provider() -> str:
    """Which ad network is active: 'adsgram', 'monetag' or 'none'.

    ``AD_PROVIDER`` wins when set. Otherwise it is inferred from whichever id
    is present, so upgrading a running Adsgram deployment needs no new env var.
    """
    explicit = _env("AD_PROVIDER").lower()
    if explicit:
        if explicit in SUPPORTED_PROVIDERS:
            return explicit
        if explicit in _PLACEHOLDERS:
            return PROVIDER_NONE
        logger.warning("Unknown AD_PROVIDER=%r — falling back to auto-detect",
                       explicit)
    if _real(_env("MONETAG_ZONE_ID")):
        return PROVIDER_MONETAG
    if _real(_env("ADSGRAM_BLOCK_ID")):
        return PROVIDER_ADSGRAM
    return PROVIDER_NONE


def get_block_id() -> str | None:
    """Adsgram block ID for the client SDK, or None when Adsgram isn't active."""
    if get_provider() != PROVIDER_ADSGRAM:
        return None
    return _real(_env("ADSGRAM_BLOCK_ID")) or None


def get_zone_id() -> str | None:
    """Monetag zone ID for the client SDK, or None when Monetag isn't active."""
    if get_provider() != PROVIDER_MONETAG:
        return None
    return _real(_env("MONETAG_ZONE_ID")) or None


def get_placement_id() -> str | None:
    """The active provider's id, whatever it happens to be called there."""
    return get_block_id() or get_zone_id()


def is_configured() -> bool:
    """True when a real ad network is wired up. False means mock/dev mode."""
    return bool(get_placement_id())


def get_sdk_url() -> str | None:
    """URL of the client SDK for the active provider."""
    provider = get_provider()
    if provider == PROVIDER_ADSGRAM:
        return ADSGRAM_SDK_URL
    if provider == PROVIDER_MONETAG:
        return _env("MONETAG_SDK_URL") or MONETAG_SDK_URL_DEFAULT
    return None


def get_sdk_function() -> str | None:
    """Name of the global the Monetag SDK tag defines, e.g. ``show_1234567``.

    Monetag's tag names the function after the zone by default; ``data-sdk`` on
    the script tag is what actually sets it, and we emit both from this one
    value so they cannot drift apart.
    """
    zone = get_zone_id()
    return f"show_{zone}" if zone else None


def client_config() -> dict:
    """Everything the Mini App needs to load and drive the active network.

    Shape is stable across providers so the frontend has exactly one code path
    for "is there an ad network, and how do I call it".
    """
    provider = get_provider()
    return {
        "provider": provider,
        "configured": is_configured(),
        # Adsgram calls it a block, Monetag calls it a zone. Both are exposed
        # under their own name plus a neutral one.
        "block_id": get_block_id(),
        "zone_id": get_zone_id(),
        "placement_id": get_placement_id(),
        "sdk_url": get_sdk_url() if is_configured() else None,
        "sdk_function": get_sdk_function(),
    }


def record_postback(session, telegram_id: int, source_ip: str = None,
                    query_string: str = None, provider: str = None):
    """Insert a row when the ad network pings our reward URL.
    Called from /api/ads/reward (and its per-provider aliases)."""
    from models import AdReward
    row = AdReward(
        telegram_id=telegram_id,
        received_at=datetime.utcnow(),
        provider=(provider or get_provider())[:20],
        source_ip=(source_ip or "")[:64],
        query_string=(query_string or "")[:500],
    )
    session.add(row)
    session.commit()
    return row


def claim_postback(session, telegram_id: int) -> bool:
    """Find and consume an unclaimed ad postback for this user.

    Returns True if a recent (within POSTBACK_WINDOW_SECONDS) unconsumed
    postback exists and was just marked consumed; False otherwise.

    This is the high-confidence "yes, the ad was really watched" signal.
    Provider-agnostic on purpose: a deployment that switches networks
    mid-cycle must still honour postbacks the old one already delivered.
    """
    from models import AdReward
    cutoff = datetime.utcnow() - timedelta(seconds=POSTBACK_WINDOW_SECONDS)
    row = (session.query(AdReward)
           .filter(AdReward.telegram_id == telegram_id,
                   AdReward.consumed_at.is_(None),
                   AdReward.received_at >= cutoff)
           .order_by(AdReward.received_at.desc())
           .first())
    if not row:
        return False
    row.consumed_at = datetime.utcnow()
    session.commit()
    return True


def _issue_token(prefix: str, telegram_id: int, scope=None) -> str:
    """Mint a one-shot token for ``telegram_id``, valid for ``CLIENT_TOKEN_TTL``.

    ``scope`` is None for a watched-ad token and the quota name for a no-fill
    pass; ``_consume_token`` requires an exact match, so the two can never be
    spent on each other.
    """
    _gc_tokens()
    token = prefix + secrets.token_urlsafe(16)
    _CLIENT_TOKENS[token] = (telegram_id, time.time() + CLIENT_TOKEN_TTL, scope)
    return token


def _consume_token(prefix: str, token: str, telegram_id: int, scope=None) -> bool:
    """Validate a one-shot token and, only if it is good, spend it.

    Read before pop, deliberately. Popping first would mean a rejected call
    destroyed a token that is still valid for its real owner and scope — and
    for a no-fill pass that silently burns the grace, which was debited when
    the token was issued. An expired record is still dropped, since it is dead
    either way.
    """
    _gc_tokens()
    if not token or not token.startswith(prefix):
        return False
    record = _CLIENT_TOKENS.get(token)
    if not record:
        return False
    tg_id, expires, token_scope = record
    if tg_id != telegram_id:
        return False
    if time.time() > expires:
        _CLIENT_TOKENS.pop(token, None)
        return False
    # A scoped token is only good for the feature it was issued for, so a pass
    # bought out of the spin grace can't be spent on a different reward.
    if token_scope != scope:
        return False
    _CLIENT_TOKENS.pop(token, None)
    return True


def issue_client_token(telegram_id: int) -> str:
    """Generate a single-use token after client-side ad completion.

    The frontend asks for one of these AFTER the ad SDK's promise resolves
    successfully; then includes it in the spin request. We can't fully trust
    this (it's frontend-issued) but combined with the ad quota and cooldown
    it's adequate — and it is the only signal available at all until a
    publisher is big enough for the networks to bother with S2S postbacks.
    """
    return _issue_token(CLIENT_TOKEN_PREFIX, telegram_id)


def consume_client_token(token: str, telegram_id: int) -> bool:
    """Check + remove a client-side ad token. Returns True if valid for user."""
    return _consume_token(CLIENT_TOKEN_PREFIX, token, telegram_id)


def issue_nofill_token(telegram_id: int, kind: str) -> str:
    """Single-use token standing in for an ad that the network never served.

    Issued only by ``/api/webapp/ad-unavailable``, and only after that endpoint
    has already debited the user's no-fill grace for the cycle — so the token is
    proof a pass was paid for, not merely that the client asked.

    ``kind`` ("spin"/"daily") is the quota the pass was debited from, and the
    token is only redeemable against that same quota.
    """
    return _issue_token(NOFILL_TOKEN_PREFIX, telegram_id, scope=kind)


def consume_nofill_token(token: str, telegram_id: int, kind: str) -> bool:
    """Check + remove a no-fill pass token. Returns True if valid for user."""
    return _consume_token(NOFILL_TOKEN_PREFIX, token, telegram_id, scope=kind)


def _gc_tokens():
    """Drop expired client tokens to keep the dict bounded."""
    now = time.time()
    expired = [t for t, (_tg, exp, _scope) in _CLIENT_TOKENS.items()
               if exp < now]
    for t in expired:
        _CLIENT_TOKENS.pop(t, None)

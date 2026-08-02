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
import re
import secrets
import threading
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Window after the network fires its postback during which it can be claimed
POSTBACK_WINDOW_SECONDS = 300  # 5 minutes

# Client-side ad tokens are stored in-memory for replay protection.
# Map of token → (telegram_id, expires_at, scope). `scope` is None for a
# watched-ad token and the quota name ("spin"/"daily") for a no-fill pass.
# Cleared periodically.
#
# Guarded by _TOKENS_LOCK. Individual dict operations are atomic under CPython,
# but "single use" needs read-validate-remove to be one step: two threads that
# both read a valid record and then both pop it would each be told the token was
# theirs to spend, which is a duplicated reward. An RLock, because _gc_tokens
# takes it too and both public entry points call that first.
_CLIENT_TOKENS = {}
_TOKENS_LOCK = threading.RLock()
CLIENT_TOKEN_TTL = 120  # 2 minutes for the user to claim after ad finishes

# Token prefixes. The prefix is what tells the spin endpoint which kind of
# evidence it is holding, so they must stay distinct.
CLIENT_TOKEN_PREFIX = "CT-"    # a real ad, watched to the end
NOFILL_TOKEN_PREFIX = "NF-"    # no ad was available to watch

PROVIDER_ADSGRAM = "adsgram"
PROVIDER_MONETAG = "monetag"
PROVIDER_ONCLICKA = "onclicka"
PROVIDER_CUSTOM = "custom"
PROVIDER_NONE = "none"

SUPPORTED_PROVIDERS = (PROVIDER_ADSGRAM, PROVIDER_MONETAG, PROVIDER_ONCLICKA,
                       PROVIDER_CUSTOM)

# How the client drives the SDK once it has loaded. There are only two shapes
# in this market, and the frontend branches on this rather than on the provider
# name — which is what makes a new network config-only.
SDK_KIND_ADSGRAM_OBJECT = "adsgram-object"    # Adsgram.init({blockId}).show()
SDK_KIND_GLOBAL_FUNCTION = "global-function"  # tag defines show_<id>(), returns a promise

ADSGRAM_SDK_URL = "https://sad.adsgram.ai/js/sad.min.js"
# Monetag serves the SDK from a per-publisher host; libtl.com is the current
# default in their docs. Always override with whatever the dashboard's SDK tag
# shows for your account — a wrong host just fails to load and every ad becomes
# a no-fill.
MONETAG_SDK_URL_DEFAULT = "https://libtl.com/sdk.js"

# Providers that follow the "script tag defines a global function" pattern,
# mapped to the env prefix their settings live under. They share one client code
# path; only the ids and the SDK host differ. Adding another network to this
# dict is the whole integration — there is no per-network JavaScript.
GLOBAL_FUNCTION_PROVIDERS = {
    PROVIDER_MONETAG: "MONETAG",
    PROVIDER_ONCLICKA: "ONCLICKA",
    # The escape hatch: any network using this pattern, with no code change at
    # all. Paste its SDK tag's URL, id and attributes into AD_SDK_* env vars.
    PROVIDER_CUSTOM: "AD",
}

# Default SDK host per provider. Empty means "no sane default — copy the exact
# URL out of your dashboard's SDK tag", which is the honest answer for any
# network that serves the SDK from a per-publisher host.
_DEFAULT_SDK_URLS = {
    PROVIDER_MONETAG: MONETAG_SDK_URL_DEFAULT,
    PROVIDER_ONCLICKA: "",
    PROVIDER_CUSTOM: "",
}

# Attribute names are written into a <script> tag, so keep them to the shape a
# real data-attribute has. Values are escaped at render time; names are not, so
# they are the ones that must be constrained.
_ATTR_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9\-]*$")

# Values that mean "not configured" rather than a real id.
_PLACEHOLDERS = ("", "none", "mock", "disabled", "off", "false", "0")


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _real(value: str) -> str:
    """Return ``value`` if it looks like a real id, else ''."""
    return value if value and value.lower() not in _PLACEHOLDERS else ""


def _provider_env(provider: str, suffix: str) -> str:
    """Read ``<PREFIX>_<suffix>`` for a global-function provider."""
    prefix = GLOBAL_FUNCTION_PROVIDERS.get(provider)
    return _env(f"{prefix}_{suffix}") if prefix else ""


def _provider_zone(provider: str) -> str:
    """The configured placement id for a global-function provider, or ''."""
    return _real(_provider_env(provider, "ZONE_ID"))


def get_provider() -> str:
    """Which ad network is active: adsgram, monetag, onclicka, custom or none.

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
    for provider in GLOBAL_FUNCTION_PROVIDERS:
        if _provider_zone(provider):
            return provider
    if _real(_env("ADSGRAM_BLOCK_ID")):
        return PROVIDER_ADSGRAM
    return PROVIDER_NONE


def get_sdk_kind() -> str | None:
    """How the client should drive the active SDK, or None in mock mode."""
    provider = get_provider()
    if provider == PROVIDER_ADSGRAM:
        return SDK_KIND_ADSGRAM_OBJECT
    if provider in GLOBAL_FUNCTION_PROVIDERS:
        return SDK_KIND_GLOBAL_FUNCTION
    return None


def get_block_id() -> str | None:
    """Adsgram block ID for the client SDK, or None when Adsgram isn't active."""
    if get_provider() != PROVIDER_ADSGRAM:
        return None
    return _real(_env("ADSGRAM_BLOCK_ID")) or None


def get_zone_id() -> str | None:
    """Zone/spot id of the active global-function provider, or None.

    Called a zone by Monetag and a spot by OnClicka; same thing, same env
    suffix (``<PROVIDER>_ZONE_ID``) so the code needs one name for it.
    """
    return _provider_zone(get_provider()) or None


def get_placement_id() -> str | None:
    """The active provider's id, whatever it happens to be called there."""
    return get_block_id() or get_zone_id()


def is_configured() -> bool:
    """True when a real ad network is wired up. False means mock/dev mode.

    A global-function provider also needs its SDK URL: the id alone can't load
    anything, and shipping a tag with an empty src would fail on every device
    while the admin page cheerfully reported the network as live.
    """
    if not get_placement_id():
        return False
    if get_sdk_kind() == SDK_KIND_GLOBAL_FUNCTION and not get_sdk_url():
        logger.warning("%s is configured but its SDK URL is empty — copy it "
                       "from the dashboard's SDK tag", get_provider())
        return False
    return True


def get_sdk_url() -> str | None:
    """URL of the client SDK for the active provider."""
    provider = get_provider()
    if provider == PROVIDER_ADSGRAM:
        return ADSGRAM_SDK_URL
    if provider in GLOBAL_FUNCTION_PROVIDERS:
        return (_provider_env(provider, "SDK_URL")
                or _DEFAULT_SDK_URLS.get(provider) or None)
    return None


def get_sdk_function() -> str | None:
    """Name of the global the SDK tag defines, e.g. ``show_1234567``.

    These networks name the function after the zone by default, and the tag's
    ``data-sdk`` attribute is what actually sets it — so both come from this one
    value and cannot drift apart. Override with ``<PROVIDER>_SDK_FUNCTION`` when
    a network names it something else.
    """
    provider = get_provider()
    if provider not in GLOBAL_FUNCTION_PROVIDERS:
        return None
    zone = _provider_zone(provider)
    if not zone:
        return None
    return _provider_env(provider, "SDK_FUNCTION") or f"show_{zone}"


def get_sdk_attrs() -> dict:
    """Attributes to put on the SDK ``<script>`` tag.

    Defaults to the ``data-zone`` / ``data-sdk`` pair Monetag documents, which
    OnClicka and the other copies of that SDK also use. Override wholesale with
    ``<PROVIDER>_SDK_ATTRS`` in ``name=value,name=value`` form when a network
    wants different ones — that is what makes a new network config-only.
    """
    provider = get_provider()
    if provider not in GLOBAL_FUNCTION_PROVIDERS:
        return {}
    zone = _provider_zone(provider)
    fn = get_sdk_function()
    if not zone:
        return {}
    raw = _provider_env(provider, "SDK_ATTRS")
    if not raw:
        return {"data-zone": zone, "data-sdk": fn}
    attrs = {}
    for pair in raw.split(","):
        name, _, value = pair.partition("=")
        name, value = name.strip(), value.strip()
        if not name:
            continue
        if not _ATTR_NAME_RE.match(name):
            logger.warning("Ignoring bad SDK attribute name %r in %s_SDK_ATTRS",
                           name, GLOBAL_FUNCTION_PROVIDERS[provider])
            continue
        attrs[name] = value
    return attrs or {"data-zone": zone, "data-sdk": fn}


def client_config() -> dict:
    """Everything the Mini App needs to load and drive the active network.

    Shape is stable across providers so the frontend has exactly one code path
    for "is there an ad network, and how do I call it".
    """
    provider = get_provider()
    configured = is_configured()
    return {
        "provider": provider,
        "configured": configured,
        # How to drive it. The client branches on this, not on `provider`, so
        # a new network of a known shape needs no new client code.
        "sdk_kind": get_sdk_kind(),
        # Adsgram calls it a block, the rest call it a zone or a spot. Both are
        # exposed under their own name plus a neutral one.
        "block_id": get_block_id(),
        "zone_id": get_zone_id(),
        "placement_id": get_placement_id(),
        "sdk_url": get_sdk_url() if configured else None,
        "sdk_function": get_sdk_function(),
        # Attributes the tag needs to configure itself, e.g. data-zone/data-sdk.
        "sdk_attrs": get_sdk_attrs() if configured else {},
    }


def record_postback(session, telegram_id: int, source_ip: str | None = None,
                    query_string: str | None = None,
                    provider: str | None = None):
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

    Consuming is a conditional UPDATE, not a read followed by a write. Two
    concurrent spins can select the same unconsumed row and both see
    ``consumed_at IS NULL``; the ``consumed_at.is_(None)`` in the UPDATE's own
    WHERE is what makes exactly one of them affect a row, and the caller whose
    rowcount is 0 simply falls through to the client-token path rather than
    being granted a second reward for one ad. Works the same on Postgres and
    SQLite, unlike SELECT … FOR UPDATE.
    """
    from models import AdReward
    now = datetime.utcnow()
    cutoff = now - timedelta(seconds=POSTBACK_WINDOW_SECONDS)
    row = (session.query(AdReward)
           .filter(AdReward.telegram_id == telegram_id,
                   AdReward.consumed_at.is_(None),
                   AdReward.received_at >= cutoff)
           .order_by(AdReward.received_at.desc())
           .first())
    if not row:
        return False
    claimed = (session.query(AdReward)
               .filter(AdReward.id == row.id,
                       AdReward.consumed_at.is_(None))
               .update({AdReward.consumed_at: now},
                       synchronize_session=False))
    session.commit()
    return bool(claimed)


def _issue_token(prefix: str, telegram_id: int, scope=None) -> str:
    """Mint a one-shot token for ``telegram_id``, valid for ``CLIENT_TOKEN_TTL``.

    ``scope`` is None for a watched-ad token and the quota name for a no-fill
    pass; ``_consume_token`` requires an exact match, so the two can never be
    spent on each other.
    """
    _gc_tokens()
    token = prefix + secrets.token_urlsafe(16)
    with _TOKENS_LOCK:
        _CLIENT_TOKENS[token] = (telegram_id, time.time() + CLIENT_TOKEN_TTL,
                                 scope)
    return token


def _consume_token(prefix: str, token: str, telegram_id: int, scope=None) -> bool:
    """Validate a one-shot token and, only if it is good, spend it.

    Read before pop, deliberately. Popping first would mean a rejected call
    destroyed a token that is still valid for its real owner and scope — and
    for a no-fill pass that silently burns the grace, which was debited when
    the token was issued. An expired record is still dropped, since it is dead
    either way.

    The whole read-validate-remove runs under _TOKENS_LOCK, and success is the
    pop returning a record rather than the validation passing: a token is spent
    by exactly the caller that removed it, so two concurrent redemptions of the
    same token can never both be granted.
    """
    _gc_tokens()
    if not token or not token.startswith(prefix):
        return False
    with _TOKENS_LOCK:
        record = _CLIENT_TOKENS.get(token)
        if not record:
            return False
        tg_id, expires, token_scope = record
        if tg_id != telegram_id:
            return False
        if time.time() > expires:
            _CLIENT_TOKENS.pop(token, None)
            return False
        # A scoped token is only good for the feature it was issued for, so a
        # pass bought out of the spin grace can't be spent on another reward.
        if token_scope != scope:
            return False
        return _CLIENT_TOKENS.pop(token, None) is not None


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
    """Drop expired client tokens to keep the dict bounded.

    Under the lock: scanning the live dict while another request thread is
    issuing a token raises "dictionary changed size during iteration", which
    would surface as a failed spin on an unrelated user's request.
    """
    now = time.time()
    with _TOKENS_LOCK:
        expired = [t for t, (_tg, exp, _scope) in _CLIENT_TOKENS.items()
                   if exp < now]
        for t in expired:
            _CLIENT_TOKENS.pop(t, None)

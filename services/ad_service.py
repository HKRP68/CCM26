"""Adsgram rewarded-ad integration.

The Mini App gates spins, the daily claim and free packs behind a rewarded ad.
Adsgram serves them; ``ADSGRAM_BLOCK_ID`` turns it on and its absence means
mock mode (dev only).

────────────────────────────────────────────────────────────────────────
How a reward is proven
────────────────────────────────────────────────────────────────────────
Adsgram gives the publisher two signals:

  1. CLIENT-SIDE: the SDK's ``AdController.show()`` promise resolves with
     ``done: true`` in the user's browser. The client then calls
     /api/webapp/ad-completed, which mints a one-shot, short-lived ``CT-``
     token that the spin/daily endpoint spends.
  2. SERVER-SIDE (optional): Adsgram fires a GET at our reward URL with the
     user's telegram id. We insert an ``AdReward`` row; the spin endpoint
     claims the most recent unconsumed one inside POSTBACK_WINDOW_SECONDS.

Server-side evidence is preferred when present, client-side is the fallback.
Adsgram describes the reward URL as worth it "for publishers who have more than
50k daily users", so the client path has to remain acceptable — the short TTL,
single use and the per-cycle ad quota are what keep it honest.

────────────────────────────────────────────────────────────────────────
No-fill passes
────────────────────────────────────────────────────────────────────────
An ad network is not a guarantee. Adsgram routinely answers "no banner" for a
request — no inventory for this user, this country, this minute — and a Telegram
WebView on a phone network drops the SDK often enough that a session can spend
its whole life with no ads at all. Neither is the player's fault, and neither is
a reason to lock them out of the feature the ad was gating: the old flow left
them tapping a button that spun a wheel for a minute and a half and then said
"no ad available", which reads as a broken app.

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
import threading
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Window after Adsgram fires its postback during which it can be claimed
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

ADSGRAM_SDK_URL = "https://sad.adsgram.ai/js/sad.min.js"

# Values that mean "not configured" rather than a real block id.
_PLACEHOLDERS = ("", "none", "mock", "disabled", "off", "false", "0")


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def get_block_id() -> str | None:
    """Adsgram block ID for the client SDK, or None for mock mode."""
    value = _env("ADSGRAM_BLOCK_ID")
    return value if value and value.lower() not in _PLACEHOLDERS else None


def is_configured() -> bool:
    """True when ADSGRAM_BLOCK_ID is set (real ads active).
    False means mock/dev mode."""
    return bool(get_block_id())


def client_config() -> dict:
    """What the Mini App needs to drive the SDK.

    The SDK script itself is loaded unconditionally by the page — it is a fixed
    URL and costs nothing when unused. Only whether we have a block id to
    ``init()`` with depends on configuration, which is the one thing here.
    Gating the script tag on this instead was a mistake worth not repeating: a
    single wrong env var then removed the SDK from the page entirely, and every
    ad in the app failed with nothing in the config to suggest why.
    """
    return {
        "configured": is_configured(),
        "block_id": get_block_id(),
        "sdk_url": ADSGRAM_SDK_URL,
    }


def record_postback(session, telegram_id: int, source_ip: str | None = None,
                    query_string: str | None = None):
    """Insert a row when Adsgram pings the reward URL.
    Called from /api/ads/reward."""
    from models import AdReward
    row = AdReward(
        telegram_id=telegram_id,
        received_at=datetime.utcnow(),
        provider="adsgram",
        source_ip=(source_ip or "")[:64],
        query_string=(query_string or "")[:500],
    )
    session.add(row)
    session.commit()
    return row


def claim_postback(session, telegram_id: int) -> bool:
    """Find and consume an unclaimed Adsgram postback for this user.

    Returns True if a recent (within POSTBACK_WINDOW_SECONDS) unconsumed
    postback exists and was just marked consumed; False otherwise.

    This is the high-confidence "yes, the ad was really watched" signal.

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

    The frontend asks for one of these AFTER the Adsgram SDK promise resolves
    successfully; then includes it in the spin request. We can't fully trust
    this (it's frontend-issued) but combined with the ad quota and cooldown
    it's adequate per Adsgram's own guidance for small apps.
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

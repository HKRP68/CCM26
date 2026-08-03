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

────────────────────────────────────────────────────────────────────────
Ad credits — a watched ad is never lost
────────────────────────────────────────────────────────────────────────
Proving the ad and spending it are two steps, and everything between them can
fail: the reward request times out on a phone network, the quota cycle turned
over, the gap between rewarded ads had not elapsed, the reward table came back
empty. Every one of those used to end the same way — the player watched a full
ad and got nothing, which is the complaint this module now has to make
impossible.

So whenever ad evidence has been consumed but the reward could not be handed
over, the endpoint calls ``bank_credit``: an ``AdReward`` row tagged
``provider="credit"``. It is exactly the receipt a postback is, minus the
5-minute window — the player redeems it on their next spin with no second ad.
``claim_credit`` spends one, and the balance is reported to the client so the
app can say "1 ad saved" instead of leaving it invisible.

``claim_credit`` redeems *any* unconsumed row, network postbacks included.
Postbacks have their own five-minute claim window, and one that goes unspent
inside it is still an ad the player watched; leaving it outside the credit
query meant it simply expired, unreachable by every path.
"""

import logging
import os
import secrets
import threading
import time
from collections import namedtuple
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# What an outstanding one-shot token stands for. ``receipt_id`` is the AdReward
# row the token addresses (None for a no-fill pass, which is not an ad).
TokenRecord = namedtuple("TokenRecord",
                         "telegram_id expires_at scope receipt_id")

# Window after Adsgram fires its postback during which it can be claimed
POSTBACK_WINDOW_SECONDS = 300  # 5 minutes

# Banked ad credits live in the same table, tagged with this provider so the
# postback query can tell them apart — a credit is not a network postback and
# must not be claimed under the postback's short window.
CREDIT_PROVIDER = "credit"
# The client's receipt for one specific watched ad, written by
# ``register_client_ad``. Kept distinct from CREDIT_PROVIDER, which also covers
# ads banked back after a refused reward: only a *receipt* is a candidate for
# reconciling against the network postback for that same ad, and treating any
# banked credit as one silently voided the postback for a genuinely new ad.
CLIENT_PROVIDER = "client"
# The two providers that are NOT network postbacks. Used to separate the two
# kinds of evidence where the distinction matters — ``claim_postback`` must not
# pick up a saved ad under its five-minute window. It is not the set of what
# can be redeemed: every unconsumed row is an ad someone watched, and
# ``claim_credit`` spends any of them.
REDEEMABLE_PROVIDERS = (CREDIT_PROVIDER, CLIENT_PROVIDER)
# How long after a client receipt the matching network postback is still the
# same ad. The two fire within seconds of each other; the full postback window
# is five minutes, which is long enough to swallow a second, real ad.
POSTBACK_DEDUPE_SECONDS = 120
# How long a banked credit stays redeemable. Long, because the whole point is
# that the player keeps what they watched: a gap of an hour, a cycle that
# resets tomorrow, or simply closing the app must all still pay out.
CREDIT_TTL_SECONDS = 7 * 24 * 3600  # 7 days

# Limits on the client-side registration of a watched ad — see
# ``register_client_ad``. The floor is well under the length of any rewarded
# ad, so it can only catch repeats; the ceiling is above what a player can
# genuinely be owed (the quota and the gap cap how fast credits are earned in
# the first place) while keeping a spammed client from hoarding them.
MIN_CLIENT_AD_INTERVAL_SECONDS = 10
MAX_OUTSTANDING_CLIENT_ADS = 3

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

# Serialises the check-then-insert in ``register_client_ad``. Two /ad-completed
# requests for one user that race inside a few milliseconds — a double tap, or
# a client retry overlapping the original — would otherwise both read "no
# recent receipt" and both insert, turning one watched ad into two saved ones.
# A process-local lock is enough here (the app serves the Mini App from a
# single threaded Flask process); the 10-second repeat window is the backstop
# if that ever stops being true.
_REGISTER_LOCK = threading.Lock()
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
    Called from /api/ads/reward.

    One ad is one reward, and both halves of Adsgram's contract fire for the
    same ad: the SDK resolves in the browser (which registers a receipt — see
    ``register_client_ad``) and the network calls this URL. Whichever lands
    first is the receipt; a postback that follows the client's receipt for that
    same ad is stored already consumed. It stays in the table for debugging,
    but it can't be spent — banked on top of the receipt it would pay twice.

    Which receipt it reconciles against has to be exact, or the rule does the
    very damage it exists to prevent. Two things scope it:

      • only a CLIENT_PROVIDER row counts. Ads banked back after a refused
        reward are credits too, and matching those meant a leftover credit from
        an hour-gapped spin silently voided the postback for a later, real ad;
      • only a receipt no postback has already answered. Once one postback has
        landed for a receipt, the next belongs to a different ad.
    """
    from models import AdReward
    now = datetime.utcnow()
    cutoff = now - timedelta(seconds=POSTBACK_DEDUPE_SECONDS)
    receipt = (session.query(AdReward)
               .filter(AdReward.telegram_id == telegram_id,
                       AdReward.provider == CLIENT_PROVIDER,
                       AdReward.received_at >= cutoff)
               .order_by(AdReward.received_at.desc())
               .first())
    already_reconciled = receipt is not None and (
        session.query(AdReward)
        .filter(AdReward.telegram_id == telegram_id,
                AdReward.provider == "adsgram",
                AdReward.received_at >= receipt.received_at)
        .first() is not None)
    duplicate_of_client_ad = receipt is not None and not already_reconciled
    row = AdReward(
        telegram_id=telegram_id,
        received_at=now,
        provider="adsgram",
        source_ip=(source_ip or "")[:64],
        query_string=(query_string or "")[:500],
        # Consumed on arrival when the client already registered this ad.
        consumed_at=now if duplicate_of_client_ad else None,
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
    return _claim_row(
        session,
        _unclaimed(session, telegram_id, POSTBACK_WINDOW_SECONDS)
        # Saved ads share the table but not the window; they are claimed by
        # claim_credit. Legacy rows have a NULL provider and are postbacks.
        .filter((AdReward.provider.is_(None))
                | (AdReward.provider.notin_(REDEEMABLE_PROVIDERS)))
        .first())


def _unclaimed(session, telegram_id: int, window_seconds: int,
               oldest_first: bool = False):
    """Query for this user's unconsumed reward rows inside ``window_seconds``.

    Newest first by default, which is what a postback wants: the latest one is
    the ad the user just watched. Credits want the opposite — see
    ``claim_credit``.
    """
    from models import AdReward
    cutoff = datetime.utcnow() - timedelta(seconds=window_seconds)
    order = (AdReward.received_at.asc() if oldest_first
             else AdReward.received_at.desc())
    return (session.query(AdReward)
            .filter(AdReward.telegram_id == telegram_id,
                    AdReward.consumed_at.is_(None),
                    AdReward.received_at >= cutoff)
            .order_by(order))


def _claim_row(session, row) -> bool:
    """Consume ``row`` with a conditional UPDATE. See ``claim_postback``."""
    from models import AdReward
    if not row:
        return False
    claimed = (session.query(AdReward)
               .filter(AdReward.id == row.id,
                       AdReward.consumed_at.is_(None))
               .update({AdReward.consumed_at: datetime.utcnow()},
                       synchronize_session=False))
    session.commit()
    return bool(claimed)


def bank_credit(session, telegram_id: int, reason: str = "",
                provider: str = CREDIT_PROVIDER):
    """Bank a watched-but-unspendable ad as a credit the user can redeem later.

    Called wherever ad evidence has already been consumed and the reward could
    not be granted — the gap between rewarded ads, an exhausted cycle, an empty
    reward table, an error mid-spin. The credit carries no expiry beyond
    ``CREDIT_TTL_SECONDS`` and needs no second ad to redeem.

    Returns the new row's id, or None if it could not be written.

    Deliberately swallows its own failures: this runs on the error path of a
    request that is already returning bad news, and an exception here would
    replace a recoverable "your ad is saved" with a 500.
    """
    from models import AdReward
    try:
        row = AdReward(
            telegram_id=telegram_id,
            received_at=datetime.utcnow(),
            provider=provider,
            query_string=(reason or "")[:500],
        )
        session.add(row)
        session.commit()
        return row.id
    except Exception:
        logger.exception("bank_credit failed for %s", telegram_id)
        try:
            session.rollback()
        except Exception:
            pass
        return None


def claim_credit(session, telegram_id: int) -> bool:
    """Consume one banked credit, oldest first. True if one was just spent.

    FIFO matters here in a way it doesn't for postbacks. Credits live for
    ``CREDIT_TTL_SECONDS``, and a player who banks them faster than they spend
    them would, under newest-first, always redeem the fresh one while the
    oldest sat untouched until it expired — losing a watched ad, which is the
    single thing this ledger exists to prevent.

    Any unconsumed row counts, not only the ones tagged as credits. A network
    postback is proof of a watched ad too, and restricting this to
    ``REDEEMABLE_PROVIDERS`` quietly threw those away: ``claim_postback`` only
    looks back POSTBACK_WINDOW_SECONDS, so a postback nobody spent within five
    minutes — the player closed the app, the phone dropped the reward request,
    the free slot covered that spin — became unreachable by every path and the
    ad was gone. Nothing here can double-pay for it: a postback that merely
    echoes a client receipt is stored already consumed by ``record_postback``,
    and consuming is a conditional UPDATE.
    """
    return _claim_row(
        session,
        _unclaimed(session, telegram_id, CREDIT_TTL_SECONDS, oldest_first=True)
        .first())


def count_credits(session, telegram_id: int) -> int:
    """How many watched-but-unspent ads this user can still redeem.

    Reported to the client so a saved ad is visible ("1 ad saved — spin it
    now") rather than a silent balance the player has to stumble into. Counts
    exactly what ``claim_credit`` will spend, postbacks included.
    """
    try:
        return int(_unclaimed(session, telegram_id, CREDIT_TTL_SECONDS)
                   .count() or 0)
    except Exception:
        logger.exception("count_credits failed for %s", telegram_id)
        return 0


def _issue_token(prefix: str, telegram_id: int, scope=None,
                 receipt_id=None) -> str:
    """Mint a one-shot token for ``telegram_id``, valid for ``CLIENT_TOKEN_TTL``.

    ``scope`` is None for a watched-ad token and the quota name for a no-fill
    pass; ``_consume_token`` requires an exact match, so the two can never be
    spent on each other.

    ``receipt_id`` is the ``AdReward`` row this token addresses. When set, that
    row — not this dict entry — is what actually gets spent, so the ad survives
    the token.
    """
    _gc_tokens()
    token = prefix + secrets.token_urlsafe(16)
    with _TOKENS_LOCK:
        _CLIENT_TOKENS[token] = TokenRecord(
            telegram_id, time.time() + CLIENT_TOKEN_TTL, scope, receipt_id)
    return token


def _take_token(prefix: str, token: str, telegram_id: int, scope=None):
    """Validate a one-shot token and, only if it is good, spend it.

    Returns the removed ``TokenRecord``, or None.

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
        return None
    with _TOKENS_LOCK:
        record = _CLIENT_TOKENS.get(token)
        if not record:
            return None
        if record.telegram_id != telegram_id:
            return None
        if time.time() > record.expires_at:
            _CLIENT_TOKENS.pop(token, None)
            return None
        # A scoped token is only good for the feature it was issued for, so a
        # pass bought out of the spin grace can't be spent on another reward.
        if record.scope != scope:
            return None
        return _CLIENT_TOKENS.pop(token, None)


def _consume_token(prefix: str, token: str, telegram_id: int, scope=None) -> bool:
    """``_take_token`` as a boolean, for tokens with no receipt behind them."""
    return _take_token(prefix, token, telegram_id, scope) is not None


def register_client_ad(session, telegram_id: int):
    """Write down that this user just watched an ad. Returns the row id.

    Called from /api/webapp/ad-completed, and the reason "watched an ad, got
    nothing" was still possible after everything above: the *proof* used to
    exist only as an entry in ``_CLIENT_TOKENS``, which is process memory with
    a two-minute life. Every one of these lost a real ad:

      • the web process restarted (a deploy, a crash, an idle recycle) and took
        every outstanding token with it;
      • the player's phone took longer than the TTL to get from the end of the
        ad to the reward request — backgrounded app, tunnel, dead signal;
      • the reward request was refused (a cooldown the client hadn't seen yet,
        a spent cycle), which returns before the token is ever presented;
      • the /ad-completed response itself was lost, so the client never even
        received the token the server had minted.

    A row survives all four. The token now only *addresses* it, so a lost token
    costs the player nothing: the ledger still holds their ad and the next spin
    or pack redeems it with no second ad.

    One ad must still be one row. If Adsgram's server postback for this same ad
    has already landed, this binds to that row instead of adding another.

    This endpoint is reachable by any authenticated client, and a durable row
    is worth more than the two-minute token it replaces — a client that called
    it in a loop used to accumulate tokens that expired unused, but would now
    accumulate saved ads and never need to watch a real one again. Two limits
    make that pointless without touching the honest path: a second call inside
    ``MIN_CLIENT_AD_INTERVAL_SECONDS`` is treated as the same ad (nothing
    completes a rewarded ad that fast), and once
    ``MAX_OUTSTANDING_CLIENT_ADS`` are already banked, further calls bind to
    one of those instead of adding more.

    Both of those are check-then-insert, so the whole body runs under
    ``_REGISTER_LOCK`` — two racing requests that each read "no recent receipt"
    would otherwise both insert and turn one ad into two.
    """
    with _REGISTER_LOCK:
        return _register_client_ad(session, telegram_id)


def _register_client_ad(session, telegram_id: int):
    """``register_client_ad`` with the lock already held."""
    from models import AdReward
    now = datetime.utcnow()

    postback = (_unclaimed(session, telegram_id, POSTBACK_WINDOW_SECONDS)
                .filter((AdReward.provider.is_(None))
                        | (AdReward.provider.notin_(REDEEMABLE_PROVIDERS)))
                .first())
    if postback:
        return postback.id

    # Same ad, registered twice (a retried request, or a client in a loop).
    recent = (session.query(AdReward)
              .filter(AdReward.telegram_id == telegram_id,
                      AdReward.provider == CLIENT_PROVIDER,
                      AdReward.received_at
                      >= now - timedelta(seconds=MIN_CLIENT_AD_INTERVAL_SECONDS))
              .order_by(AdReward.received_at.desc())
              .first())
    if recent is not None:
        # Still unspent → the token addresses it. Already spent → no receipt,
        # so the token behaves like the memory-only one it used to be.
        return recent.id if recent.consumed_at is None else None

    # Counts every unspent row, which is what ``count_credits`` reports to the
    # player — filtering to the non-postback providers here would let the cap
    # disagree with the number on their screen.
    outstanding = (_unclaimed(session, telegram_id, CREDIT_TTL_SECONDS,
                              oldest_first=True)
                   .limit(MAX_OUTSTANDING_CLIENT_ADS + 1)
                   .all())
    if len(outstanding) >= MAX_OUTSTANDING_CLIENT_ADS:
        return outstanding[0].id

    return bank_credit(session, telegram_id, "ad watched (client)",
                       provider=CLIENT_PROVIDER)


def issue_client_token(telegram_id: int, session=None) -> str:
    """Generate a single-use token after client-side ad completion.

    The frontend asks for one of these AFTER the Adsgram SDK promise resolves
    successfully; then includes it in the spin request. We can't fully trust
    this (it's frontend-issued) but combined with the ad quota and cooldown
    it's adequate per Adsgram's own guidance for small apps.

    With a ``session`` the ad is also written to the ledger and the token is
    bound to that row — see ``register_client_ad``. Without one the token is
    memory-only, which is the old behaviour and all a caller with no database
    handle can do.
    """
    receipt_id = None
    if session is not None:
        try:
            receipt_id = register_client_ad(session, telegram_id)
        except Exception:
            # A token with no receipt still works for an immediate spin; only
            # the durability is lost. Better than failing the whole claim.
            logger.exception("could not register the ad receipt for %s",
                             telegram_id)
    return _issue_token(CLIENT_TOKEN_PREFIX, telegram_id,
                        receipt_id=receipt_id)


def consume_client_token(token: str, telegram_id: int, session=None) -> bool:
    """Check + remove a client-side ad token. Returns True if valid for user.

    When the token carries a receipt, spending it means consuming that row —
    the database, not the dict, decides. That is what stops one ad paying
    twice: a token whose row was already redeemed as a saved ad (or claimed as
    a postback) is spent, and this returns False.
    """
    record = _take_token(CLIENT_TOKEN_PREFIX, token, telegram_id)
    if record is None:
        return False
    if record.receipt_id is None or session is None:
        return True
    from models import AdReward
    return _claim_row(session, session.get(AdReward, record.receipt_id))


def preserve_client_ad(session, token: str, telegram_id: int,
                       reason: str = "") -> bool:
    """Keep a watched ad the caller turned out not to need.

    For the case where the reward came free after all — the cycle rolled over
    between the tap and the request — so the ad should go to the *next* one
    rather than being spent here or left to rot.

    A token with a receipt behind it needs nothing done: the row is already in
    the ledger, unconsumed, and the next spin picks it up. A memory-only token
    (one minted before this shipped, or without a session) has to be converted
    into a row before its two minutes run out.
    """
    record = _take_token(CLIENT_TOKEN_PREFIX, token, telegram_id)
    if record is None:
        return False
    if record.receipt_id is not None:
        return True
    return bank_credit(session, telegram_id, reason) is not None


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
        expired = [t for t, record in _CLIENT_TOKENS.items()
                   if record.expires_at < now]
        for t in expired:
            _CLIENT_TOKENS.pop(t, None)

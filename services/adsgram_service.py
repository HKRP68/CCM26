"""Adsgram integration helpers.

Adsgram has two ways the publisher learns "user watched the ad":
  1. CLIENT-SIDE: the SDK's AdController.show() promise resolves
     with `done: true` in the user's browser
  2. SERVER-SIDE (optional): Adsgram fires a GET request to your
     configured Reward URL with the user's telegram_id

For small/new apps, Adsgram says the server-side Reward URL "makes sense
for publishers who have more than 50k daily users" — so we treat both
as valid evidence but prefer the server-side one when present.

How we use it:
  - The /api/adsgram/reward endpoint is what Adsgram's servers hit.
    We just insert an AdsgramReward row each time.
  - When /api/webapp/spin is called, we look for an UN-consumed
    AdsgramReward row for this telegram_id received in the last
    POSTBACK_WINDOW_SECONDS. If found, we mark it consumed and grant
    the spin. If not found, we fall back to the client-side ad token
    (less secure but acceptable per Adsgram's own guidance for small apps).
  - Client-side tokens are time-limited and one-shot too (tracked
    in-memory per process for replay protection).

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
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Window after Adsgram fires its postback during which it can be claimed
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


def is_configured() -> bool:
    """True when ADSGRAM_BLOCK_ID env var is set (real ads active).
    False means mock/dev mode."""
    bid = os.getenv("ADSGRAM_BLOCK_ID", "").strip()
    return bool(bid and bid.lower() not in ("", "none", "mock", "disabled"))


def get_block_id() -> str | None:
    """Returns the Adsgram block ID for the client SDK, or None for mock mode."""
    if not is_configured():
        return None
    return os.getenv("ADSGRAM_BLOCK_ID", "").strip()


def record_postback(session, telegram_id: int, source_ip: str = None,
                    query_string: str = None):
    """Insert a row when Adsgram pings the Reward URL.
    Called from /api/adsgram/reward."""
    from models import AdsgramReward
    row = AdsgramReward(
        telegram_id=telegram_id,
        received_at=datetime.utcnow(),
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
    """
    from models import AdsgramReward
    cutoff = datetime.utcnow() - timedelta(seconds=POSTBACK_WINDOW_SECONDS)
    row = (session.query(AdsgramReward)
           .filter(AdsgramReward.telegram_id == telegram_id,
                   AdsgramReward.consumed_at.is_(None),
                   AdsgramReward.received_at >= cutoff)
           .order_by(AdsgramReward.received_at.desc())
           .first())
    if not row:
        return False
    row.consumed_at = datetime.utcnow()
    session.commit()
    return True


def _issue_token(prefix: str, telegram_id: int, scope=None) -> str:
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

    The frontend asks for one of these AFTER the Adsgram SDK promise
    resolves successfully; then includes it in the spin request. We
    can't fully trust this (it's frontend-issued) but combined with
    the 8h cooldown it's adequate per Adsgram's own guidance.
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

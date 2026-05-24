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
"""

import logging
import os
import secrets
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Window after Adsgram fires its postback during which it can be claimed
POSTBACK_WINDOW_SECONDS = 300  # 5 minutes

# Client-side ad tokens are stored in-memory for replay protection
# Map of token → (telegram_id, expires_at). Cleared periodically.
_CLIENT_TOKENS = {}
CLIENT_TOKEN_TTL = 120  # 2 minutes for the user to claim after ad finishes


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


def issue_client_token(telegram_id: int) -> str:
    """Generate a single-use token after client-side ad completion.

    The frontend asks for one of these AFTER the Adsgram SDK promise
    resolves successfully; then includes it in the spin request. We
    can't fully trust this (it's frontend-issued) but combined with
    the 8h cooldown it's adequate per Adsgram's own guidance.
    """
    _gc_tokens()
    token = "CT-" + secrets.token_urlsafe(16)
    _CLIENT_TOKENS[token] = (telegram_id, time.time() + CLIENT_TOKEN_TTL)
    return token


def consume_client_token(token: str, telegram_id: int) -> bool:
    """Check + remove a client-side ad token. Returns True if valid for user."""
    _gc_tokens()
    record = _CLIENT_TOKENS.pop(token, None)
    if not record:
        return False
    tg_id, expires = record
    if tg_id != telegram_id:
        return False
    if time.time() > expires:
        return False
    return True


def _gc_tokens():
    """Drop expired client tokens to keep the dict bounded."""
    now = time.time()
    expired = [t for t, (_, exp) in _CLIENT_TOKENS.items() if exp < now]
    for t in expired:
        _CLIENT_TOKENS.pop(t, None)

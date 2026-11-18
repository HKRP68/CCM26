"""Telegram Mini App authentication.

When a user opens the Mini App, Telegram passes an `initData` string
containing the user's identity, signed by Telegram with HMAC-SHA256
using a key derived from the bot token. Verifying this signature is
how we authenticate the user — no separate login needed.

Reference: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qsl

logger = logging.getLogger(__name__)

# Reject initData older than this many seconds (replay protection)
MAX_AGE_SECONDS = 86400  # 24 hours


def verify_init_data(init_data: str, bot_token: str) -> dict | None:
    """Validate Telegram Mini App initData.

    Args:
      init_data: the raw string Telegram passes as window.Telegram.WebApp.initData
      bot_token: your bot's secret token

    Returns:
      A dict with parsed fields (including 'user' as a sub-dict) if valid.
      None if signature is invalid, malformed, or stale.
    """
    if not init_data or len(init_data) > 16384 or not bot_token:
        return None

    try:
        # Parse the query-string-style initData
        fields = parse_qsl(init_data, keep_blank_values=True, max_num_fields=64)
        if any(not key.isidentifier() or "\n" in value for key, value in fields):
            return None
        parsed = dict(fields)
        received_hash = parsed.pop("hash", None)
        if len(fields) != len(parsed) + 1 or not received_hash or not received_hash.isascii():
            return None
        auth_date = parsed.get("auth_date", "")
        if not auth_date.isascii() or not auth_date.isdecimal() or len(auth_date) > 12:
            return None
        age = time.time() - int(auth_date)
        if not -30 <= age <= MAX_AGE_SECONDS:
            return None

        # Build data-check-string per Telegram spec:
        # sorted key=value pairs joined by \n
        data_check_string = "\n".join(
            f"{k}={parsed[k]}" for k in sorted(parsed.keys())
        )

        # Derive secret key: HMAC-SHA256("WebAppData", bot_token)
        secret_key = hmac.new(
            b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
        ).digest()

        # Compute expected hash
        expected_hash = hmac.new(
            secret_key, data_check_string.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(expected_hash, received_hash):
            return None

        # Parse the user JSON if present
        if "user" in parsed:
            try:
                parsed["user"] = json.loads(parsed["user"])
            except (ValueError, TypeError):
                return None

        return parsed
    except Exception:
        logger.exception("initData validation crashed")
        return None


def get_user_id(verified_data: dict) -> int | None:
    """Extract the Telegram user id from verified initData."""
    if not verified_data:
        return None
    user = verified_data.get("user")
    if isinstance(user, dict):
        uid = user.get("id")
        if type(uid) is int and 0 < uid < 2**63:
            return uid
        if isinstance(uid, str) and uid.isascii() and uid.isdecimal() and len(uid) < 20:
            value = int(uid)
            if 0 < value < 2**63:
                return value
    return None

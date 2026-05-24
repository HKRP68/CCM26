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
from urllib.parse import parse_qsl, unquote

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
    if not init_data or not bot_token:
        return None

    try:
        # Parse the query-string-style initData
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None

        # Reject stale initData (replay protection)
        auth_date = parsed.get("auth_date")
        if auth_date:
            try:
                age = time.time() - int(auth_date)
                if age > MAX_AGE_SECONDS:
                    logger.warning(f"initData too old: {age:.0f}s")
                    return None
            except ValueError:
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
        if isinstance(uid, int):
            return uid
        try:
            return int(uid)
        except (ValueError, TypeError):
            pass
    return None

"""Telegram Login Widget verification for the standalone franchise website.

The Login Widget uses SHA256(bot_token), unlike Mini App initData's
WebAppData key derivation. Never accept unsigned profile fields as identity.
"""

import hashlib
import hmac
import re
import time

LOGIN_MAX_AGE_SECONDS = 300
SESSION_MAX_AGE_SECONDS = 86400
_WIDGET_FIELDS = frozenset({"id", "first_name", "last_name", "username",
                            "photo_url", "auth_date", "hash"})


def valid_bot_token(value):
    """Reject missing tokens and configuration placeholders before login."""
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9]{5,}:[A-Za-z0-9_-]{20,}", value))


def positive_integer(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    if not re.fullmatch(r"[0-9]+", str(value)):
        return None
    number = int(value)
    return number if 0 < number < 2**63 else None


def verify_widget_login(payload, bot_token, *, now=None):
    """Return a minimal verified profile or None for invalid/stale input."""
    if not valid_bot_token(bot_token) or not isinstance(payload, dict):
        return None
    if not payload.keys() <= _WIDGET_FIELDS:
        return None
    if not {"id", "first_name", "auth_date", "hash"} <= payload.keys():
        return None
    if any(isinstance(v, bool) or not isinstance(v, (str, int)) for v in payload.values()):
        return None
    fields = {key: str(value) for key, value in payload.items() if key != "hash"}
    if any("\n" in value or "\r" in value or len(value) > 2048 for value in fields.values()):
        return None
    received = payload["hash"]
    if not isinstance(received, str) or not re.fullmatch(r"[0-9a-f]{64}", received):
        return None
    user_id = positive_integer(payload["id"])
    auth_date = positive_integer(payload["auth_date"])
    if user_id is None or auth_date is None:
        return None
    age = (time.time() if now is None else now) - auth_date
    if age < 0 or age > LOGIN_MAX_AGE_SECONDS:
        return None
    if not isinstance(payload["first_name"], str) or not 0 < len(payload["first_name"]) <= 256:
        return None
    username = payload.get("username") or ""
    if not isinstance(username, str) or (username and not re.fullmatch(r"[A-Za-z0-9_]{1,32}", username)):
        return None
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    expected = hmac.new(key, check.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    return {"id": user_id, "first_name": payload["first_name"], "username": username}

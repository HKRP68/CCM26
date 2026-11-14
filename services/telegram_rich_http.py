"""Rich delivery for synchronous website/news notification workers."""

from collections import OrderedDict
import hashlib
from threading import Lock
import time

from services import rich_message as R
from services.rich_message_defaults import content_for

_unavailable = OrderedDict()
_lock = Lock()


def post_message(token, payload, *, timeout=12, post=None):
    """Return the original requests response; retain all privacy and controls.

    Retry a rejected native format through the original formatted send. A
    network error or flood limit is left to the caller instead of repeating a
    possibly delivered message or evading Telegram's rate limit.
    """
    if post is None:
        import requests

        post = requests.post
    base = f"https://api.telegram.org/bot{token}/"
    key = hashlib.sha256(token.encode()).digest()
    with _lock:
        supported = time.monotonic() >= _unavailable.get(key, 0)
    content = content_for(payload) if R.rich_text_enabled() and supported else None
    if content is not None:
        native = {
            name: value
            for name, value in payload.items()
            if name
            not in (
                "text",
                "parse_mode",
                "entities",
                "link_preview_options",
                "disable_web_page_preview",
            )
        }
        native["rich_message"] = content
        response = post(base + "sendRichMessage", json=native, timeout=timeout)
        body = response.json()
        if body.get("ok") or body.get("error_code") not in (400, 404):
            return response
        description = str(body.get("description", "")).lower()
        if body.get("error_code") == 404 or any(
            hint in description for hint in R._MISSING_METHOD_HINTS
        ):
            with _lock:
                if len(_unavailable) >= 64:
                    _unavailable.popitem(last=False)
                _unavailable[key] = time.monotonic() + R.UNSUPPORTED_RETRY_SECONDS
    return post(base + "sendMessage", json=payload, timeout=timeout)

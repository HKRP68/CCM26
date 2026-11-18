"""Fetch a small Telegram display photo without disclosing credentials."""

import logging
import os
import re
import time
from functools import lru_cache
from io import BytesIO

logger = logging.getLogger(__name__)


def profile_photo(telegram_id):
    return _cached_photo(telegram_id, int(time.monotonic()) // 300)


@lru_cache(maxsize=128)
def _cached_photo(telegram_id, period):
    import requests
    from PIL import Image

    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        return None
    def telegram(method, params):
        response = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=params,
                                 timeout=4, allow_redirects=False)
        return (response.json() or {}).get("result", {}) if response.status_code == 200 else {}

    try:
        result = telegram("getUserProfilePhotos", {"user_id": telegram_id, "limit": 1})
        photos = result.get("photos", [])
        if not photos or not photos[0]:
            return None
        size = min(photos[0], key=lambda image: abs(image.get("width", 0) - 160))
        file = telegram("getFile", {"file_id": size["file_id"]})
        path = file.get("file_path", "")
        # Telegram's sandbox-relative profile photo paths.
        if not re.fullmatch(r"photos/[A-Za-z0-9_-]+\.(?:jpg|jpeg|png|webp)", path):
            return None
        with requests.get(f"https://api.telegram.org/file/bot{token}/{path}", stream=True,
                          timeout=4, allow_redirects=False) as response:
            if response.status_code != 200:
                return None
            raw = response.raw.read(512 * 1024 + 1, decode_content=True)
        if len(raw) > 512 * 1024:
            return None
        with Image.open(BytesIO(raw)) as photo:
            if photo.width * photo.height > 4_000_000:
                return None
            photo.thumbnail((192, 192))
            out = BytesIO()
            photo.convert("RGB").save(out, "JPEG", quality=85)
            return out.getvalue()
    except Exception:
        # Transport exceptions can include the bot token in their request URL.
        logger.warning("Telegram profile photo unavailable")
        return None

"""Event sound service — gameplay audio for the Crickidex Arena Mini App.

Mirrors services.event_media_service, but for audio: one sound per key, with
committed default files under static/cricket/sounds/ and optional admin
overrides stored in the EventSound table (Telegram file_id / URL / disk file).

The Mini App receives the enabled sounds in the /api/match poll payload as
payload["eventSounds"] and plays them client-side (see static/cricket/app.js).
"""

import os
import time
import logging

from services.perf_log import perf_timed

logger = logging.getLogger(__name__)

# All sound keys. Order matters for the admin UI. category:
#   'ingame' → one-shot effects, gated by the player's In-Game Sound toggle
#   'match'  → crowd ambience loop, gated by the Match Sound toggle
SOUND_KEYS = [
    ("wicket",  "🟥 Wicket",       "When a wicket falls",              "ingame"),
    ("four",    "🏏 Four",         "When a 4 is hit",                  "ingame"),
    ("six",     "🔥 Six",          "When a 6 is hit",                  "ingame"),
    ("runs",    "🏃 1/2/3 Runs",   "When the batsman runs 1, 2 or 3",  "ingame"),
    ("no_ball", "🚫 No Ball",      "When a no-ball is bowled",         "ingame"),
    ("crowd_1", "📣 Crowd 1",      "Stadium ambience (alternates with Crowd 2)", "match"),
    ("crowd_2", "📣 Crowd 2",      "Stadium ambience (alternates with Crowd 1)", "match"),
]

VALID_SOUND_KEYS = {k for k, _, _, _ in SOUND_KEYS}

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Committed default files — durable across redeploys, unlike data/.
DEFAULT_SOUND_FILES = {
    "wicket":  os.path.join(_PROJECT_ROOT, "static", "cricket", "sounds", "wicket.mp3"),
    "four":    os.path.join(_PROJECT_ROOT, "static", "cricket", "sounds", "four.mp3"),
    "six":     os.path.join(_PROJECT_ROOT, "static", "cricket", "sounds", "six.mp3"),
    "runs":    os.path.join(_PROJECT_ROOT, "static", "cricket", "sounds", "runs.mp3"),
    "no_ball": os.path.join(_PROJECT_ROOT, "static", "cricket", "sounds", "no_ball.wav"),
    "crowd_1": os.path.join(_PROJECT_ROOT, "static", "cricket", "sounds", "crowd_1.mp3"),
    "crowd_2": os.path.join(_PROJECT_ROOT, "static", "cricket", "sounds", "crowd_2.mp3"),
}

# Disk-fallback upload directory (unreliable on ephemeral hosts — the admin
# page warns and prefers the Telegram storage channel, same as event media).
SOUND_UPLOAD_DIR = os.path.join(_PROJECT_ROOT, "data", "event_media", "sounds")

ALLOWED_SOUND_EXTENSIONS = (".mp3", ".wav", ".ogg", ".m4a")
MAX_SOUND_UPLOAD_BYTES = 10 * 1024 * 1024

_CONTENT_TYPES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
}


def sound_content_type(filename):
    ext = os.path.splitext(str(filename or ""))[1].lower()
    return _CONTENT_TYPES.get(ext, "audio/mpeg")


def clamp_volume(value, default=100):
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return default


def sound_category(sound_key):
    for key, _label, _desc, category in SOUND_KEYS:
        if key == sound_key:
            return category
    return "ingame"


def miniapp_event_sounds(rows):
    """Serialize the sound config for the browser Mini App.

    `rows` are the EventSound overrides; every key without a row falls back to
    its committed default. Keys explicitly disabled by the admin are omitted so
    the client simply never plays them.
    """
    by_key = {r.sound_key: r for r in rows}
    result = {}
    for key, _label, _desc, category in SOUND_KEYS:
        row = by_key.get(key)
        if row is not None and not row.enabled:
            continue
        version = 0
        volume = 100
        if row is not None:
            volume = clamp_volume(row.volume)
            try:
                version = int(row.updated_at.timestamp()) if row.updated_at else row.id
            except (AttributeError, OSError, OverflowError, ValueError):
                version = row.id or 0
        result[key] = {
            "url": f"/api/event-sound/{key}?v={version}",
            "volume": volume / 100.0,
            "category": category,
        }
    return result


_miniapp_cache = {"expires": 0, "payload": {}}


@perf_timed("get_miniapp_event_sounds")
def get_miniapp_event_sounds(session, ttl_seconds=5):
    """Return the Mini App sound config with a short poll-friendly cache."""
    now = time.time()
    if now < _miniapp_cache["expires"]:
        return _miniapp_cache["payload"]
    from models import EventSound
    try:
        rows = session.query(EventSound).all()
    except Exception:
        # Missing table on a not-yet-migrated DB must never break match polls.
        logger.exception("get_miniapp_event_sounds query failed; serving defaults")
        rows = []
    payload = miniapp_event_sounds(rows)
    _miniapp_cache.update(expires=now + ttl_seconds, payload=payload)
    return payload


def invalidate_miniapp_event_sounds():
    _miniapp_cache.update(expires=0, payload={})

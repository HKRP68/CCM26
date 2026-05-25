"""Telegram channel storage for player images and other files.

Render's filesystem doesn't persist between deploys. Neon storage is
expensive. Telegram lets bots upload files to channels (or DMs to self)
and reference them later by `file_id` — free, fast, served by Telegram's
CDN, and persisting forever.

Usage:
  1. Set env var `STORAGE_CHAT_ID` to a chat ID the bot can post to.
     Easiest: a private channel where you're the only member; add the
     bot as admin with "Post messages" permission. Channel ID is
     `-100<numeric_id>`. Or just use your own user ID — DM-to-self works.
  2. When uploading an image, call `upload_photo(image_path)`. Returns
     the file_id string. Cache it in DB.
  3. When sending the image later, use the cached file_id directly with
     `InputFile` / `send_photo(photo=file_id)`. No file system access.

If `STORAGE_CHAT_ID` isn't configured, upload_photo() returns None — the
caller should fall back to local disk path. This makes the feature
opt-in: it works without storage setup, just less efficiently.
"""

import os
import logging
from telegram import Bot, InputFile

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(os.getenv("STORAGE_CHAT_ID", "").strip()
                and os.getenv("BOT_TOKEN", "").strip())


def _chat_id():
    raw = os.getenv("STORAGE_CHAT_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return raw  # could be @channelname (str)


async def upload_photo_async(image_path: str, caption: str = None) -> str | None:
    """Upload a local image to the storage channel; return Telegram file_id.

    Returns None if storage isn't configured or upload fails.
    """
    if not is_configured():
        return None
    if not os.path.exists(image_path):
        logger.warning(f"upload_photo: file not found: {image_path}")
        return None
    try:
        token = os.getenv("BOT_TOKEN", "").strip()
        bot = Bot(token=token)
        chat_id = _chat_id()
        with open(image_path, "rb") as f:
            msg = await bot.send_photo(chat_id=chat_id, photo=f,
                                       caption=caption[:1024] if caption else None)
        # Telegram returns multiple photo sizes — we want the highest-res file_id
        if msg.photo:
            return msg.photo[-1].file_id
        return None
    except Exception:
        logger.exception("upload_photo failed")
        return None


def upload_photo_sync(image_path: str, caption: str = None) -> str | None:
    """Sync wrapper for callers without an event loop (Flask routes).

    Spins up a fresh asyncio loop, runs the upload, closes it. Suitable
    for occasional admin actions (upload once per player image), NOT for
    high-frequency calls.
    """
    if not is_configured():
        return None
    import asyncio
    try:
        # Create a fresh loop to avoid colliding with bot's main loop.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                upload_photo_async(image_path, caption=caption))
        finally:
            loop.close()
    except Exception:
        logger.exception("upload_photo_sync failed")
        return None


async def upload_document_async(file_path: str, caption: str = None) -> str | None:
    """Upload any file (not just images) — used for archive dumps."""
    if not is_configured() or not os.path.exists(file_path):
        return None
    try:
        token = os.getenv("BOT_TOKEN", "").strip()
        bot = Bot(token=token)
        chat_id = _chat_id()
        with open(file_path, "rb") as f:
            msg = await bot.send_document(
                chat_id=chat_id, document=f,
                caption=caption[:1024] if caption else None)
        if msg.document:
            return msg.document.file_id
        return None
    except Exception:
        logger.exception("upload_document failed")
        return None

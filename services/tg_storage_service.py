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
     `send_photo(photo=file_id)`, or call `download_file_bytes_sync(file_id)`
     when a renderer needs the bytes after an ephemeral filesystem reset.

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


def _has_bot_token() -> bool:
    """Return whether Telegram API reads can be performed.

    Downloads only need the bot token. They intentionally do not require the
    storage chat ID so existing durable images still restore if that optional
    setting is renamed or temporarily absent after a redeploy.
    """
    return bool(os.getenv("BOT_TOKEN", "").strip())


async def download_file_bytes_async(file_id: str) -> bytes | None:
    """Download a Telegram file by ID and return its bytes.

    A stored ``file_id`` is durable across ephemeral filesystem resets. Return
    ``None`` on configuration or Telegram errors so callers can gracefully
    fall back without breaking card generation.
    """
    if not file_id or not _has_bot_token():
        return None
    try:
        bot = Bot(token=os.getenv("BOT_TOKEN", "").strip())
        tg_file = await bot.get_file(file_id=file_id)
        return bytes(await tg_file.download_as_bytearray())
    except Exception:
        logger.exception("download_file_bytes failed")
        return None


def download_file_bytes_sync(file_id: str) -> bytes | None:
    """Sync wrapper for restoring Telegram files from Flask/card render paths.

    Card renderers can be invoked by async bot handlers. If this function is
    called while their event loop is running, perform the download in a short-
    lived worker thread rather than attempting to nest asyncio event loops.
    """
    if not file_id or not _has_bot_token():
        return None
    import asyncio

    def _download():
        return asyncio.run(download_file_bytes_async(file_id))

    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return _download()
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(_download).result()
    except Exception:
        logger.exception("download_file_bytes_sync failed")
        return None


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

async def upload_text_async(text: str, filename: str, caption: str = None) -> str | None:
    """Upload an in-memory text blob to the Telegram storage channel as a file.

    Used for human-readable match scorecard dumps (e.g. ``MatchNo576.txt``).
    Returns the Telegram document ``file_id`` or ``None`` when storage is not
    configured or Telegram rejects the upload.
    """
    if not is_configured():
        return None
    try:
        import io
        token = os.getenv("BOT_TOKEN", "").strip()
        bot = Bot(token=token)
        chat_id = _chat_id()
        bio = io.BytesIO((text or "").encode("utf-8"))
        bio.name = filename
        msg = await bot.send_document(
            chat_id=chat_id,
            document=InputFile(bio, filename=filename),
            caption=caption[:1024] if caption else None,
        )
        if msg.document:
            return msg.document.file_id
        return None
    except Exception:
        logger.exception("upload_text_async failed")
        return None


async def upload_json_async(payload: dict, filename: str, caption: str = None) -> str | None:
    """Upload an in-memory JSON payload to the Telegram storage channel.

    This is used for durable scorecard/match-summary data snapshots. It returns
    the Telegram document ``file_id`` or ``None`` when storage is not configured
    or Telegram rejects the upload.
    """
    if not is_configured():
        return None
    try:
        import io
        import json
        token = os.getenv("BOT_TOKEN", "").strip()
        bot = Bot(token=token)
        chat_id = _chat_id()
        data = json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        bio = io.BytesIO(data)
        bio.name = filename
        msg = await bot.send_document(
            chat_id=chat_id,
            document=InputFile(bio, filename=filename),
            caption=caption[:1024] if caption else None,
        )
        if msg.document:
            return msg.document.file_id
        return None
    except Exception:
        logger.exception("upload_json_async failed")
        return None

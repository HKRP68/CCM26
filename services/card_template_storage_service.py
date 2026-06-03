"""Telegram-channel backed state for website card templates.

The card-template page stores layout values in a small JSON file and mirrors
that file to the Telegram storage channel as a pinned document. This keeps the
hot card path away from the database; the DB fields are only legacy fallback.
"""

import asyncio
import io
import json
import logging
import os
from datetime import datetime, timezone

from telegram import Bot

logger = logging.getLogger(__name__)

STATE_VERSION = 1
STATE_CAPTION_PREFIX = "card-template-state-v1"
STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "card_templates", "state.json",
)


def _storage_chat_id():
    raw = (os.getenv("CARD_TEMPLATE_STORAGE_CHAT_ID", "").strip()
           or os.getenv("STORAGE_CHAT_ID", "").strip()
           or os.getenv("MEDIA_STORAGE_CHAT_ID", "").strip())
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return raw


def is_configured():
    return bool(_storage_chat_id() is not None and os.getenv("BOT_TOKEN", "").strip())


def _ensure_dir():
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)


def load_local_state():
    try:
        if os.path.isfile(STATE_PATH):
            with open(STATE_PATH, "r", encoding="utf-8") as handle:
                state = json.load(handle)
            if state.get("version") == STATE_VERSION:
                return state
    except Exception:
        logger.exception("Failed to load local card-template state")
    return None


def save_local_state(state):
    _ensure_dir()
    payload = dict(state or {})
    payload["version"] = STATE_VERSION
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = f"{STATE_PATH}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, indent=2)
    os.replace(tmp, STATE_PATH)
    return payload


async def _pin_state_async(state, audit_text=None):
    if not is_configured():
        return False
    bot = Bot(token=os.getenv("BOT_TOKEN", "").strip())
    chat_id = _storage_chat_id()
    document = io.BytesIO(json.dumps(state, sort_keys=True).encode("utf-8"))
    document.name = "card_template_state.json"
    msg = await bot.send_document(
        chat_id=chat_id,
        document=document,
        caption=f"{STATE_CAPTION_PREFIX}\n{audit_text or 'card template save'}"[:1024],
        disable_notification=True,
    )
    await bot.pin_chat_message(
        chat_id=chat_id,
        message_id=msg.message_id,
        disable_notification=True,
    )
    return True


def pin_state_sync(state, audit_text=None):
    if not is_configured():
        return False

    async def _run():
        return await _pin_state_async(state, audit_text=audit_text)

    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(_run())
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(lambda: asyncio.run(_run())).result()
    except Exception:
        logger.exception("Failed to pin card-template state")
        return False


async def _load_pinned_state_async():
    if not is_configured():
        return None
    bot = Bot(token=os.getenv("BOT_TOKEN", "").strip())
    chat = await bot.get_chat(_storage_chat_id())
    pinned = getattr(chat, "pinned_message", None)
    caption = getattr(pinned, "caption", "") or ""
    document = getattr(pinned, "document", None)
    if not document or not caption.startswith(STATE_CAPTION_PREFIX):
        return None
    tg_file = await bot.get_file(document.file_id)
    payload = await tg_file.download_as_bytearray()
    state = json.loads(bytes(payload).decode("utf-8"))
    if state.get("version") != STATE_VERSION:
        return None
    return state


def restore_pinned_state_sync():
    if not is_configured():
        return None

    async def _run():
        return await _load_pinned_state_async()

    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            state = asyncio.run(_run())
        else:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=1) as executor:
                state = executor.submit(lambda: asyncio.run(_run())).result()
        if state:
            return save_local_state(state)
    except Exception:
        logger.exception("Failed to restore pinned card-template state")
    return None


def get_state(allow_restore=True):
    state = load_local_state()
    if not state and allow_restore:
        state = restore_pinned_state_sync()
    if state and allow_restore:
        restore_assets_sync(state.get("assets"))
    return state


def _asset_paths():
    try:
        from services.card_template_service import TEMPLATES_ROOT, ALLOWED_EXT, ALLOWED_FONT_EXT
    except Exception:
        return {}
    assets = {}
    for key, stem in (("template_base", "template"), ("template_star", "template_star"),
                      ("template_legend", "template_legend")):
        for ext in ALLOWED_EXT:
            path = os.path.join(TEMPLATES_ROOT, f"{stem}.{ext}")
            if os.path.isfile(path):
                assets[key] = {"path": path, "filename": f"{stem}.{ext}"}
                break
    for ext in ALLOWED_FONT_EXT:
        path = os.path.join(TEMPLATES_ROOT, f"font.{ext}")
        if os.path.isfile(path):
            assets["font"] = {"path": path, "filename": f"font.{ext}"}
            break
    return assets


async def _upload_assets_async():
    if not is_configured():
        return {}
    bot = Bot(token=os.getenv("BOT_TOKEN", "").strip())
    chat_id = _storage_chat_id()
    uploaded = {}
    for key, meta in _asset_paths().items():
        try:
            with open(meta["path"], "rb") as handle:
                msg = await bot.send_document(
                    chat_id=chat_id,
                    document=handle,
                    caption=f"card-template-asset-v1 {key} {meta['filename']}"[:1024],
                    disable_notification=True,
                )
            if msg.document:
                uploaded[key] = {
                    "file_id": msg.document.file_id,
                    "filename": meta["filename"],
                }
        except Exception:
            logger.exception("Failed to upload card-template asset %s", key)
    return uploaded


def upload_assets_sync():
    if not is_configured():
        return {}

    async def _run():
        return await _upload_assets_async()

    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(_run())
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(lambda: asyncio.run(_run())).result()
    except Exception:
        logger.exception("Failed to upload card-template assets")
        return {}


async def _restore_assets_async(assets):
    if not assets or not is_configured():
        return False
    try:
        from services.card_template_service import TEMPLATES_ROOT
    except Exception:
        return False
    bot = Bot(token=os.getenv("BOT_TOKEN", "").strip())
    os.makedirs(TEMPLATES_ROOT, exist_ok=True)
    restored = False
    for key, meta in (assets or {}).items():
        file_id = (meta or {}).get("file_id")
        filename = os.path.basename((meta or {}).get("filename") or "")
        if not file_id or not filename:
            continue
        path = os.path.join(TEMPLATES_ROOT, filename)
        if os.path.isfile(path):
            continue
        try:
            tg_file = await bot.get_file(file_id)
            payload = await tg_file.download_as_bytearray()
            tmp = f"{path}.tmp"
            with open(tmp, "wb") as handle:
                handle.write(bytes(payload))
            os.replace(tmp, path)
            restored = True
        except Exception:
            logger.exception("Failed to restore card-template asset %s", key)
    return restored


def restore_assets_sync(assets):
    if not assets or not is_configured():
        return False

    async def _run():
        return await _restore_assets_async(assets)

    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(_run())
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(lambda: asyncio.run(_run())).result()
    except Exception:
        logger.exception("Failed to restore card-template assets")
        return False

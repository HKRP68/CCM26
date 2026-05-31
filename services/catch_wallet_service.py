"""Telegram-channel-backed CRIC wallets for the /catch mini-game.

The wallet snapshot is stored as a pinned JSON document in a Telegram storage
chat.  This intentionally avoids Neon entirely: reading `/bal cric` and playing
`/catch` do not open a database session or write an activity-log row.

Configure a dedicated ``CATCH_STORAGE_CHAT_ID`` where the bot can post and pin
messages. ``STORAGE_CHAT_ID`` and ``MEDIA_STORAGE_CHAT_ID`` remain supported as
fallbacks for installations that already have a Telegram storage channel.
"""

import asyncio
import io
import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

STATE_VERSION = 1
STATE_CAPTION_PREFIX = "catch-wallet-state-v1"
DEFAULT_STARTING_CRIC = 1000

_lock = asyncio.Lock()
_balances = None


def _storage_chat_id():
    raw = (os.getenv("CATCH_STORAGE_CHAT_ID", "").strip()
           or os.getenv("STORAGE_CHAT_ID", "").strip()
           or os.getenv("MEDIA_STORAGE_CHAT_ID", "").strip())
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return raw


def _starting_balance():
    raw = os.getenv("CATCH_STARTING_CRIC", str(DEFAULT_STARTING_CRIC)).strip()
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("Invalid CATCH_STARTING_CRIC=%r; using %s", raw,
                       DEFAULT_STARTING_CRIC)
        return DEFAULT_STARTING_CRIC


def is_configured():
    return _storage_chat_id() is not None


async def _load_locked(bot):
    global _balances
    if _balances is not None:
        return
    chat_id = _storage_chat_id()
    if chat_id is None:
        raise RuntimeError("Telegram storage is not configured")

    chat = await bot.get_chat(chat_id)
    pinned = getattr(chat, "pinned_message", None)
    caption = getattr(pinned, "caption", "") or ""
    document = getattr(pinned, "document", None)
    if not document or not caption.startswith(STATE_CAPTION_PREFIX):
        _balances = {}
        return

    tg_file = await bot.get_file(document.file_id)
    payload = await tg_file.download_as_bytearray()
    state = json.loads(bytes(payload).decode("utf-8"))
    if state.get("version") != STATE_VERSION or not isinstance(state.get("balances"), dict):
        raise RuntimeError("Pinned catch-wallet snapshot has an unsupported format")
    _balances = {str(key): int(value) for key, value in state["balances"].items()}


async def _save_locked(bot, audit_text):
    chat_id = _storage_chat_id()
    payload = {
        "version": STATE_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "balances": _balances,
    }
    document = io.BytesIO(json.dumps(payload, sort_keys=True).encode("utf-8"))
    document.name = "catch_wallet_state.json"
    message = await bot.send_document(
        chat_id=chat_id,
        document=document,
        caption=f"{STATE_CAPTION_PREFIX}\n{audit_text}"[:1024],
        disable_notification=True,
    )
    await bot.pin_chat_message(
        chat_id=chat_id,
        message_id=message.message_id,
        disable_notification=True,
    )


async def get_balance(bot, telegram_id):
    """Return a user's CRIC balance without touching the application database."""
    async with _lock:
        await _load_locked(bot)
        return _balances.get(str(telegram_id), _starting_balance())


async def apply_catch_result(bot, telegram_id, bet, delta, audit_text):
    """Atomically apply a /catch result and persist the new Telegram snapshot.

    ``delta`` is the net change: positive winnings or the negative lost bet.
    Raises ``ValueError`` when the wallet cannot cover the bet.
    """
    global _balances
    async with _lock:
        await _load_locked(bot)
        key = str(telegram_id)
        current = _balances.get(key, _starting_balance())
        if current < bet:
            raise ValueError("insufficient CRIC balance")
        updated = current + delta
        previous = _balances.get(key)
        _balances[key] = updated
        try:
            await _save_locked(bot, f"{audit_text} · bal {updated} CRIC")
        except Exception:
            if previous is None:
                _balances.pop(key, None)
            else:
                _balances[key] = previous
            raise
        return updated


def _reset_cache_for_tests():
    global _balances
    _balances = None

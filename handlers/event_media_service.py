"""Event media service — pick + send a GIF for in-match events.

Usage from match handler:
    from services.event_media_service import fire_event_media
    await fire_event_media(context, chat_id, "six")

Behavior:
  - Picks one random enabled media entry for the event_key, weighted by `weight`
  - Sends via send_animation (works for GIF + mp4)
  - Has a per-(chat, event) cooldown to avoid spam in 6-6-6 overs
  - Never raises — silently no-ops on missing/disabled media or send failure
"""

import os
import logging
import random
import time
from sqlalchemy import and_

logger = logging.getLogger(__name__)

# All event keys we know about. Order matters for admin UI grouping.
EVENT_KEYS = [
    ("six",        "🔥 Six",          "When a 6 is hit"),
    ("four",       "🏏 Four",         "When a 4 is hit"),
    ("wicket",     "🟥 Wicket",       "When a wicket falls"),
    ("fifty",      "⭐ Fifty",        "Batsman reaches 50 runs"),
    ("hundred",    "🌟 Hundred",      "Batsman reaches 100 runs"),
    ("hattrick",   "🎯 Hat-trick",    "Bowler takes 3 wickets in 3 balls"),
    ("maiden_over","🎯 Maiden Over",  "Bowler bowls a maiden over"),
]

# Cooldown in seconds — same (chat, event) can't fire twice within this window
COOLDOWN_SECONDS = 8

# File-upload directory (relative to project root)
MEDIA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "event_media",
)


def _pick_random(items, weight_fn):
    """Weighted random pick. Returns one item or None if list empty."""
    if not items:
        return None
    weights = [max(1, weight_fn(it)) for it in items]
    total = sum(weights)
    r = random.random() * total
    cumulative = 0
    for it, w in zip(items, weights):
        cumulative += w
        if r < cumulative:
            return it
    return items[-1]


def _on_cooldown(ctx, chat_id, event_key):
    """Return True if we just fired this event in this chat recently."""
    key = f"emcd_{chat_id}_{event_key}"
    last = ctx.bot_data.get(key, 0)
    now = time.time()
    if now - last < COOLDOWN_SECONDS:
        return True
    ctx.bot_data[key] = now
    return False


async def fire_event_media(context, chat_id, event_key):
    """Send a random GIF for the given event_key to the chat.

    Silent on any error — never break the match flow.
    """
    if not event_key:
        return

    # Cooldown check first (cheap)
    if _on_cooldown(context, chat_id, event_key):
        return

    # Query DB for enabled media
    try:
        from database import get_session
        from models import EventMedia
        session = get_session()
        try:
            rows = (session.query(EventMedia)
                    .filter(EventMedia.event_key == event_key,
                            EventMedia.enabled == True)
                    .all())
            if not rows:
                # No media configured for this event — quietly skip
                return
            pick = _pick_random(rows, weight_fn=lambda r: r.weight or 1)
            if not pick:
                return
            source_type = pick.source_type
            source = pick.source
        finally:
            session.close()
    except Exception:
        logger.exception(f"fire_event_media({event_key}) DB error")
        return

    # Send animation
    try:
        if source_type == "url":
            # Telegram fetches the URL directly
            await context.bot.send_animation(chat_id=chat_id, animation=source)
        elif source_type == "telegram":
            # source IS a Telegram file_id obtained by previously uploading
            # the file to our storage channel. Telegram serves it directly.
            await context.bot.send_animation(chat_id=chat_id, animation=source)
        elif source_type == "file":
            # Local file path — open and send. Note: on Render free tier, the
            # disk wipes on every deploy, so this is unreliable. Admins should
            # use 'url' or 'telegram' source_type for durable storage.
            full_path = source
            if not os.path.isabs(full_path):
                full_path = os.path.join(MEDIA_DIR, os.path.basename(source))
            if not os.path.exists(full_path):
                logger.warning(f"Event media file missing: {full_path}")
                return
            with open(full_path, "rb") as f:
                await context.bot.send_animation(chat_id=chat_id, animation=f)
    except Exception:
        logger.exception(f"send_animation failed for event {event_key}")


# ════════════════════════════════════════════════════════════════════
# Storage-channel helper (used by the admin website on upload)
# ════════════════════════════════════════════════════════════════════

def upload_to_storage_channel(file_bytes, filename, original_label=None):
    """Upload a file to the configured Telegram storage channel and return
    its file_id. Telegram hosts the file forever; the file_id lets the bot
    re-send it without re-uploading.

    Synchronous interface so it can be called from Flask request handlers.
    Internally spins up a fresh asyncio event loop + Bot instance for the
    upload (~0.5–2s of overhead per call, fine for rare admin actions).

    Returns:
        {'success': True, 'file_id': '...', 'message_id': 12345}
      or
        {'success': False, 'error': 'human-readable reason'}
    """
    import asyncio
    from config import BOT_TOKEN, MEDIA_STORAGE_CHAT_ID

    if not BOT_TOKEN:
        return {"success": False, "error": "BOT_TOKEN env var not set"}
    if not MEDIA_STORAGE_CHAT_ID:
        return {"success": False, "error": "MEDIA_STORAGE_CHAT_ID env var not set"}

    # Detect file type from extension and route to the right Telegram method.
    # GIFs and short MP4s go via send_animation (returns msg.animation.file_id).
    # Anything else → send_document (returns msg.document.file_id).
    ext = os.path.splitext(filename)[1].lower()
    is_animation = ext in (".gif", ".mp4", ".webp")

    async def _do_upload():
        # Lazy import so the service module is importable without telegram
        # installed (the main bot dependency tree will obviously have it).
        from telegram import Bot
        from io import BytesIO

        bot = Bot(token=BOT_TOKEN)
        async with bot:
            buf = BytesIO(file_bytes)
            buf.name = filename  # python-telegram-bot uses .name for hint

            caption = (f"📦 Event media: {original_label or filename}"
                       if original_label else f"📦 {filename}")

            try:
                if is_animation:
                    msg = await bot.send_animation(
                        chat_id=MEDIA_STORAGE_CHAT_ID,
                        animation=buf, caption=caption[:1024],
                    )
                    if msg.animation:
                        return {"file_id": msg.animation.file_id,
                                "message_id": msg.message_id, "kind": "animation"}
                    if msg.document:
                        return {"file_id": msg.document.file_id,
                                "message_id": msg.message_id, "kind": "document"}
                    return {"error": "send_animation returned no file_id"}
                else:
                    msg = await bot.send_document(
                        chat_id=MEDIA_STORAGE_CHAT_ID,
                        document=buf, caption=caption[:1024],
                    )
                    if msg.document:
                        return {"file_id": msg.document.file_id,
                                "message_id": msg.message_id, "kind": "document"}
                    return {"error": "send_document returned no file_id"}
            except Exception as e:
                # Common: bot not admin in channel (Forbidden),
                # bad chat_id (BadRequest), file too large
                return {"error": f"{type(e).__name__}: {e}"}

    # Run the coroutine on a fresh loop. Flask handler is sync — this is safe.
    try:
        result = asyncio.run(_do_upload())
    except RuntimeError as e:
        # Already-running loop or other asyncio weirdness
        return {"success": False, "error": f"asyncio.run failed: {e}"}
    except Exception as e:
        return {"success": False, "error": f"upload failed: {e}"}

    if result.get("error"):
        return {"success": False, "error": result["error"]}
    return {"success": True, "file_id": result["file_id"],
            "message_id": result["message_id"], "kind": result["kind"]}

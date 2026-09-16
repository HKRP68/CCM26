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

from services.perf_log import perf_timed

logger = logging.getLogger(__name__)

# All event keys we know about. Order matters for admin UI grouping.
EVENT_KEYS = [
    ("dot_ball",   "⚫ Dot Ball",      "When a legal delivery scores no runs"),
    ("four",       "🏏 Four",          "When a 4 is hit"),
    ("six",        "🔥 Six",           "When a 6 is hit"),
    ("wicket",     "🟥 Wicket",        "When a wicket falls"),
    ("wide",       "↔️ Wide",          "When a wide is bowled"),
    ("no_ball",    "🚫 No Ball",       "When a no-ball is bowled"),
    ("fifty",      "⭐ Fifty",          "Batsman reaches 50 runs"),
    ("century",    "🌟 Century",        "Batsman reaches 100 runs"),
    ("implant",    "💠 Implant",        "Special implant/player-trait moment"),
    # Existing chat-animation events retained for backwards compatibility.
    ("hattrick",   "🎯 Hat-trick",      "Bowler takes 3 wickets in 3 balls"),
    ("maiden_over","🎯 Maiden Over",    "Bowler bowls a maiden over"),
    # Milestone keys fired by the over-by-over engine (services/milestones.py).
    ("three_fer",      "🎳 Three-fer",     "Bowler takes a 3rd wicket"),
    ("five_fer",       "🏅 Five-wicket haul", "Bowler takes a 5th wicket"),
    ("team_100",       "💯 Team 100",      "Team total passes 100"),
    ("team_150",       "🔢 Team 150",      "Team total passes 150"),
    ("team_200",       "🚀 Team 200",      "Team total passes 200"),
    ("partnership_50", "🤝 50 Partnership", "A stand reaches 50"),
    ("partnership_100","🤝 100 Partnership","A stand reaches 100"),
    ("match_won",      "🏆 Match Won",     "A side wins the match"),
    ("impact_player",  "🔄 Impact Player", "A team uses its Impact Player"),
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


# Placeholders an admin may use in a caption. Substituted with str.replace and
# NOT str.format — admin-written text may contain stray braces, which would make
# format() raise, and a milestone celebration must never break a live match.
# This mirrors services.commentary_service._render for the same reason.
CAPTION_FIELDS = (
    "player", "team", "opponent", "runs", "balls", "score", "overs",
    "bowler", "figures", "wickets", "partnership", "margin",
)


def render_caption(text, fields=None):
    """Fill an admin caption's placeholders. Unknown ones are left as written."""
    out = text or ""
    for key, value in (fields or {}).items():
        out = out.replace("{" + str(key) + "}", str(value))
    return out.strip()


def _send_kind(pick):
    """Which Telegram send method suits this row.

    Defaults to send_animation — what every pre-caption row was sent with — so
    existing media keeps behaving exactly as before.
    """
    source = str(getattr(pick, "source", "") or "").lower()
    media_type = (getattr(pick, "media_type", "") or "").strip().lower()
    if source.endswith((".mp4", ".mov")) or media_type == "video":
        return "video"
    if source.endswith((".jpg", ".jpeg", ".png", ".webp")) or media_type == "photo":
        return "photo"
    return "animation"


def _on_cooldown(ctx, chat_id, event_key):
    """Return True if we just fired this event in this chat recently."""
    key = f"emcd_{chat_id}_{event_key}"
    last = ctx.bot_data.get(key, 0)
    now = time.time()
    if now - last < COOLDOWN_SECONDS:
        return True
    ctx.bot_data[key] = now
    return False


async def fire_event_media(context, chat_id, event_key, fields=None,
                           cooldown=True):
    """Send the configured celebration for ``event_key`` to the chat.

    A row may carry media, a caption, or both:
      * media + caption -> the clip/photo with the caption under it
      * media only      -> what this has always done
      * caption only    -> a plain text message, so an admin can configure a
                           milestone message without having to find an image

    ``fields`` fills the caption's placeholders (see CAPTION_FIELDS).
    ``cooldown=False`` is for milestones, which are rare and must not be
    swallowed by the anti-spam window meant for 6-6-6 overs.

    Silent on any error — never break the match flow.
    """
    if not event_key:
        return

    if cooldown and _on_cooldown(context, chat_id, event_key):
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
            caption = render_caption(getattr(pick, "caption", None), fields)
            kind = _send_kind(pick)
        finally:
            session.close()
    except Exception:
        logger.exception(f"fire_event_media({event_key}) DB error")
        return

    # A caption with no media is still worth sending.
    if not source:
        if caption:
            try:
                await context.bot.send_message(chat_id=chat_id, text=caption,
                                               parse_mode="HTML")
            except Exception:
                logger.exception(f"caption-only send failed for event {event_key}")
        return

    send = {
        "photo": context.bot.send_photo,
        "video": context.bot.send_video,
        "animation": context.bot.send_animation,
    }[kind]
    arg = {"photo": "photo", "video": "video", "animation": "animation"}[kind]
    kwargs = {"chat_id": chat_id}
    if caption:
        kwargs["caption"] = caption
        kwargs["parse_mode"] = "HTML"

    try:
        if source_type in ("url", "telegram"):
            # 'url'      -> Telegram fetches it directly
            # 'telegram' -> source IS a file_id from our storage channel
            await send(**{arg: source}, **kwargs)
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
                await send(**{arg: f}, **kwargs)
    except Exception:
        logger.exception(f"send_{kind} failed for event {event_key}")


def detect_media_dimensions(file_bytes, filename):
    """Return (width, height, media_type) for uploaded media when detectable."""
    ext = os.path.splitext(filename or "")[1].lower()
    media_type = "video" if ext == ".mp4" else "image"
    if ext in (".gif", ".webp", ".png", ".jpg", ".jpeg"):
        try:
            from io import BytesIO
            from PIL import Image
            image = Image.open(BytesIO(file_bytes))
            return int(image.width or 0), int(image.height or 0), media_type
        except Exception:
            logger.exception("Could not detect uploaded media dimensions")
    return None, None, media_type


def normalize_size_mode(value):
    return "fixed_16_9" if value == "fixed_16_9" else "original"


def clamp_mobile_width(value):
    try:
        return max(240, min(640, int(value)))
    except (TypeError, ValueError):
        return 440


def miniapp_event_gifs(rows):
    """Serialize enabled EventMedia rows for the browser MiniApp."""
    result = {}
    for row in rows:
        if not row.enabled:
            continue
        key = "century" if row.event_key == "hundred" else row.event_key
        width = int(row.original_width or 0)
        height = int(row.original_height or 0)
        source = str(row.source or "")
        kind = row.media_type or ("video" if source.lower().endswith(".mp4") else "image")
        if row.source_type == "telegram" and not row.media_type:
            kind = "video" if source.lower().endswith(".mp4") else "image"
        result.setdefault(key, []).append({
            "id": row.id,
            "url": f"/api/event-media/{row.id}",
            "durationMs": max(500, min(15000, int(row.duration_ms or 3000))),
            "weight": max(1, int(row.weight or 1)),
            "kind": kind,
            "label": row.label or key.replace("_", " ").title(),
            "sizeMode": normalize_size_mode(row.size_mode),
            "width": width if width > 0 else None,
            "height": height if height > 0 else None,
            "maxMobileWidth": clamp_mobile_width(row.max_mobile_width),
        })
    return result


_miniapp_cache = {"expires": 0, "payload": {}}


@perf_timed("get_miniapp_event_gifs")
def get_miniapp_event_gifs(session, ttl_seconds=5):
    """Return enabled MiniApp GIF settings with a short poll-friendly cache."""
    now = time.time()
    if now < _miniapp_cache["expires"]:
        return _miniapp_cache["payload"]
    from models import EventMedia
    payload = miniapp_event_gifs(
        session.query(EventMedia).filter(EventMedia.enabled == True).all())
    _miniapp_cache.update(expires=now + ttl_seconds, payload=payload)
    return payload


def invalidate_miniapp_event_gifs():
    _miniapp_cache.update(expires=0, payload={})


def optimize_event_media_upload(file_bytes, filename, max_width=960, max_height=540):
    """Best-effort resize/optimization for uploaded animated GIFs."""
    if not (filename or "").lower().endswith(".gif"):
        return file_bytes
    try:
        from io import BytesIO
        from PIL import Image, ImageSequence
        image = Image.open(BytesIO(file_bytes))
        ratio = min(1.0, max_width / image.width, max_height / image.height)
        frames, durations = [], []
        for frame in ImageSequence.Iterator(image):
            rendered = frame.convert("RGBA")
            if ratio < 1.0:
                rendered = rendered.resize((max(1, round(image.width * ratio)), max(1, round(image.height * ratio))), Image.Resampling.LANCZOS)
            frames.append(rendered)
            durations.append(frame.info.get("duration", image.info.get("duration", 80)))
        if not frames:
            return file_bytes
        output = BytesIO()
        frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=durations, loop=image.info.get("loop", 0), optimize=True, disposal=2)
        optimized = output.getvalue()
        return optimized if optimized and len(optimized) < len(file_bytes) else file_bytes
    except Exception:
        logger.exception("GIF optimization failed; keeping original upload")
        return file_bytes


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


def upload_photo_to_storage_channel(file_bytes, filename, caption=None):
    """Upload an image to the storage channel via send_photo and return its
    file_id. Unlike :func:`upload_to_storage_channel` (which uses
    send_animation/send_document), this uses send_photo so the file_id can be
    re-sent as a proper Telegram photo (for the /CMUshop carousel).

    Synchronous — spins up a fresh loop + Bot, safe to call from Flask.
    Returns {'success': True, 'file_id': '...', 'message_id': int} or
    {'success': False, 'error': '...'}.
    """
    import asyncio
    from config import BOT_TOKEN, MEDIA_STORAGE_CHAT_ID

    if not BOT_TOKEN:
        return {"success": False, "error": "BOT_TOKEN env var not set"}
    if not MEDIA_STORAGE_CHAT_ID:
        return {"success": False, "error": "MEDIA_STORAGE_CHAT_ID env var not set"}

    async def _do_upload():
        from telegram import Bot
        from io import BytesIO

        bot = Bot(token=BOT_TOKEN)
        async with bot:
            buf = BytesIO(file_bytes)
            buf.name = filename
            cap = (f"🛍️ CMU Shop: {filename}" if not caption else caption)[:1024]
            try:
                msg = await bot.send_photo(
                    chat_id=MEDIA_STORAGE_CHAT_ID, photo=buf, caption=cap)
                if msg.photo:
                    # Largest rendition is last.
                    return {"file_id": msg.photo[-1].file_id,
                            "message_id": msg.message_id}
                return {"error": "send_photo returned no file_id"}
            except Exception as e:
                return {"error": f"{type(e).__name__}: {e}"}

    try:
        result = asyncio.run(_do_upload())
    except RuntimeError as e:
        return {"success": False, "error": f"asyncio.run failed: {e}"}
    except Exception as e:
        return {"success": False, "error": f"upload failed: {e}"}

    if result.get("error"):
        return {"success": False, "error": result["error"]}
    return {"success": True, "file_id": result["file_id"],
            "message_id": result["message_id"]}

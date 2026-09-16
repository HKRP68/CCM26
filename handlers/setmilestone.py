"""/setmilestone — configure in-match milestone messages and media from Telegram.

The website (/media) is the full editor; this is the phone-friendly half, for
setting a milestone's message or swapping its clip without opening a browser.
Both write the same ``EventMedia`` rows.

Built from CallbackQueryHandler + ``context.user_data`` rather than a
ConversationHandler, because this codebase has none — every multi-step flow
(trade, draft, XI picking) is built this way.

Uploads go to the storage channel so what is stored is a durable Telegram
file_id: the host filesystem is wiped on deploy.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from models import EventMedia
from services.admin_ids import is_admin
from services.event_media_service import (
    EVENT_KEYS, CAPTION_FIELDS, invalidate_miniapp_event_gifs,
)

logger = logging.getLogger(__name__)

CB = "msm"
# What the flow is waiting for, parked in user_data between messages.
AWAIT_KEY = "msm_await"


def _is_admin_update(update):
    user = update.effective_user
    return bool(user and is_admin(user.id))


def _keys_keyboard():
    rows, row = [], []
    for key, label, _desc in EVENT_KEYS:
        row.append(InlineKeyboardButton(label, callback_data=f"{CB}_k_{key}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    return rows


def _key_meta(event_key):
    return next((m for m in EVENT_KEYS if m[0] == event_key), None)


def _summarise(event_key):
    """Current state of one event key, as (text, keyboard)."""
    meta = _key_meta(event_key)
    if not meta:
        return "Unknown milestone.", []
    _k, label, desc = meta
    db = get_session()
    try:
        rows = (db.query(EventMedia)
                .filter(EventMedia.event_key == event_key)
                .order_by(EventMedia.enabled.desc(), EventMedia.id.desc())
                .all())
        enabled = [r for r in rows if r.enabled]
        captions = [r.caption for r in rows if (r.caption or "").strip()]
        lines = [f"<b>{label}</b>", f"<i>{desc}</i>", ""]
        lines.append(f"🎬 Media: {len(enabled)} enabled / {len(rows)} total")
        if captions:
            preview = captions[0]
            if len(preview) > 220:
                preview = preview[:220] + "…"
            lines.append(f"💬 Message: {preview}")
        else:
            lines.append("💬 Message: <i>none set</i>")
        lines.append("")
        lines.append("Placeholders: " + ", ".join(
            "{" + f + "}" for f in CAPTION_FIELDS))
    finally:
        db.close()
    kb = [
        [InlineKeyboardButton("💬 Set message", callback_data=f"{CB}_msg_{event_key}")],
        [InlineKeyboardButton("🎬 Upload media", callback_data=f"{CB}_up_{event_key}")],
        [InlineKeyboardButton("🗑 Clear message", callback_data=f"{CB}_clr_{event_key}")],
        [InlineKeyboardButton("⬅️ All milestones", callback_data=f"{CB}_list")],
    ]
    return "\n".join(lines), kb


async def setmilestone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setmilestone — list the configurable in-match moments."""
    msg = update.effective_message
    if not msg:
        return
    if not _is_admin_update(update):
        await msg.reply_text("That command is bot-admin only.")
        return
    context.user_data.pop(AWAIT_KEY, None)
    await msg.reply_text(
        "🏆 <b>Milestone Messages</b>\n\n"
        "Pick a moment to set its message or media. These fire during "
        "over-by-over matches (/letsplay and Challenge League).",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(_keys_keyboard()))


async def milestone_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _is_admin_update(update):
        await q.answer("Bot admins only.", show_alert=True)
        return
    data = q.data or ""
    try:
        action = data.split("_")[1]
    except IndexError:
        await q.answer()
        return

    if action == "list":
        context.user_data.pop(AWAIT_KEY, None)
        await q.answer()
        await q.edit_message_text(
            "🏆 <b>Milestone Messages</b>\n\nPick a moment:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(_keys_keyboard()))
        return

    event_key = data.split("_", 2)[2] if data.count("_") >= 2 else ""
    if not _key_meta(event_key):
        await q.answer("Unknown milestone.", show_alert=True)
        return

    if action == "k":
        context.user_data.pop(AWAIT_KEY, None)
        text, kb = _summarise(event_key)
        await q.answer()
        await q.edit_message_text(text, parse_mode="HTML",
                                  reply_markup=InlineKeyboardMarkup(kb))
        return

    if action == "msg":
        context.user_data[AWAIT_KEY] = {"mode": "caption", "event_key": event_key}
        await q.answer()
        await q.edit_message_text(
            f"💬 Send the message for <b>{_key_meta(event_key)[1]}</b>.\n\n"
            f"HTML is allowed. Placeholders: "
            + ", ".join("{" + f + "}" for f in CAPTION_FIELDS)
            + "\n\nSend /cancel to stop.", parse_mode="HTML")
        return

    if action == "up":
        context.user_data[AWAIT_KEY] = {"mode": "media", "event_key": event_key}
        await q.answer()
        await q.edit_message_text(
            f"🎬 Send the GIF, photo or video for "
            f"<b>{_key_meta(event_key)[1]}</b>.\n\nSend /cancel to stop.",
            parse_mode="HTML")
        return

    if action == "clr":
        db = get_session()
        try:
            rows = db.query(EventMedia).filter(
                EventMedia.event_key == event_key).all()
            for r in rows:
                r.caption = None
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("clearing milestone caption failed")
            await q.answer("Could not clear the message.", show_alert=True)
            return
        finally:
            db.close()
        _invalidate()
        await q.answer("Message cleared.")
        text, kb = _summarise(event_key)
        await q.edit_message_text(text, parse_mode="HTML",
                                  reply_markup=InlineKeyboardMarkup(kb))


def _invalidate():
    try:
        invalidate_miniapp_event_gifs()
    except Exception:
        logger.debug("event gif cache invalidation failed", exc_info=True)


async def milestone_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catch the admin's next message when /setmilestone is waiting for one.

    Returns True when it consumed the message, so the router can stop.
    """
    pending = (context.user_data or {}).get(AWAIT_KEY)
    if not pending or not _is_admin_update(update):
        return False
    msg = update.effective_message
    if not msg:
        return False
    text = (msg.text or "").strip()
    if text.lower() in ("/cancel", "cancel"):
        context.user_data.pop(AWAIT_KEY, None)
        await msg.reply_text("Cancelled.")
        return True

    event_key = pending.get("event_key")
    if pending.get("mode") == "caption":
        if not text:
            await msg.reply_text("Send the message as text, or /cancel.")
            return True
        _set_caption(event_key, msg.text_html or text)
        context.user_data.pop(AWAIT_KEY, None)
        _invalidate()
        await msg.reply_text("✅ Milestone message saved.")
        return True

    if pending.get("mode") == "media":
        stored = _store_media_from_message(msg, event_key)
        if not stored:
            await msg.reply_text("Send a GIF, photo or video — or /cancel.")
            return True
        context.user_data.pop(AWAIT_KEY, None)
        _invalidate()
        await msg.reply_text("✅ Milestone media saved.")
        return True
    return False


def _set_caption(event_key, caption):
    """Write the caption onto this key's rows, creating a text-only row if none.

    A caption with no media is valid — fire_event_media sends it as a plain
    message — so an admin can configure a milestone without finding an image.
    """
    db = get_session()
    try:
        rows = db.query(EventMedia).filter(
            EventMedia.event_key == event_key).all()
        if rows:
            for r in rows:
                r.caption = caption
        else:
            db.add(EventMedia(event_key=event_key, source_type="none",
                              source="", caption=caption, weight=1,
                              enabled=True, label="Message only"))
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("saving milestone caption failed")
    finally:
        db.close()


def _store_media_from_message(msg, event_key):
    """Persist an uploaded animation/photo/video as a durable Telegram file_id."""
    file_id, media_type = None, "image"
    if msg.animation:
        file_id, media_type = msg.animation.file_id, "image"
    elif msg.photo:
        file_id, media_type = msg.photo[-1].file_id, "photo"
    elif msg.video:
        file_id, media_type = msg.video.file_id, "video"
    elif msg.document:
        file_id, media_type = msg.document.file_id, "image"
    if not file_id:
        return False

    db = get_session()
    try:
        # Carry the existing message across so uploading a clip does not silently
        # wipe a caption the admin set earlier.
        existing = (db.query(EventMedia)
                    .filter(EventMedia.event_key == event_key)
                    .order_by(EventMedia.id.desc()).first())
        db.add(EventMedia(
            event_key=event_key, source_type="telegram", source=file_id,
            caption=(existing.caption if existing else None),
            media_type=media_type, weight=1, enabled=True,
            label="Uploaded from Telegram"))
        db.commit()
        return True
    except Exception:
        db.rollback()
        logger.exception("saving milestone media failed")
        return False
    finally:
        db.close()

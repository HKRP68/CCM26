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
from html import escape as _esc

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
    # A way out of the command from every screen — the flow parks state in
    # user_data, so an admin who opened it by accident needs something to press
    # rather than having to remember /cancel.
    rows.append([InlineKeyboardButton("✖️ Close", callback_data=f"{CB}_x_")])
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
            # Escaped, not rendered: a caption is admin-written HTML, and
            # truncating it mid-tag would make this whole screen fail to send.
            # Showing the source is also what an admin about to rewrite it wants.
            preview = captions[0]
            if len(preview) > 220:
                preview = preview[:220] + "…"
            lines.append(f"💬 Message: <code>{_esc(preview)}</code>")
            # Each clip carries its own caption, so say when they differ —
            # otherwise the one preview above reads as "the" message.
            if len(set(captions)) > 1:
                lines.append(f"   <i>…and {len(set(captions)) - 1} other "
                             f"message(s) across this milestone's clips.</i>")
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
        [InlineKeyboardButton("⬅️ All milestones", callback_data=f"{CB}_list"),
         InlineKeyboardButton("✖️ Close", callback_data=f"{CB}_x_")],
    ]
    return "\n".join(lines), kb


def _back_keyboard(event_key):
    """Way out of a prompt that is waiting on the admin's next message.

    Back drops the pending wait and returns to the milestone; Close leaves the
    command entirely. Either way ``AWAIT_KEY`` is cleared, so the admin's next
    message is not swallowed by a flow they thought they had left.
    """
    return [[InlineKeyboardButton("⬅️ Back", callback_data=f"{CB}_k_{event_key}"),
             InlineKeyboardButton("✖️ Cancel", callback_data=f"{CB}_x_")]]


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

    # Close carries no event key, so it has to be answered before the lookup
    # below — which would otherwise reject it as an unknown milestone.
    if action == "x":
        context.user_data.pop(AWAIT_KEY, None)
        await q.answer("Closed.")
        try:
            await q.edit_message_text(
                "✖️ <b>Milestone Messages closed.</b>\n\n"
                "<i>Run /setmilestone again whenever you need it.</i>",
                parse_mode="HTML")
        except Exception:
            logger.debug("closing the milestone menu failed", exc_info=True)
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
            "This sets the text on <b>every</b> clip for this milestone. To "
            "give one clip its own line, send it as the caption when you "
            "upload it.\n\n"
            f"HTML is allowed. Placeholders: "
            + ", ".join("{" + f + "}" for f in CAPTION_FIELDS)
            + "\n\nSend /cancel to stop.", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(_back_keyboard(event_key)))
        return

    if action == "up":
        context.user_data[AWAIT_KEY] = {"mode": "media", "event_key": event_key}
        await q.answer()
        await q.edit_message_text(
            f"🎬 Send the GIF, photo or video for "
            f"<b>{_key_meta(event_key)[1]}</b>.\n\n"
            "💬 <b>Add a caption to it</b> and that text becomes this "
            "milestone's message, sent under the clip. HTML is allowed. "
            "Placeholders: "
            + ", ".join("{" + f + "}" for f in CAPTION_FIELDS)
            + "\n\nSend the clip with no caption to keep the message you "
            "already have.\n\nSend /cancel to stop.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(_back_keyboard(event_key)))
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
        stored, caption_set = _store_media_from_message(msg, event_key)
        if not stored:
            await msg.reply_text("Send a GIF, photo or video — or /cancel.")
            return True
        context.user_data.pop(AWAIT_KEY, None)
        _invalidate()
        await msg.reply_text(
            "✅ Milestone media saved, with the caption you sent as its "
            "message." if caption_set else "✅ Milestone media saved.")
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
    """Persist an uploaded animation/photo/video as a durable Telegram file_id.

    A caption sent **with** the clip becomes that clip's own message, shown
    under it when the milestone fires — admins compose the two together in
    Telegram, so a GIF with text on it should arrive in one message rather than
    needing a second trip through "Set message". Captions are per row, so two
    clips on one milestone can carry different lines; ``fire_event_media`` picks
    a row and sends that row's caption.

    An uncaptioned upload inherits the message already configured for the key,
    so swapping a clip never silently drops the text.

    Returns ``(stored, caption_set)``.
    """
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
        return False, False

    # caption_html keeps the bold/italic/links the admin typed in Telegram —
    # fire_event_media sends captions with parse_mode="HTML", so the plain text
    # would otherwise arrive stripped of every entity they applied.
    caption = (getattr(msg, "caption_html", None) or msg.caption or "").strip()

    db = get_session()
    try:
        # Carry the existing message across so uploading a clip does not silently
        # wipe a caption the admin set earlier.
        existing = (db.query(EventMedia)
                    .filter(EventMedia.event_key == event_key)
                    .order_by(EventMedia.id.desc()).first())
        db.add(EventMedia(
            event_key=event_key, source_type="telegram", source=file_id,
            caption=(caption or (existing.caption if existing else None)),
            media_type=media_type, weight=1, enabled=True,
            label="Uploaded from Telegram"))
        db.commit()
        return True, bool(caption)
    except Exception:
        db.rollback()
        logger.exception("saving milestone media failed")
        return False, False
    finally:
        db.close()

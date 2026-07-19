"""/CMUshop — a website-managed image gallery shown in the bot.

Admins add/remove images and set ONE shared caption from the website
(admin_cmushop page). Behaviour in chat:
  • no active images → a friendly "empty" reply
  • exactly one image → a single photo, no buttons
  • several images   → a photo with ◀️ Prev | i/N | Next ▶️ carousel buttons

The carousel is stateless: the callback re-reads the active images from the DB
by index (wrapping modulo N), so it keeps working across bot restarts and
website edits. The shared caption comes from GameConfig.cmushop_caption.
"""

import logging

from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      InputMediaPhoto)
from telegram.ext import ContextTypes

from database import get_session
from models import CMUShopImage

logger = logging.getLogger(__name__)


def _load_shop():
    """Return (list_of_tg_file_ids, caption). Only active images with a
    Telegram file_id are usable in chat."""
    session = get_session()
    try:
        rows = (session.query(CMUShopImage)
                .filter(CMUShopImage.is_active == True,  # noqa: E712
                        CMUShopImage.tg_file_id.isnot(None))
                .order_by(CMUShopImage.sort_order, CMUShopImage.id)
                .all())
        file_ids = [r.tg_file_id for r in rows]
    finally:
        session.close()

    caption = ""
    try:
        from services.config_service import get_config
        caption = (get_config().get("cmushop_caption") or "").strip()
    except Exception:
        logger.exception("Failed to load cmushop caption")
    return file_ids, caption


def _keyboard(index: int, total: int) -> InlineKeyboardMarkup:
    prev_i = (index - 1) % total
    next_i = (index + 1) % total
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("◀️ Prev", callback_data=f"cmushop:{prev_i}"),
        InlineKeyboardButton(f"{index + 1}/{total}", callback_data="noop"),
        InlineKeyboardButton("Next ▶️", callback_data=f"cmushop:{next_i}"),
    ]])


async def cmushop_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    file_ids, caption = _load_shop()

    if not file_ids:
        await update.message.reply_text(
            "🛍️ The shop is empty right now — check back soon!")
        return

    cap = caption or None
    if len(file_ids) == 1:
        await update.effective_chat.send_photo(
            photo=file_ids[0], caption=cap, parse_mode="HTML")
        return

    await update.effective_chat.send_photo(
        photo=file_ids[0], caption=cap, parse_mode="HTML",
        reply_markup=_keyboard(0, len(file_ids)))


async def cmushop_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    try:
        index = int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        await query.answer()
        return

    file_ids, caption = _load_shop()
    if not file_ids:
        await query.answer("The shop is empty now.", show_alert=True)
        return

    total = len(file_ids)
    index %= total  # wrap + guard against a shrunk gallery
    cap = caption or None

    await query.answer()
    try:
        await query.edit_message_media(
            media=InputMediaPhoto(media=file_ids[index], caption=cap,
                                  parse_mode="HTML"),
            reply_markup=_keyboard(index, total),
        )
    except Exception:
        # Telegram raises if the media is identical — ignore silently.
        logger.debug("cmushop edit_message_media no-op", exc_info=True)

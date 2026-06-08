"""/modes command: reply-based league selection prompt."""

from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import ModeLeague
from services.modes_service import (
    DEFAULT_MODES_INTRO,
    build_modes_keyboard,
    get_active_mode_leagues,
    get_modes_config,
)


async def modes_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    host = update.effective_user
    if not message or not host:
        return

    replied = message.reply_to_message
    if not replied or not replied.from_user:
        await message.reply_text(
            "🏏 <b>Reply to another user's message with /modes.</b>\n\n"
            "Example: Host replies to Guest's message and sends <code>/modes</code>.",
            parse_mode="HTML",
        )
        return

    guest = replied.from_user
    if guest.id == host.id:
        await message.reply_text(
            "⚠️ Reply to the other player's message before using /modes.",
            parse_mode="HTML",
        )
        return
    if getattr(guest, "is_bot", False):
        await message.reply_text(
            "⚠️ /modes needs a real guest player, not a bot message.",
            parse_mode="HTML",
        )
        return

    db = get_session()
    try:
        cfg = get_modes_config(db)
        leagues = get_active_mode_leagues(db)
        if not leagues:
            await message.reply_text(
                "⚠️ No active leagues are configured yet. Ask the owner to add leagues in the admin panel.",
                parse_mode="HTML",
            )
            return

        host_name = host.mention_html()
        guest_name = guest.mention_html()
        intro = (cfg.modes_intro_text or DEFAULT_MODES_INTRO).strip()
        caption = f"{intro}\n\n👤 Host: {host_name}\n🎯 Guest: {guest_name}"
        keyboard = build_modes_keyboard(leagues, host.id, guest.id)
        banner_url = (cfg.modes_banner_image_url or "").strip()

        if banner_url:
            await message.reply_photo(
                photo=banner_url,
                caption=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        else:
            await message.reply_text(
                caption,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
    finally:
        db.close()


async def mode_league_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    parts = (query.data or "").split(":")
    if len(parts) != 4:
        await query.answer("Invalid mode selection.", show_alert=True)
        return

    _, league_id, host_tg, guest_tg = parts
    clicker = query.from_user
    if str(clicker.id) not in {host_tg, guest_tg}:
        await query.answer("Only the host or guest can choose this league.", show_alert=True)
        return

    db = get_session()
    try:
        league = db.query(ModeLeague).get(int(league_id))
        if not league or not league.is_active:
            await query.answer("This league is no longer available.", show_alert=True)
            return
        await query.answer(f"Selected {league.name}", show_alert=False)
        selected_text = (
            f"🏏 <b>{league.name}</b> mode selected by {clicker.mention_html()}."
        )
        if league.image_url:
            await query.message.reply_photo(
                photo=league.image_url,
                caption=selected_text,
                parse_mode="HTML",
            )
        else:
            await query.message.reply_text(
                selected_text,
                parse_mode="HTML",
            )
    finally:
        db.close()

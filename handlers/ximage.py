"""Handler for /ximage — render the user's Playing XI as a single image.

Additive companion to the text /pxi (handlers/lineup.playingxi_handler). The XI
cards are produced by the existing card generator, so the image uses the bot's
real cards and card design; only the surrounding frame is new.
"""

import asyncio
import io
import logging
from html import escape

from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from handlers.lineup import (_get_ordered_roster, _build_display_order,
                             format_xi_text)
from services.telegram_user_service import (resolve_command_target,
                                            sync_telegram_user)
from services.xi_image import build_xi_image

logger = logging.getLogger(__name__)


async def ximage_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Render the caller's (or a targeted user's) Playing XI as an image.

    Falls back to the text XI (``format_xi_text``) if image generation fails, so
    the command never hard-errors for a user with a valid 11-player roster.
    """
    tg_user = update.effective_user
    session = get_session()
    try:
        # Optional: view another user's XI by arg, reply, or mention, like /pxi.
        # resolve_command_target handles the no-args reply/mention case and
        # returns reason "missing" when no target was attempted at all.
        target_user, target_source = resolve_command_target(
            session, update, context, "ximage")
        if not target_user and target_source != "missing":
            if target_source == "not_mention":
                await update.message.reply_text(
                    "❌ Reply to a user or use a real @username mention.")
            else:
                await update.message.reply_text(
                    "❌ User not found. If they changed or don't have a "
                    "username, reply to their message and run /ximage.")
            return

        viewer = sync_telegram_user(session, tg_user)
        if not viewer:
            await update.message.reply_text("❌ Do /debut first!")
            return

        view_user = target_user or viewer
        roster = _get_ordered_roster(session, view_user.id)

        if len(roster) < 11:
            who = f"@{view_user.username}" if target_user else "You"
            verb = "has" if target_user else "have"
            await update.message.reply_text(
                f"❌ {who} {verb} only <b>{len(roster)}</b> player(s). "
                f"Need at least 11 to form a Playing XI.\n"
                f"Get more with /claim, /gspin, /buypl, or /buypack.",
                parse_mode="HTML")
            return

        display = _build_display_order(roster)
        xi_pairs = display[:11]

        handle = (f"@{view_user.username}" if view_user.username
                  else (view_user.team_name or view_user.first_name))
        # team_name/first_name are user-controlled — escape before sending in any
        # parse_mode="HTML" text. (The raw handle is fine for the PIL image, which
        # draws plain text.)
        safe_handle = escape(handle or "Unknown")
        captain_rid = view_user.captain_roster_id

        total_ovr = sum(p.rating for _, p in xi_pairs)
        avg_ovr = round(total_ovr / 11, 1)

        await update.message.reply_chat_action("upload_photo")

        # Pillow is CPU-bound — render off the event loop. Keep the ORM objects
        # un-expired during the threaded render (SessionLocal defaults to
        # expire_on_commit=True), so commit only AFTER rendering finishes.
        png = await asyncio.to_thread(
            build_xi_image, xi_pairs,
            team_name=handle, captain_roster_id=captain_rid)
        session.commit()

        if png:
            caption = (
                f"🏏 <b>PLAYING XI — {safe_handle}</b>\n"
                f"📊 <b>XI Rating:</b> <code>{total_ovr} OVR</code> "
                f"(avg <b>{avg_ovr}</b>)\n\n"
                f"💡 <i>Use /swap to reorder · /setcaptain to set captain.</i>")
            await update.message.reply_photo(
                photo=io.BytesIO(png), caption=caption, parse_mode="HTML")
            return

        # Image render failed — never hard-fail; fall back to the text XI.
        logger.warning("build_xi_image returned None; falling back to text XI")
        text = format_xi_text(roster, safe_handle, captain_rid, show_bench=False,
                              origin_chat_id=update.effective_chat.id)
        await update.message.reply_text(text, parse_mode="HTML",
                                        disable_web_page_preview=True)

    except Exception:
        logger.exception("ximage_handler error")
        await update.message.reply_text(
            "⚠️ Couldn't build the Playing XI image. Try /pxi.")
    finally:
        session.close()

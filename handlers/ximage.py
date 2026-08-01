"""Handler for /ximage — render the user's Playing XI as a single image.

Additive companion to the text /pxi (handlers/lineup.playingxi_handler). The XI
cards are produced by the existing card generator, so the image uses the bot's
real cards and card design; only the surrounding frame is new.
"""

import asyncio
import io
import logging
from datetime import datetime
from html import escape

from telegram import Update
from telegram.ext import ContextTypes

from config import XIMAGE_COOLDOWN
from database import get_session
from models import UserStats
from handlers.lineup import (_get_ordered_roster, _build_display_order,
                             format_xi_text)
from services.cooldown_service import check_cooldown, format_remaining
from services.telegram_user_service import (resolve_command_target,
                                            sync_telegram_user)
from services.xi_image import build_xi_image
from utils.idempotency import claim_once, release

logger = logging.getLogger(__name__)

# Guard TTL: comfortably longer than a render + upload so a duplicate in-flight
# /ximage from the same user is blocked, but a legitimate later call isn't.
_XIMAGE_GUARD_TTL = 60.0

# Process-wide serialization of the (CPU-bound, thread-offloaded) XI render.
# The bot runs handlers across a thread pool (concurrent_updates), so two users
# firing /ximage at the same instant would each spawn a Pillow render on its own
# thread. Those renders share process-global PIL state (notably the cached
# TrueType font objects, which are NOT thread-safe to draw with concurrently),
# which could corrupt one render or make both messages carry the same picture.
# A single asyncio lock queues the renders so each caller waits its turn and
# always gets its OWN XI back. Renders are ~sub-second, so the wait is tiny.
_XIMAGE_RENDER_LOCK = asyncio.Lock()


def _stamp_cooldown(user_id):
    """Record that ``user_id`` just spent their /ximage render (blocking).

    Deliberately its own short-lived session: the caller has already released
    the one it used for reads so the render didn't hold a pooled connection.
    """
    session = get_session()
    try:
        stats = session.query(UserStats).filter(
            UserStats.user_id == user_id).first()
        if not stats:
            stats = UserStats(user_id=user_id)
            session.add(stats)
        stats.last_ximage = datetime.utcnow()
        session.commit()
    except Exception:
        logger.exception("ximage cooldown stamp failed for user %s", user_id)
        session.rollback()
    finally:
        session.close()


async def ximage_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Render the caller's (or a targeted user's) Playing XI as an image.

    Falls back to the text XI (``format_xi_text``) if image generation fails, so
    the command never hard-errors for a user with a valid 11-player roster.
    """
    tg_user = update.effective_user
    session = get_session()
    guard_key = None
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

        # 1-hour cooldown on the (CPU-heavy) image render, per caller. Applies
        # regardless of whose XI is being viewed so the command can't be spammed.
        stats = session.query(UserStats).filter(
            UserStats.user_id == viewer.id).first()
        if not stats:
            stats = UserStats(user_id=viewer.id)
            session.add(stats)
            session.flush()
        from services.command_config_service import get_cooldown
        effective_cooldown = get_cooldown(session, "ximage", XIMAGE_COOLDOWN)
        ready, remaining = check_cooldown(stats, "last_ximage", effective_cooldown)
        if not ready:
            await update.message.reply_text(
                f"⏳ <b>/ximage</b> is on cooldown.\n"
                f"Try again in <b>{format_remaining(remaining)}</b>.\n"
                f"<i>Tip: /pxi shows your Playing XI as text anytime.</i>",
                parse_mode="HTML")
            return

        # Serialize concurrent renders from the same caller: the bot runs
        # handlers across a thread pool (concurrent_updates), so two /ximage sent
        # back-to-back could both pass the cooldown gate above and each kick off
        # the CPU-heavy Pillow render. Hold a short in-process guard for the
        # render window so only the first one proceeds.
        guard_key = f"ximage_{viewer.id}"
        if not claim_once(guard_key, ttl=_XIMAGE_GUARD_TTL):
            guard_key = None  # not ours — don't release someone else's claim
            await update.message.reply_text(
                "⏳ Your Playing XI image is already being generated — hang tight.")
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
        viewer_id = viewer.id

        await update.message.reply_chat_action("upload_photo")

        # Everything below reads only already-loaded columns, so hand the
        # pooled connection back BEFORE the slow part. Rendering eleven cards
        # can take minutes when the host's ephemeral disk has been wiped and
        # each card has to be restored from Telegram storage first, and this
        # handler used to hold its session open across all of it — waiting for
        # the render lock, then the render itself, then committing. A
        # connection parked mid-transaction for that long gets closed by the
        # database, and the commit afterwards blocked on the dead socket. That
        # commit runs on the event loop, so it took the entire bot down with it
        # (see the 13:46–13:51 UTC freeze on 2026-08-01).
        #
        # close() detaches these instances but does not expire them, so the
        # render still sees every value loaded above. Commit first so the
        # username sync done by sync_telegram_user is not rolled back, with
        # expiry off so the commit itself doesn't blank the objects out.
        session.expire_on_commit = False
        session.commit()
        session.close()

        # Pillow is CPU-bound — render off the event loop. Serialize the render
        # process-wide (see _XIMAGE_RENDER_LOCK) so two users' concurrent
        # /ximage calls can't share PIL font state and end up both receiving the
        # same XI image.
        async with _XIMAGE_RENDER_LOCK:
            png = await asyncio.to_thread(
                build_xi_image, xi_pairs,
                team_name=handle, captain_roster_id=captain_rid)
        # Consume the cooldown now that the (expensive) render has run. Fresh
        # short-lived session, and off the event loop: if the database is having
        # the bad day described above, this stamp is allowed to be slow, but it
        # is not allowed to stop the bot answering everyone else.
        await asyncio.to_thread(_stamp_cooldown, viewer_id)

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
        # Release the render guard so a legitimate later /ximage isn't blocked
        # (the 1-hour cooldown still governs normal spacing).
        if guard_key:
            release(guard_key)
        session.close()

"""Handlers for /release and /releasemultiple commands."""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, Player, UserRoster
from config import get_sell_value
from services.roster_service import (
    find_roster_entry,
    release_player,
    get_duplicate_entries,
    release_duplicates,
)

logger = logging.getLogger(__name__)


# ── /release [player_name] ──────────────────────────────────────────

async def release_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    logger.info(f"/release from {tg_user.id}, args={context.args}")

    if not context.args:
        await update.message.reply_text(
            "Usage: /release <player name>\nExample: /release Virat Kohli"
        )
        return

    search_name = " ".join(context.args).strip()
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        entry, player = find_roster_entry(session, user.id, search_name)
        if not entry:
            await update.message.reply_text(f"❌ No player named '{search_name}' in your roster")
            return

        sell_val = get_sell_value(player.rating)

        text = (
            "🔴 <b>RELEASE PLAYER?</b>\n\n"
            f"📛 Player: {player.name}\n"
            f"⭐ Rating: {player.rating} OVR\n"
            f"🎯 Category: {player.category}\n\n"
            f"💸 You will receive: {sell_val:,} 🪙\n"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Confirm Release", callback_data=f"rlconfirm_{entry.id}"),
                InlineKeyboardButton("❌ Cancel", callback_data="rlcancel"),
            ]
        ])

        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

    except Exception:
        logger.exception(f"Release error for {tg_user.id}")
        await update.message.reply_text("⚠️ Database error. Please try again later.")
    finally:
        session.close()


async def release_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle release confirmation button."""
    query = update.callback_query
    await query.answer()
    tg_user = query.from_user

    roster_entry_id = int(query.data.split("_")[1])

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await query.edit_message_reply_markup(reply_markup=None)
            return

        result = release_player(session, user, roster_entry_id)
        session.commit()

        if not result["success"]:
            await query.edit_message_text(f"❌ {result['error']}")
            return

        text = (
            "✅ <b>PLAYER RELEASED!</b>\n\n"
            f"📛 {result['name']} - {result['rating']} OVR\n\n"
            f"💸 Received: {result['sell_value']:,} 🪙\n"
            f"💰 New Balance: {result['new_balance']:,}\n"
            f"📊 Roster: {result['new_count']}/25"
        )
        await query.edit_message_text(text, parse_mode="HTML")

    except Exception:
        session.rollback()
        logger.exception(f"Release confirm error for {tg_user.id}")
    finally:
        session.close()


async def release_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle release cancel button."""
    query = update.callback_query
    await query.answer("Release cancelled")
    try:
        await query.edit_message_text("❌ Release cancelled.")
    except Exception:
        pass


# ── /releasemultiple ─────────────────────────────────────────────────

async def releasemultiple_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    logger.info(f"/releasemultiple from {tg_user.id}")

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        dupes = get_duplicate_entries(session, user.id)

        if not dupes:
            await update.message.reply_text("📊 No duplicate players in your roster!")
            return

        lines = []
        buttons = []
        for i, (player, qty) in enumerate(dupes, 1):
            sell = get_sell_value(player.rating)
            lines.append(
                f"{i}. {player.name} - {player.rating} OVR (×{qty})\n"
                f"   💸 {sell:,} 🪙 each"
            )
            row = [
                InlineKeyboardButton(f"Release 1× {player.name[:15]}", callback_data=f"rldup_{player.id}_1"),
            ]
            if qty > 2:
                row.append(
                    InlineKeyboardButton(f"Release {qty-1}×", callback_data=f"rldup_{player.id}_{qty - 1}")
                )
            buttons.append(row)

        text = (
            f"📋 <b>YOUR DUPLICATE PLAYERS</b>\n\n"
            f"Found {len(dupes)} players owned multiple times:\n\n"
            + "\n\n".join(lines)
        )

        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="rlcancel")])
        keyboard = InlineKeyboardMarkup(buttons)

        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

    except Exception:
        logger.exception(f"ReleaseMultiple error for {tg_user.id}")
        await update.message.reply_text("⚠️ Database error. Please try again later.")
    finally:
        session.close()


async def release_dup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle releasing N duplicates of a specific player."""
    query = update.callback_query
    await query.answer()
    tg_user = query.from_user

    parts = query.data.split("_")  # rldup_{player_id}_{count}
    player_id = int(parts[1])
    count = int(parts[2])

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await query.edit_message_reply_markup(reply_markup=None)
            return

        result = release_duplicates(session, user, player_id, count)
        session.commit()

        if not result["success"]:
            await query.edit_message_text(f"❌ {result['error']}")
            return

        text = (
            "✅ <b>DUPLICATES RELEASED!</b>\n\n"
            f"📛 {result['name']} - {result['rating']} OVR\n"
            f"🔢 Released: {result['released_count']}×\n\n"
            f"💸 Total Received: {result['total_value']:,} 🪙\n"
            f"💰 New Balance: {result['new_balance']:,}\n"
            f"📊 Roster: {result['new_count']}/25"
        )
        await query.edit_message_text(text, parse_mode="HTML")

    except Exception:
        session.rollback()
        logger.exception(f"ReleaseDup error for {tg_user.id}")
    finally:
        session.close()

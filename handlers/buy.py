"""Handler for /buypl <player_name> — buy a player from the market."""

import io
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import ContextTypes

from database import get_session
from models import User, Player, UserRoster
from services.card_generator import generate_card
from config import get_buy_value, get_sell_value, MAX_ROSTER
from services.activity_service import log_activity
from services.card_text import format_player_card

logger = logging.getLogger(__name__)


def _buypl_versions_keyboard(versions, current_idx, owner_user_id, owner_tg_id):
    current = versions[current_idx]
    nav = []
    if current_idx > 0:
        nav.append(InlineKeyboardButton(
            "⬅️ Previous",
            callback_data=f"buypage_{owner_user_id}_{owner_tg_id}_{current_idx - 1}",
        ))
    nav.append(InlineKeyboardButton(
        f"{current_idx + 1}/{len(versions)}",
        callback_data="noop",
    ))
    if current_idx < len(versions) - 1:
        nav.append(InlineKeyboardButton(
            "Next ➡️",
            callback_data=f"buypage_{owner_user_id}_{owner_tg_id}_{current_idx + 1}",
        ))
    return InlineKeyboardMarkup([
        nav,
        [InlineKeyboardButton("💰 Buy", callback_data=f"buypl_{current.id}_{owner_user_id}_{owner_tg_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"buycancel_{owner_tg_id}")],
    ])


def _buypl_versions_text(player, user, index, total):
    version_name = player.version or "Base"
    return (
        f"🧾 <b>{player.name}</b> — <i>{version_name}</i>\n"
        f"📄 Card {index + 1}/{total}\n\n"
        + format_player_card(player)
        + "\n\n"
        + f"💳 Your Balance: {user.total_coins:,} 🪙"
    )


async def buypl_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user

    if not context.args:
        await update.message.reply_text("Usage: /buypl <player name>\nExample: /buypl Virat Kohli")
        return

    search = " ".join(context.args).strip()
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        if user.roster_count >= MAX_ROSTER:
            await update.message.reply_text("❌ Roster full (25/25)! Release players first.")
            return

        # Default to base card; fall back to any version
        player = (session.query(Player)
                  .filter(Player.name.ilike(f"%{search}%"),
                          Player.parent_player_id.is_(None),
                          Player.is_active == True).first())
        if not player:
            player = (session.query(Player)
                      .filter(Player.name.ilike(f"%{search}%"),
                              Player.is_active == True).first())
        if not player:
            await update.message.reply_text(f"❌ No player found matching '{search}'")
            return

        # Ownership check across all versions of this base
        from services.version_service import user_owns_any_version, get_all_versions
        if user_owns_any_version(session, user.id, player.id):
            base_id = player.parent_player_id or player.id
            versions = get_all_versions(session, base_id)
            owned_v = None
            owned_row = (session.query(UserRoster)
                         .filter(UserRoster.user_id == user.id,
                                 UserRoster.player_id.in_([v.id for v in versions])).first())
            if owned_row:
                owned_v = next((v for v in versions if v.id == owned_row.player_id), None)
            await update.message.reply_text(
                f"❌ You already own a version of <b>{player.name}</b>"
                + (f" (<i>{owned_v.version}</i>)" if owned_v else "")
                + ".\n\nYou can only own one version of any player at a time.",
                parse_mode="HTML",
            )
            return

        from services.version_service import get_all_versions
        base_id = player.parent_player_id or player.id
        versions = get_all_versions(session, base_id)
        versions = sorted(
            versions,
            key=lambda v: (0 if (v.version or "").strip().lower() == "base" else 1, v.id),
        )
        current_idx = next((i for i, v in enumerate(versions) if v.id == player.id), 0)
        context.bot_data[f"buypl_base_{tg_user.id}"] = versions[current_idx].id
        text = _buypl_versions_text(versions[current_idx], user, current_idx, len(versions))
        keyboard = _buypl_versions_keyboard(versions, current_idx, user.id, tg_user.id)

        card_bytes = generate_card(versions[current_idx])
        if card_bytes:
            sent = await update.message.reply_photo(
                photo=io.BytesIO(card_bytes), caption=text,
                parse_mode="HTML", reply_markup=keyboard,
            )
        else:
            sent = await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

        # 2-minute auto-cleanup
        try:
            from services.button_timeout import schedule_button_timeout
            schedule_button_timeout(context, sent.chat_id, sent.message_id, delay_seconds=120)
        except Exception:
            pass

    except Exception:
        logger.exception(f"BuyPl error for {tg_user.id}")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


async def buypl_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tg_user = query.from_user
    try:
        _, owner_user_id, owner_tg_id, idx = query.data.split("_")
        owner_user_id = int(owner_user_id)
        owner_tg_id = int(owner_tg_id)
        idx = int(idx)
    except (ValueError, IndexError):
        return
    if tg_user.id != owner_tg_id:
        await query.answer("This isn't your purchase view!", show_alert=True)
        return

    session = get_session()
    try:
        user = session.query(User).get(owner_user_id)
        if not user or user.telegram_id != owner_tg_id:
            return
        from services.version_service import get_all_versions
        seed_player_id = context.bot_data.get(f"buypl_base_{owner_tg_id}")
        if not seed_player_id:
            await query.edit_message_reply_markup(reply_markup=None)
            return
        seed = session.query(Player).get(seed_player_id)
        if not seed:
            return
        base_id = seed.parent_player_id or seed.id
        versions = get_all_versions(session, base_id)
        versions = sorted(
            versions,
            key=lambda v: (0 if (v.version or "").strip().lower() == "base" else 1, v.id),
        )
        if not versions:
            return
        idx = max(0, min(idx, len(versions) - 1))
        player = versions[idx]
        text = _buypl_versions_text(player, user, idx, len(versions))
        kb = _buypl_versions_keyboard(versions, idx, owner_user_id, owner_tg_id)
        card_bytes = generate_card(player)
        if card_bytes:
            await query.edit_message_media(
                media=InputMediaPhoto(media=io.BytesIO(card_bytes), caption=text, parse_mode="HTML"),
                reply_markup=kb,
            )
        else:
            await query.edit_message_caption(caption=text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        logger.exception("buypl_page_callback error")
    finally:
        session.close()


async def buypl_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tg_user = query.from_user

    parts = query.data.split("_")  # buypl_{player_id}_{user_id}
    player_id = int(parts[1])
    owner_user_id = int(parts[2])

    session = get_session()
    try:
        user = session.query(User).get(owner_user_id)
        if not user or user.telegram_id != tg_user.id:
            await query.edit_message_reply_markup(reply_markup=None)
            return

        player = session.query(Player).get(player_id)
        if not player:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("❌ Player no longer available")
            return

        buy_val = get_buy_value(player.rating)
        username = tg_user.username or tg_user.first_name

        if user.total_coins < buy_val:
            shortage = buy_val - user.total_coins
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                f"❌ @{username} needs {shortage:,} more coins to buy {player.name}\n"
                f"💰 Balance: {user.total_coins:,} | Price: {buy_val:,}"
            )
            return

        if user.roster_count >= MAX_ROSTER:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("❌ Roster full! Release players first.")
            return

        # Re-check version ownership (user might have bought another version since)
        from services.version_service import user_owns_any_version
        if user_owns_any_version(session, user.id, player.id):
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                f"❌ You already own a version of <b>{player.name}</b>.",
                parse_mode="HTML",
            )
            return

        # Buy the player
        user.total_coins -= buy_val
        entry = UserRoster(
            user_id=user.id, player_id=player.id,
            order_position=user.roster_count + 1,
            acquired_date=datetime.utcnow(),
        )
        session.add(entry)
        user.roster_count += 1

        log_activity(session, user.id, "buy",
                     f"Bought {player.name} ({player.rating} OVR) for {buy_val:,}",
                     coins_change=-buy_val,
                     player_name=player.name, player_rating=player.rating)
        session.commit()

        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            f"✅ <b>PURCHASED!</b>\n\n"
            f"📛 {player.name} - {player.rating} OVR\n"
            f"💰 Paid: {buy_val:,} 🪙\n"
            f"💳 Balance: {user.total_coins:,} 🪙\n"
            f"📊 Roster: {user.roster_count}/25",
            parse_mode="HTML",
        )

    except Exception:
        session.rollback()
        logger.exception(f"BuyPl confirm error")
    finally:
        session.close()


async def buypl_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tg_user = query.from_user
    parts = query.data.split("_")
    if len(parts) >= 2:
        try:
            owner_tg = int(parts[1])
            if tg_user.id != owner_tg:
                await query.answer("This isn't your purchase!", show_alert=True)
                return
        except ValueError:
            pass
    await query.answer("Cancelled")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

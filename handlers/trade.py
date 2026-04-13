"""Handler for /trade @username — multi-step trading flow."""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, Player, Trade
from config import get_buy_value, TRADE_EXPIRES_SECONDS
from services.rating_matcher_service import (
    find_matching_ratings,
    get_players_at_rating,
    get_trade_fee,
    can_trade_with_user,
)
from services.activity_service import log_activity
from services.trading_service import (
    initiate_trade,
    accept_trade,
    reject_trade,
    get_pending_trade_for_user,
)

logger = logging.getLogger(__name__)


# ── /trade @username ─────────────────────────────────────────────────

async def trade_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    logger.info(f"/trade from {tg_user.id}, args={context.args}")

    if not context.args:
        await update.message.reply_text(
            "Usage: /trade @username\nExample: /trade @friend123"
        )
        return

    target_raw = context.args[0].lstrip("@").strip()
    if not target_raw:
        await update.message.reply_text("❌ Invalid username format. Use /trade @username")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        if target_raw.lower() == (user.username or "").lower():
            await update.message.reply_text("❌ You cannot trade with yourself")
            return

        target = session.query(User).filter(User.username.ilike(target_raw)).first()
        if not target:
            await update.message.reply_text(f"❌ User @{target_raw} not found. They need to /debut first.")
            return

        # Check for existing pending trade
        pending = get_pending_trade_for_user(session, user.id)
        if pending:
            await update.message.reply_text(
                "⚠️ You already have a pending trade. Cancel it first or wait for it to expire."
            )
            return

        # Find matching ratings
        matching = find_matching_ratings(session, user.id, target.id)
        if not matching:
            await update.message.reply_text(
                f"❌ No matching tradeable ratings with @{target.username}.\n"
                "Both users need players rated 75+ OVR at the same rating."
            )
            return

        # Show matching ratings
        lines = []
        for rating in matching:
            my_players = get_players_at_rating(session, user.id, rating)
            their_players = get_players_at_rating(session, target.id, rating)
            my_names = ", ".join(p.name for _, p in my_players)
            their_names = ", ".join(p.name for _, p in their_players)
            fee = get_trade_fee(rating)
            lines.append(
                f"<b>{rating} OVR</b> (fee: {fee:,} 🪙)\n"
                f"  You: {my_names}\n"
                f"  Them: {their_names}"
            )

        text = (
            f"🔍 <b>TRADE MATCHES WITH</b> @{target.username}\n\n"
            + "\n\n".join(lines) + "\n\n"
            "Select a rating to trade:"
        )

        buttons = []
        for rating in matching:
            buttons.append([
                InlineKeyboardButton(
                    f"⚡ {rating} OVR",
                    callback_data=f"trate_{target.id}_{rating}",
                )
            ])
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="tcancel")])

        await update.message.reply_text(
            text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons)
        )

    except Exception:
        logger.exception(f"Trade error for {tg_user.id}")
        await update.message.reply_text("⚠️ Database error. Please try again later.")
    finally:
        session.close()


# ── Step 2: Select your player at chosen rating ─────────────────────

async def trade_rating_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User selected a rating to trade at."""
    query = update.callback_query
    await query.answer()
    tg_user = query.from_user

    parts = query.data.split("_")  # trate_{target_user_id}_{rating}
    target_id = int(parts[1])
    rating = int(parts[2])

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            return

        my_players = get_players_at_rating(session, user.id, rating)
        if not my_players:
            await query.edit_message_text(f"❌ You no longer have any {rating} OVR players")
            return

        text = (
            f"🏏 <b>SELECT YOUR PLAYER TO TRADE</b> ({rating} OVR)\n\n"
            "Available:\n"
        )
        buttons = []
        for i, (entry, player) in enumerate(my_players, 1):
            text += f"{i}. {player.name} - {player.rating} OVR | {player.category}\n"
            label = player.name[:20]
            if len(my_players) > 1:
                label = f"{player.name[:18]} #{i}"
            buttons.append([
                InlineKeyboardButton(
                    f"Select {label}",
                    callback_data=f"tmypl_{target_id}_{rating}_{entry.id}",
                )
            ])

        buttons.append([InlineKeyboardButton("◀️ Back", callback_data=f"tback_{target_id}")])
        await query.edit_message_text(
            text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons)
        )

    except Exception:
        logger.exception(f"Trade rating callback error for {tg_user.id}")
    finally:
        session.close()


# ── Step 3: Select their player at same rating ──────────────────────

async def trade_myplayer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User selected their player, now pick the receiver's player."""
    query = update.callback_query
    await query.answer()
    tg_user = query.from_user

    parts = query.data.split("_")  # tmypl_{target_id}_{rating}_{my_roster_id}
    target_id = int(parts[1])
    rating = int(parts[2])
    my_roster_id = int(parts[3])

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        target = session.query(User).get(target_id)
        if not user or not target:
            return

        their_players = get_players_at_rating(session, target.id, rating)
        if not their_players:
            await query.edit_message_text(f"❌ @{target.username} no longer has {rating} OVR players")
            return

        text = (
            f"🏏 <b>SELECT @{target.username}'s PLAYER TO RECEIVE</b> ({rating} OVR)\n\n"
            "Available:\n"
        )
        buttons = []
        for i, (entry, player) in enumerate(their_players, 1):
            text += f"{i}. {player.name} - {player.rating} OVR | {player.category}\n"
            label = player.name[:20]
            if len(their_players) > 1:
                label = f"{player.name[:18]} #{i}"
            buttons.append([
                InlineKeyboardButton(
                    f"Select {label}",
                    callback_data=f"tthpl_{target_id}_{my_roster_id}_{entry.id}",
                )
            ])

        buttons.append([InlineKeyboardButton("◀️ Back", callback_data=f"trate_{target_id}_{rating}")])
        await query.edit_message_text(
            text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons)
        )

    except Exception:
        logger.exception(f"Trade myplayer callback error for {tg_user.id}")
    finally:
        session.close()


# ── Step 4: Confirm & send offer ─────────────────────────────────────

async def trade_theirplayer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User selected both players. Show confirmation with fees."""
    query = update.callback_query
    await query.answer()
    tg_user = query.from_user

    parts = query.data.split("_")  # tthpl_{target_id}_{my_roster_id}_{their_roster_id}
    target_id = int(parts[1])
    my_roster_id = int(parts[2])
    their_roster_id = int(parts[3])

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        target = session.query(User).get(target_id)
        if not user or not target:
            return

        from models import UserRoster
        my_entry = session.query(UserRoster).get(my_roster_id)
        their_entry = session.query(UserRoster).get(their_roster_id)
        if not my_entry or not their_entry:
            await query.edit_message_text("❌ One of the players is no longer available")
            return

        my_player = session.query(Player).get(my_entry.player_id)
        their_player = session.query(Player).get(their_entry.player_id)

        buy_val = get_buy_value(my_player.rating)
        fee = get_trade_fee(my_player.rating)

        # Validate
        check = can_trade_with_user(session, user, target, my_player.rating)
        if not check["can_trade"]:
            await query.edit_message_text(f"❌ {check['reason']}")
            return

        text = (
            f"📬 <b>TRADE OFFER CONFIRMATION</b>\n\n"
            f"➡️  You offer: {my_player.name} - {my_player.rating} OVR\n"
            f"💰 Buy Value: {buy_val:,} 🪙\n"
            f"💳 Trade Fee (5%): {fee:,} 🪙\n\n"
            f"⬅️  You receive: {their_player.name} - {their_player.rating} OVR\n"
            f"💰 Buy Value: {buy_val:,} 🪙\n"
            f"💳 Trade Fee (5%): {fee:,} 🪙\n\n"
            f"🔄 Fair Trade: ✅ Yes (Same rating)\n\n"
            f"💸 Your cost: {fee:,} 🪙\n"
            f"⏳ Offer expires in: {TRADE_EXPIRES_SECONDS} seconds"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Send Offer",
                    callback_data=f"tsend_{target_id}_{my_roster_id}_{their_roster_id}",
                ),
                InlineKeyboardButton("❌ Cancel", callback_data="tcancel"),
            ]
        ])

        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)

    except Exception:
        logger.exception(f"Trade confirm callback error for {tg_user.id}")
    finally:
        session.close()


# ── Send the offer ───────────────────────────────────────────────────

async def trade_send_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the trade offer to the receiver."""
    query = update.callback_query
    await query.answer()
    tg_user = query.from_user

    parts = query.data.split("_")  # tsend_{target_id}_{my_roster_id}_{their_roster_id}
    target_id = int(parts[1])
    my_roster_id = int(parts[2])
    their_roster_id = int(parts[3])

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        target = session.query(User).get(target_id)
        if not user or not target:
            return

        result = initiate_trade(session, user, target, my_roster_id, their_roster_id)
        session.commit()

        if not result["success"]:
            await query.edit_message_text(f"❌ {result['message']}")
            return

        init_p = result["init_player"]
        recv_p = result["recv_player"]
        trade_id = result["trade_id"]
        fee = result["fee"]

        # Update initiator's message
        await query.edit_message_text(
            f"📤 <b>TRADE OFFER SENT!</b>\n\n"
            f"To: @{target.username}\n\n"
            f"➡️  You offer: {init_p.name} - {init_p.rating} OVR\n"
            f"⬅️  You receive: {recv_p.name} - {recv_p.rating} OVR\n\n"
            f"⏳ Waiting for response... (expires in {TRADE_EXPIRES_SECONDS}s)\n",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel Offer", callback_data=f"treject_{trade_id}")]
            ]),
        )

        # Send Accept/Reject in same chat (works in groups and DMs)
        buy_val = get_buy_value(init_p.rating)
        recv_text = (
            f"📬 <b>INCOMING TRADE OFFER!</b>\n\n"
            f"From: @{user.username} → To: @{target.username}\n\n"
            f"➡️  Offering: {init_p.name} - {init_p.rating} OVR\n"
            f"⬅️  Wants: {recv_p.name} - {recv_p.rating} OVR\n"
            f"💳 Trade Fee: {fee:,} 🪙 (5% from both)\n\n"
            f"⏳ Expires in: {TRADE_EXPIRES_SECONDS} seconds\n\n"
            f"@{target.username} tap below to respond:"
        )

        recv_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Accept", callback_data=f"taccept_{trade_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"treject_{trade_id}"),
            ]
        ])

        # Send in same chat
        chat_id = query.message.chat_id
        await context.bot.send_message(
            chat_id=chat_id,
            text=recv_text,
            parse_mode="HTML",
            reply_markup=recv_keyboard,
        )

    except Exception:
        session.rollback()
        logger.exception(f"Trade send error for {tg_user.id}")
    finally:
        session.close()


# ── Accept / Reject callbacks ────────────────────────────────────────

async def trade_accept_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receiver accepts the trade."""
    query = update.callback_query
    await query.answer()
    tg_user = query.from_user

    trade_id = int(query.data.split("_")[1])

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            return

        result = accept_trade(session, trade_id, user)
        session.commit()

        if not result["success"]:
            await query.edit_message_text(f"❌ {result['message']}")
            return

        init_p = result["init_player"]
        recv_p = result["recv_player"]
        initiator = result["initiator"]
        receiver = result["receiver"]
        fee = result["fee"]

        # Notify receiver (the one who clicked accept)
        await query.edit_message_text(
            f"✅ <b>TRADE COMPLETED!</b>\n\n"
            f"@{receiver.username} ↔ @{initiator.username}\n\n"
            f"✅ @{receiver.username} gave: {recv_p.name} - {recv_p.rating} OVR\n"
            f"✅ @{initiator.username} gave: {init_p.name} - {init_p.rating} OVR\n\n"
            f"💸 Trade Fee: {fee:,} 🪙 from each\n"
            f"💰 @{receiver.username} Balance: {receiver.total_coins:,}\n"
            f"💰 @{initiator.username} Balance: {initiator.total_coins:,}",
            parse_mode="HTML",
        )

    except Exception:
        session.rollback()
        logger.exception(f"Trade accept error for {tg_user.id}")
    finally:
        session.close()


async def trade_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reject or cancel a trade."""
    query = update.callback_query
    await query.answer()
    tg_user = query.from_user

    trade_id = int(query.data.split("_")[1])

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            return

        result = reject_trade(session, trade_id, user)
        session.commit()

        if not result["success"]:
            await query.edit_message_text(f"❌ {result['message']}")
            return

        trade = result["trade"]
        is_initiator = trade.initiator_id == user.id
        action = "cancelled" if is_initiator else "rejected"
        await query.edit_message_text(f"❌ Trade {action} by @{user.username}.")

    except Exception:
        session.rollback()
        logger.exception(f"Trade reject error for {tg_user.id}")
    finally:
        session.close()


# ── Cancel / Back ────────────────────────────────────────────────────

async def trade_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Trade cancelled")
    try:
        await query.edit_message_text("❌ Trade cancelled.")
    except Exception:
        pass


async def trade_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Go back to rating selection for a target user."""
    query = update.callback_query
    await query.answer()
    tg_user = query.from_user

    target_id = int(query.data.split("_")[1])

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        target = session.query(User).get(target_id)
        if not user or not target:
            return

        matching = find_matching_ratings(session, user.id, target.id)
        if not matching:
            await query.edit_message_text("❌ No matching ratings available anymore.")
            return

        lines = []
        for rating in matching:
            fee = get_trade_fee(rating)
            lines.append(f"<b>{rating} OVR</b> (fee: {fee:,} 🪙)")

        text = (
            f"🔍 <b>TRADE MATCHES WITH</b> @{target.username}\n\n"
            + "\n".join(lines) + "\n\n"
            "Select a rating to trade:"
        )
        buttons = [[InlineKeyboardButton(f"⚡ {r} OVR", callback_data=f"trate_{target_id}_{r}")] for r in matching]
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="tcancel")])

        await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))

    except Exception:
        logger.exception(f"Trade back error for {tg_user.id}")
    finally:
        session.close()

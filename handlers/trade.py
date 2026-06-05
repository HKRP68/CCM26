"""Handler for /trade @username — exact same-OVR Telegram-storage flow."""

import logging
import time
import uuid
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import TRADE_EXPIRES_SECONDS
from database import get_session
from models import Player, User, UserRoster
from services.rating_matcher_service import get_players_at_rating, get_tradable_players
from services.trading_service import complete_trade, create_pending_trade, get_pending_trade_for_user, reject_trade

logger = logging.getLogger(__name__)
TRADE_STORE_KEY = "active_player_trades"
USER_TRADE_KEY = "active_player_trade_by_user"


def _mention(user: User) -> str:
    return f"@{user.username}" if user.username else (user.first_name or f"user{user.telegram_id}")


def _player_label(player: Player) -> str:
    return f"{player.name} | OVR {player.rating}"


def _store(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.bot_data.setdefault(TRADE_STORE_KEY, {})


def _user_index(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.bot_data.setdefault(USER_TRADE_KEY, {})


def _new_trade_id() -> str:
    return uuid.uuid4().hex[:10]


def _active_trade_for_user(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    tid = _user_index(context).get(user_id)
    state = _store(context).get(tid)
    if not state:
        return None
    if state.get("expires_at", 0) <= time.time() or state.get("status") in {"completed", "cancelled", "expired"}:
        _clear_trade(context, tid)
        return None
    return state


def _clear_trade(context: ContextTypes.DEFAULT_TYPE, trade_id: str):
    state = _store(context).pop(trade_id, None)
    if state:
        for uid in (state.get("user1_id"), state.get("user2_id")):
            _user_index(context).pop(uid, None)


def _is_expired(state: dict) -> bool:
    return time.time() >= state.get("expires_at", 0)


def _load_users(session, state):
    user1 = session.query(User).get(state["user1_id"])
    user2 = session.query(User).get(state["user2_id"])
    return user1, user2


def _select_buttons(trade_id: str, rows, prefix: str, cancel_label: str):
    buttons = [
        [InlineKeyboardButton(_player_label(player), callback_data=f"{prefix}_{trade_id}_{entry.id}")]
        for entry, player in rows
    ]
    buttons.append([InlineKeyboardButton(cancel_label, callback_data=f"tcancel_{trade_id}")])
    return InlineKeyboardMarkup(buttons)


async def _send_expiry_message(context: ContextTypes.DEFAULT_TYPE, state: dict):
    try:
        if state.get("message_id"):
            await context.bot.edit_message_text(
                chat_id=state["chat_id"],
                message_id=state["message_id"],
                text="Trade expired.",
            )
        else:
            await context.bot.send_message(chat_id=state["chat_id"], text="Trade expired.")
    except Exception:
        logger.exception("Failed to send trade expiry message")


async def _expire_trade_job(context: ContextTypes.DEFAULT_TYPE):
    trade_id = context.job.data["trade_id"]
    state = _store(context).get(trade_id)
    if not state or state.get("status") in {"completed", "cancelled", "expired"}:
        return
    state["status"] = "expired"
    session = get_session()
    try:
        trade_db_id = state.get("db_trade_id")
        if trade_db_id:
            from models import Trade
            trade = session.query(Trade).get(trade_db_id)
            if trade and trade.status == "pending":
                trade.status = "expired"
                trade.updated_at = datetime.utcnow()
                session.commit()
        await _send_expiry_message(context, state)
    finally:
        session.close()
        _clear_trade(context, trade_id)


def _schedule_expiry(context: ContextTypes.DEFAULT_TYPE, trade_id: str):
    if context.job_queue:
        context.job_queue.run_once(
            _expire_trade_job,
            when=TRADE_EXPIRES_SECONDS,
            data={"trade_id": trade_id},
            name=f"trade_expiry_{trade_id}",
        )


async def _answer_not_part(query):
    await query.answer("You are not part of this trade.", show_alert=True)


# ── /trade @username ─────────────────────────────────────────────────
async def trade_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    if not context.args:
        await update.message.reply_text("Usage: /trade @username\nExample: /trade @friend123")
        return

    target_raw = context.args[0].lstrip("@").strip()
    if not target_raw:
        await update.message.reply_text("❌ Invalid username format. Use /trade @username")
        return

    session = get_session()
    try:
        user1 = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user1:
            await update.message.reply_text("❌ Do /debut first!")
            return
        if target_raw.lower() == (user1.username or "").lower():
            await update.message.reply_text("❌ You cannot trade with yourself")
            return
        user2 = session.query(User).filter(User.username.ilike(target_raw)).first()
        if not user2:
            await update.message.reply_text(f"❌ User @{target_raw} not found. They need to /debut first.")
            return
        if (
            _active_trade_for_user(context, user1.id)
            or _active_trade_for_user(context, user2.id)
            or get_pending_trade_for_user(session, user1.id)
            or get_pending_trade_for_user(session, user2.id)
        ):
            await update.message.reply_text("⚠️ One of these users already has an active trade. Cancel it or wait for expiry.")
            return

        user1_players = get_tradable_players(session, user1.id)
        if not user1_players:
            await update.message.reply_text("❌ You have no tradable players available.")
            return

        trade_id = _new_trade_id()
        state = {
            "id": trade_id,
            "user1_id": user1.id,
            "user2_id": user2.id,
            "user1_tg_id": user1.telegram_id,
            "user2_tg_id": user2.telegram_id,
            "chat_id": update.effective_chat.id,
            "status": "awaiting_user1_player",
            "created_at": time.time(),
            "expires_at": time.time() + TRADE_EXPIRES_SECONDS,
            "confirmations": [],
        }
        _store(context)[trade_id] = state
        _user_index(context)[user1.id] = trade_id
        _user_index(context)[user2.id] = trade_id
        _schedule_expiry(context, trade_id)

        text = (
            f"{_mention(user1)} wants to trade with {_mention(user2)}\n\n"
            f"{_mention(user1)}, select your player to trade with {_mention(user2)}"
        )
        sent = await update.message.reply_text(
            text,
            reply_markup=_select_buttons(trade_id, user1_players, "t1p", "Cancel Trade"),
        )
        state["message_id"] = sent.message_id
    except Exception:
        logger.exception("Trade start error for %s", tg_user.id)
        await update.message.reply_text("⚠️ Database error. Please try again later.")
    finally:
        session.close()


# ── Step 2: User 1 selects player ────────────────────────────────────
async def trade_user1_player_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("_")  # t1p_{trade_id}_{roster_id}
    trade_id, roster_id = parts[1], int(parts[2])
    state = _store(context).get(trade_id)
    if not state:
        await query.answer("Trade expired.", show_alert=True)
        return
    if query.from_user.id != state["user1_tg_id"]:
        await _answer_not_part(query)
        return
    await query.answer()
    if _is_expired(state):
        state["status"] = "expired"
        await query.edit_message_text("Trade expired.")
        _clear_trade(context, trade_id)
        return

    session = get_session()
    try:
        user1, user2 = _load_users(session, state)
        entry = session.query(UserRoster).filter(UserRoster.id == roster_id, UserRoster.user_id == user1.id).first()
        player = session.query(Player).get(entry.player_id) if entry else None
        valid = any(e.id == roster_id for e, _ in get_tradable_players(session, user1.id))
        if not entry or not player or not valid:
            await query.edit_message_text("Trade cancelled because one player is no longer available.")
            _clear_trade(context, trade_id)
            return

        user2_players = get_players_at_rating(session, user2.id, player.rating)
        if not user2_players:
            await query.edit_message_text(f"❌ {_mention(user2)} has no tradable players with OVR {player.rating}.")
            _clear_trade(context, trade_id)
            return

        state.update(
            {
                "status": "awaiting_user2_player",
                "user1_selected_roster_id": entry.id,
                "user1_selected_player_id": player.id,
                "selected_player_name": player.name,
                "selected_player_ovr": player.rating,
            }
        )
        text = (
            f"{_mention(user1)} selected {_player_label(player)}\n\n"
            f"{_mention(user2)}, select your player to trade with {_mention(user1)}\n\n"
            f"{_mention(user1)} has selected: {_player_label(player)}\n"
            f"Rule: You can only select players with OVR {player.rating}"
        )
        await query.edit_message_text(
            text,
            reply_markup=_select_buttons(trade_id, user2_players, "t2p", "Reject Trade"),
        )
    except Exception:
        logger.exception("Trade user1 selection error")
    finally:
        session.close()


# ── Step 3: User 2 selects player ────────────────────────────────────
async def trade_user2_player_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("_")  # t2p_{trade_id}_{roster_id}
    trade_id, roster_id = parts[1], int(parts[2])
    state = _store(context).get(trade_id)
    if not state:
        await query.answer("Trade expired.", show_alert=True)
        return
    if query.from_user.id != state["user2_tg_id"]:
        await _answer_not_part(query)
        return
    await query.answer()
    if _is_expired(state):
        state["status"] = "expired"
        await query.edit_message_text("Trade expired.")
        _clear_trade(context, trade_id)
        return

    session = get_session()
    try:
        user1, user2 = _load_users(session, state)
        entry = session.query(UserRoster).filter(UserRoster.id == roster_id, UserRoster.user_id == user2.id).first()
        player = session.query(Player).get(entry.player_id) if entry else None
        required_ovr = state["selected_player_ovr"]
        valid = any(e.id == roster_id for e, _ in get_players_at_rating(session, user2.id, required_ovr))
        if not entry or not player or player.rating != required_ovr or not valid:
            await query.edit_message_text("Trade cancelled because one player is no longer available.")
            _clear_trade(context, trade_id)
            return

        result = create_pending_trade(
            session, user1, user2, state["user1_selected_roster_id"], entry.id
        )
        session.commit()
        if not result["success"]:
            await query.edit_message_text(result["message"])
            _clear_trade(context, trade_id)
            return

        state.update(
            {
                "status": "awaiting_confirmations",
                "db_trade_id": result["trade_id"],
                "user2_selected_roster_id": entry.id,
                "user2_selected_player_id": player.id,
                "user2_selected_player_name": player.name,
                "user2_selected_player_ovr": player.rating,
                "confirmations": [],
            }
        )
        init_player = result["init_player"]
        recv_player = result["recv_player"]
        text = (
            "Trade Confirmation\n\n"
            f"{_mention(user1)} gives:\n{_player_label(init_player)}\n\n"
            f"{_mention(user2)} gives:\n{_player_label(recv_player)}\n\n"
            "Both players have the same OVR rating."
        )
        buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"{_mention(user1)} Confirm", callback_data=f"tcfrm_{trade_id}_{user1.id}"),
                InlineKeyboardButton(f"{_mention(user2)} Confirm", callback_data=f"tcfrm_{trade_id}_{user2.id}"),
            ],
            [InlineKeyboardButton("Cancel Trade", callback_data=f"tcancel_{trade_id}")],
        ])
        await query.edit_message_text(text, reply_markup=buttons)
    except Exception:
        session.rollback()
        logger.exception("Trade user2 selection error")
    finally:
        session.close()


# ── Step 4: Confirm callbacks ────────────────────────────────────────
async def trade_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("_")  # tcfrm_{trade_id}_{expected_user_id}
    trade_id, expected_user_id = parts[1], int(parts[2])
    state = _store(context).get(trade_id)
    if not state:
        await query.answer("Trade expired.", show_alert=True)
        return
    if query.from_user.id not in {state["user1_tg_id"], state["user2_tg_id"]}:
        await _answer_not_part(query)
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == query.from_user.id).first()
        user1, user2 = _load_users(session, state)
        if not user or user.id != expected_user_id or user.id not in {user1.id, user2.id}:
            await query.answer("This confirm button is not for you.", show_alert=True)
            return
        await query.answer()
        if _is_expired(state):
            await query.edit_message_text("Trade expired.")
            _clear_trade(context, trade_id)
            return

        confirmations = state.setdefault("confirmations", [])
        if user.id not in confirmations:
            confirmations.append(user.id)
        confirmed_ids = set(confirmations)
        other = user2 if user.id == user1.id else user1
        if {user1.id, user2.id} - confirmed_ids:
            await query.edit_message_text(
                f"{_mention(user)} confirmed the trade.\nWaiting for {_mention(other)} confirmation...",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{_mention(other)} Confirm", callback_data=f"tcfrm_{trade_id}_{other.id}")],
                    [InlineKeyboardButton("Cancel Trade", callback_data=f"tcancel_{trade_id}")],
                ]),
            )
            return

        await query.edit_message_text(f"{_mention(user)} confirmed the trade.\nProcessing trade...")
        result = complete_trade(session, state["db_trade_id"])
        session.commit()
        if not result["success"]:
            await context.bot.send_message(chat_id=state["chat_id"], text=result["message"])
            _clear_trade(context, trade_id)
            return
        initiator = result["initiator"]
        receiver = result["receiver"]
        init_player = result["init_player"]
        recv_player = result["recv_player"]
        final_text = (
            "Trade Completed Successfully\n\n"
            f"{_mention(initiator)} received: {_player_label(recv_player)}\n"
            f"{_mention(receiver)} received: {_player_label(init_player)}"
        )
        await context.bot.send_message(chat_id=state["chat_id"], text=final_text)
        _clear_trade(context, trade_id)
    except Exception:
        session.rollback()
        logger.exception("Trade confirm error")
    finally:
        session.close()


async def trade_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("_")
    trade_id = parts[1] if len(parts) > 1 else None
    state = _store(context).get(trade_id) if trade_id else None
    if state and query.from_user.id not in {state["user1_tg_id"], state["user2_tg_id"]}:
        await _answer_not_part(query)
        return
    await query.answer("Trade cancelled")
    if state and state.get("db_trade_id"):
        session = get_session()
        try:
            user = session.query(User).filter(User.telegram_id == query.from_user.id).first()
            if user:
                reject_trade(session, state["db_trade_id"], user)
                session.commit()
        except Exception:
            session.rollback()
            logger.exception("Trade cancel DB error")
        finally:
            session.close()
    if state:
        state["status"] = "cancelled"
        _clear_trade(context, trade_id)
    try:
        await query.edit_message_text("Trade cancelled.")
    except Exception:
        pass


# Legacy callback names kept so older imports do not break.
async def trade_rating_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await trade_cancel_callback(update, context)


async def trade_myplayer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await trade_user1_player_callback(update, context)


async def trade_theirplayer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await trade_user2_player_callback(update, context)


async def trade_send_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await trade_confirm_callback(update, context)


async def trade_accept_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await trade_confirm_callback(update, context)


async def trade_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await trade_cancel_callback(update, context)


async def trade_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await trade_cancel_callback(update, context)

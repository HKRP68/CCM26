"""/tradetrait @username — swap an inventory trait with another captain.

The same shape as /trade for players, one tier down: you pick one of your
unequipped traits, they pick one of theirs at the **same level and rarity** (the
trait analogue of /trade's same-OVR rule), then both confirm and the two swap.
Each side pays a gem fee for that tier — 15 💎 at Common Lv.1, rising with the
upgrade gems sunk into the trait and with its rarity (see
``config.trait_trade_fee``):

    Level        1      2      3      4      5
    trade fee   15     16     18     22     29   each side, Common tier

Rules worth knowing:
  • Inventory only. A trait on a player isn't tradable — /removetrait first,
    which is free and keeps the level.
  • Same level AND same rarity both ways, so one fee covers both sides and a
    Common is never swapped for an Elite.
  • Not the same trait: swapping Yorker Lv.3 for Yorker Lv.3 changes nothing.
  • One open trade per captain, 60s to complete it, and the fee is re-checked at
    the moment of the swap — nobody ends up with a half-done trade.

State lives in ``context.bot_data`` for the life of the prompt (same as /trade);
every rule that decides an outcome lives in
``services.trait_trading_service`` so it can be tested without Telegram.
"""

import logging
import time
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import TRADE_EXPIRES_SECONDS, trait_trade_fee, trait_rarity_label
from services.trait_service import trait_rarity_of
from database import get_session
from models import User
from services.telegram_user_service import resolve_command_target, sync_telegram_user
from services.trait_trading_service import (
    complete_trait_swap, list_inventory, quote_trait_swap,
)

logger = logging.getLogger(__name__)

TRADE_STORE_KEY = "active_trait_trades"
USER_TRADE_KEY = "active_trait_trade_by_user"


# ── State plumbing ───────────────────────────────────────────────────

def _store(context):
    return context.bot_data.setdefault(TRADE_STORE_KEY, {})


def _user_index(context):
    return context.bot_data.setdefault(USER_TRADE_KEY, {})


def _new_trade_id():
    return uuid.uuid4().hex[:10]


def _is_expired(state):
    return time.time() >= state.get("expires_at", 0)


def _clear_trade(context, trade_id):
    state = _store(context).pop(trade_id, None)
    if state:
        for uid in (state.get("user1_id"), state.get("user2_id")):
            _user_index(context).pop(uid, None)


def _active_trade_for_user(context, user_id):
    """This captain's live trait trade, dropping it if it has expired."""
    tid = _user_index(context).get(user_id)
    state = _store(context).get(tid)
    if not state:
        return None
    if _is_expired(state) or state.get("status") in {"completed", "cancelled", "expired"}:
        _clear_trade(context, tid)
        return None
    return state


def _mention(user):
    return f"@{user.username}" if getattr(user, "username", None) else (
        getattr(user, "first_name", None) or f"user{user.telegram_id}")


def _label(trait, level):
    return f"{trait.emoji or '✨'} {trait.name} Lv.{level}"


def _load_users(session, state):
    return (session.query(User).get(state["user1_id"]),
            session.query(User).get(state["user2_id"]))


async def _answer_not_part(query, message="You are not part of this trade."):
    await query.answer(message, show_alert=True)


def _side_name(state, side):
    """'@rohit' for the stored side, for turn-order alerts.

    The trade state keeps telegram ids, not names, so fall back to a neutral
    label rather than guessing — the point of the alert is *whose turn it is*,
    and "the other captain" says that perfectly well when the name is unknown.
    """
    return state.get(f"{side}_name") or "the other captain"


def _pick_buttons(trade_id, rows, prefix):
    buttons = [[InlineKeyboardButton(_label(trait, int(inv.level or 1)),
                                    callback_data=f"{prefix}_{trade_id}_{inv.id}")]
               for inv, trait in rows]
    buttons.append([InlineKeyboardButton("Cancel Trade",
                                         callback_data=f"ttcancel_{trade_id}")])
    return InlineKeyboardMarkup(buttons)


def _schedule_expiry(context, trade_id):
    if context.job_queue:
        context.job_queue.run_once(
            _expire_trade_job, when=TRADE_EXPIRES_SECONDS,
            data={"trade_id": trade_id}, name=f"trait_trade_expiry_{trade_id}")


async def _expire_trade_job(context):
    trade_id = context.job.data["trade_id"]
    state = _store(context).get(trade_id)
    if not state or state.get("status") in {"completed", "cancelled", "expired"}:
        return
    state["status"] = "expired"
    try:
        await context.bot.send_message(
            chat_id=state["chat_id"],
            text="⌛ Trait trade expired — run /tradetrait again to retry.")
    except Exception:
        logger.debug("trait trade: expiry message failed", exc_info=True)
    _clear_trade(context, trade_id)


# ── Step 1: /tradetrait @username ────────────────────────────────────

async def tradetrait_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tradetrait @user — start a trait-for-trait swap."""
    tg_user = update.effective_user
    session = get_session()
    try:
        user1 = sync_telegram_user(session, tg_user)
        if not user1:
            await update.message.reply_text("❌ Do /debut first!")
            return

        user2, target_source = resolve_command_target(session, update, context,
                                                      "tradetrait")
        if not user2:
            if target_source == "not_mention":
                await update.message.reply_text(
                    "❌ Reply to the user's message or use a real @username.\n"
                    "Usage: /tradetrait @username")
            elif target_source == "missing":
                await update.message.reply_text(
                    "Usage: /tradetrait @username\n"
                    "Tip: for users without an @username, reply to their message "
                    "and run /tradetrait.")
            else:
                await update.message.reply_text(
                    "❌ User not found. They need to /debut first; if they have no "
                    "username, reply to their message and run /tradetrait.")
            return
        if user2.id == user1.id:
            await update.message.reply_text("❌ You cannot trade with yourself.")
            return

        if _active_trade_for_user(context, user1.id) or \
                _active_trade_for_user(context, user2.id):
            await update.message.reply_text(
                "⚠️ One of you already has a trait trade open. Cancel it or wait "
                "for it to expire.")
            return

        mine = list_inventory(session, user1.id)
        if not mine:
            await update.message.reply_text(
                "📦 <b>Your trait inventory is empty.</b>\n\n"
                "Only unequipped traits can be traded — /removetrait brings one "
                "back for free, or buy from /traitshop.", parse_mode="HTML")
            return
        if not list_inventory(session, user2.id):
            await update.message.reply_text(
                f"📦 {_mention(user2)} has no unequipped traits to trade.")
            return

        trade_id = _new_trade_id()
        state = {
            "id": trade_id,
            "user1_id": user1.id, "user2_id": user2.id,
            "user1_tg_id": user1.telegram_id, "user2_tg_id": user2.telegram_id,
            "user1_name": _mention(user1), "user2_name": _mention(user2),
            "chat_id": update.effective_chat.id,
            "status": "awaiting_user1_trait",
            "created_at": time.time(),
            "expires_at": time.time() + TRADE_EXPIRES_SECONDS,
            "confirmations": [],
        }
        _store(context)[trade_id] = state
        _user_index(context)[user1.id] = trade_id
        _user_index(context)[user2.id] = trade_id
        _schedule_expiry(context, trade_id)

        sent = await update.message.reply_text(
            f"💠 <b>TRAIT TRADE</b>\n\n"
            f"{_mention(user1)} wants to trade a trait with {_mention(user2)}.\n\n"
            f"{_mention(user1)}, pick the trait you're offering:\n"
            f"<i>Both traits must be the same level and rarity, and each side pays the "
            f"level's fee in gems.</i>",
            parse_mode="HTML",
            reply_markup=_pick_buttons(trade_id, mine, "tt1"))
        state["message_id"] = getattr(sent, "message_id", None)
    except Exception:
        logger.exception("trait trade start failed for %s", tg_user.id)
        await update.message.reply_text("⚠️ Error. Please try again later.")
    finally:
        session.close()


# ── Step 2: the initiator picks their trait ──────────────────────────

async def tradetrait_user1_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """tt1_<trade_id>_<inventory_id>"""
    query = update.callback_query
    try:
        _, trade_id, inv_id = query.data.split("_")
        inv_id = int(inv_id)
    except ValueError:
        await query.answer("Invalid")
        return
    state = _store(context).get(trade_id)
    if not state:
        await query.answer("Trade expired.", show_alert=True)
        return
    if query.from_user.id != state["user1_tg_id"]:
        # The other captain tapping here isn't a stranger — it's simply not
        # their turn yet, so say that instead of "you are not part of this".
        await _answer_not_part(
            query,
            f"{_side_name(state, 'user1')} picks first — yours is next."
            if query.from_user.id == state["user2_tg_id"]
            else "You are not part of this trade.")
        return
    await query.answer()
    if _is_expired(state):
        state["status"] = "expired"
        await query.edit_message_text("⌛ Trait trade expired.")
        _clear_trade(context, trade_id)
        return

    session = get_session()
    try:
        user1, user2 = _load_users(session, state)
        mine = {inv.id: (inv, trait) for inv, trait in list_inventory(session, user1.id)}
        if inv_id not in mine:
            await query.edit_message_text(
                "❌ That trait is no longer in your inventory — trade cancelled.")
            _clear_trade(context, trade_id)
            return
        inv, trait = mine[inv_id]
        level = int(inv.level or 1)
        rarity = trait_rarity_of(trait)
        rarity_name = trait_rarity_label(rarity)

        theirs = list_inventory(session, user2.id, level=level)
        # Their identical trait can't be the other half of the swap, and neither
        # can one from a different rarity tier — a swap has to be like for like.
        theirs = [(i, t) for i, t in theirs
                  if i.trait_id != inv.trait_id and trait_rarity_of(t) == rarity]
        if not theirs:
            await query.edit_message_text(
                f"❌ {_mention(user2)} has no other unequipped {rarity_name} "
                f"<b>Lv.{level}</b> trait to trade. Pick a different one, or they "
                f"can /traitupgrade one to match.", parse_mode="HTML")
            _clear_trade(context, trade_id)
            return

        fee = trait_trade_fee(level, rarity)
        state.update({"status": "awaiting_user2_trait",
                      "user1_inv_id": inv.id, "level": level,
                      "rarity": rarity, "fee": fee})
        await query.edit_message_text(
            f"💠 <b>TRAIT TRADE</b>\n\n"
            f"{_mention(user1)} offers: <b>{_label(trait, level)}</b> "
            f"({rarity_name})\n\n"
            f"{_mention(user2)}, pick a {rarity_name} <b>Lv.{level}</b> trait to "
            f"trade back:\n"
            f"<i>Fee at this tier: {fee:,} 💎 each.</i>",
            parse_mode="HTML",
            reply_markup=_pick_buttons(trade_id, theirs, "tt2"))
    except Exception:
        logger.exception("trait trade user1 selection failed")
    finally:
        session.close()


# ── Step 3: the other captain picks theirs ───────────────────────────

async def tradetrait_user2_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """tt2_<trade_id>_<inventory_id>"""
    query = update.callback_query
    try:
        _, trade_id, inv_id = query.data.split("_")
        inv_id = int(inv_id)
    except ValueError:
        await query.answer("Invalid")
        return
    state = _store(context).get(trade_id)
    if not state:
        await query.answer("Trade expired.", show_alert=True)
        return
    if query.from_user.id != state["user2_tg_id"]:
        await _answer_not_part(
            query,
            f"You've made your offer — it's {_side_name(state, 'user2')}'s "
            f"turn to pick."
            if query.from_user.id == state["user1_tg_id"]
            else "You are not part of this trade.")
        return
    await query.answer()
    if _is_expired(state):
        state["status"] = "expired"
        await query.edit_message_text("⌛ Trait trade expired.")
        _clear_trade(context, trade_id)
        return

    session = get_session()
    try:
        user1, user2 = _load_users(session, state)
        quote = quote_trait_swap(session, user1, user2,
                                 state["user1_inv_id"], inv_id)
        if not quote["ok"]:
            await query.edit_message_text(f"❌ {quote['message']}")
            _clear_trade(context, trade_id)
            return

        state.update({"status": "awaiting_confirmations",
                      "user2_inv_id": inv_id, "fee": quote["fee"],
                      "level": quote["level"], "confirmations": []})
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{_mention(user1)} Confirm",
                                  callback_data=f"ttcfrm_{trade_id}_{user1.id}"),
             InlineKeyboardButton(f"{_mention(user2)} Confirm",
                                  callback_data=f"ttcfrm_{trade_id}_{user2.id}")],
            [InlineKeyboardButton("Cancel Trade",
                                  callback_data=f"ttcancel_{trade_id}")],
        ])
        await query.edit_message_text(
            f"💠 <b>TRAIT TRADE — CONFIRM</b>\n\n"
            f"{_mention(user1)} gives: <b>{quote['label1']}</b>\n"
            f"{_mention(user2)} gives: <b>{quote['label2']}</b>\n\n"
            f"Both traits are Lv.{quote['level']}.\n"
            f"💎 <b>Fee: {quote['fee']:,} gems EACH.</b>\n\n"
            f"<i>Both captains must confirm.</i>",
            parse_mode="HTML", reply_markup=buttons)
    except Exception:
        logger.exception("trait trade user2 selection failed")
    finally:
        session.close()


# ── Step 4: both confirm → swap ──────────────────────────────────────

async def tradetrait_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ttcfrm_<trade_id>_<expected_user_id>"""
    query = update.callback_query
    try:
        _, trade_id, expected_user_id = query.data.split("_")
        expected_user_id = int(expected_user_id)
    except ValueError:
        await query.answer("Invalid")
        return
    state = _store(context).get(trade_id)
    if not state:
        await query.answer("Trade expired.", show_alert=True)
        return
    if query.from_user.id not in {state["user1_tg_id"], state["user2_tg_id"]}:
        await _answer_not_part(query)
        return

    session = get_session()
    try:
        user1, user2 = _load_users(session, state)
        user = user1 if query.from_user.id == state["user1_tg_id"] else user2
        if not user:
            await _answer_not_part(query)
            return
        # Both captains' Confirm buttons sit side by side, so hitting the wrong
        # one is a near-miss, not an attack. Confirm the TAPPER's own side
        # rather than refusing: a tap can only ever confirm for whoever made it
        # (``user`` is resolved from the telegram id, never from the button), so
        # nobody can confirm on the other captain's behalf.
        await query.answer("Confirmed for you." if user.id != expected_user_id
                           else None)
        if _is_expired(state):
            await query.edit_message_text("⌛ Trait trade expired.")
            _clear_trade(context, trade_id)
            return

        confirmations = state.setdefault("confirmations", [])
        if user.id not in confirmations:
            confirmations.append(user.id)
        other = user2 if user.id == user1.id else user1
        if {user1.id, user2.id} - set(confirmations):
            await query.edit_message_text(
                f"{_mention(user)} confirmed.\n"
                f"Waiting for {_mention(other)} to confirm "
                f"({state['fee']:,} 💎 each)…",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{_mention(other)} Confirm",
                                          callback_data=f"ttcfrm_{trade_id}_{other.id}")],
                    [InlineKeyboardButton("Cancel Trade",
                                          callback_data=f"ttcancel_{trade_id}")],
                ]))
            return

        await query.edit_message_text("💠 Both confirmed — processing trade…")
        result = complete_trait_swap(session, user1, user2,
                                    state["user1_inv_id"], state["user2_inv_id"])
        if not result["success"]:
            session.rollback()
            await context.bot.send_message(chat_id=state["chat_id"],
                                           text=f"❌ {result['message']}")
            _clear_trade(context, trade_id)
            return
        session.commit()
        state["status"] = "completed"
        await context.bot.send_message(
            chat_id=state["chat_id"],
            text=(f"✅ <b>TRAIT TRADE COMPLETE</b>\n\n"
                  f"{_mention(user1)} received: <b>{result['label2']}</b>\n"
                  f"{_mention(user2)} received: <b>{result['label1']}</b>\n\n"
                  f"💎 Fee: {result['fee']:,} gems each\n"
                  f"{_mention(user1)}: {user1.total_gems:,} 💎  |  "
                  f"{_mention(user2)}: {user2.total_gems:,} 💎\n\n"
                  f"<i>Traded traits land in your inventory — /traitapply to "
                  f"equip.</i>"),
            parse_mode="HTML")
        _clear_trade(context, trade_id)
    except Exception:
        session.rollback()
        logger.exception("trait trade confirm failed")
        try:
            await context.bot.send_message(
                chat_id=state["chat_id"],
                text="⚠️ The trait trade failed — nothing was exchanged.")
        except Exception:
            pass
        _clear_trade(context, trade_id)
    finally:
        session.close()


# ── Cancel ───────────────────────────────────────────────────────────

async def tradetrait_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ttcancel_<trade_id>"""
    query = update.callback_query
    parts = query.data.split("_")
    trade_id = parts[1] if len(parts) > 1 else None
    state = _store(context).get(trade_id) if trade_id else None
    if state and query.from_user.id not in {state["user1_tg_id"], state["user2_tg_id"]}:
        await _answer_not_part(query)
        return
    await query.answer("Cancelled")
    if state:
        state["status"] = "cancelled"
        _clear_trade(context, trade_id)
    try:
        await query.edit_message_text("❌ Trait trade cancelled.")
    except Exception:
        pass

"""/playermarket — single composite image + button list.

Flow:
  1. User runs /playermarket
  2. Bot sends ONE image (composite of all slots) with player-name buttons + Cancel
  3. User taps a player button → bot shows full card with Buy + Cancel
  4. Buy → confirms purchase or shows error

The composite image is rebuilt on every command for now (cheap because it's
just PIL drawing from already-cached player data). If perf becomes an issue,
add a cache by (slot_ids, purchased_counts) tuple.
"""

import io
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, Player
from services.global_market import (
    list_player_market, buy_player, ensure_player_market_fresh,
    get_next_refresh_at,
)
from services.market_image import generate_market_image
from services.card_generator import generate_card
from services.activity_service import log_activity
from services.button_timeout import schedule_button_timeout

logger = logging.getLogger(__name__)


def _format_eta(dt):
    """Return 'in 12h 30m' for a future datetime, or '' if past/None."""
    if not dt:
        return ""
    from datetime import datetime
    delta = dt - datetime.utcnow()
    if delta.total_seconds() <= 0:
        return "now"
    hours = int(delta.total_seconds() // 3600)
    minutes = int((delta.total_seconds() % 3600) // 60)
    if hours > 0:
        return f"in {hours}h {minutes}m"
    return f"in {minutes}m"


async def playermarket_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        # Auto-refresh if 24h have passed (or first run)
        ensure_player_market_fresh(session)

        slots = list_player_market(session)
        if not slots:
            await update.message.reply_text(
                "❌ Market is empty. Try again in a moment.")
            return

        # Generate the composite image
        next_at = get_next_refresh_at(session)
        eta = _format_eta(next_at)
        subtitle = f"Refresh {eta}" if eta else None

        img_bytes = generate_market_image(
            session, slots,
            title="🌟 PLAYER MARKET",
            subtitle=subtitle,
        )

        # Build buttons: one per player (their name) + Cancel
        btns = []
        for slot in slots:
            player = session.query(Player).get(slot.player_id)
            if not player:
                continue
            sold = (slot.purchased_count >= slot.quantity)
            label = f"{'❌ ' if sold else ''}{player.name} • {slot.final_price:,} 🪙"
            cb = (f"pmsel_{tg_user.id}_{slot.slot_index}"
                  if not sold else f"pmnoop_{tg_user.id}")
            btns.append([InlineKeyboardButton(label, callback_data=cb)])
        btns.append([InlineKeyboardButton(
            "❌ Cancel", callback_data=f"pmcancel_{tg_user.id}")])

        caption = (
            f"💰 <b>{user.total_coins:,}</b> 🪙 · "
            f"📊 {user.roster_count}/25 roster\n"
            f"🏷️ All cards <b>10% off</b> · Refreshes every 24h"
        )

        if img_bytes:
            sent = await update.message.reply_photo(
                photo=io.BytesIO(img_bytes),
                caption=caption,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(btns),
            )
        else:
            # Fallback: text-only listing if image generation fails
            text_lines = [
                "🌟 <b>PLAYER MARKET</b>",
                caption,
                "━━━━━━━━━━━━━━━━━━",
            ]
            for slot in slots:
                p = session.query(Player).get(slot.player_id)
                if p:
                    text_lines.append(
                        f"#{slot.slot_index+1}. <b>{p.name}</b> "
                        f"({p.rating} OVR) — {slot.final_price:,} 🪙")
            sent = await update.message.reply_text(
                "\n".join(text_lines),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(btns),
            )

        # 3-minute button expiry
        try:
            schedule_button_timeout(
                context, sent.chat_id, sent.message_id,
                delay_seconds=180,
                custom_text=None,
            )
        except Exception:
            pass

    except Exception:
        session.rollback()
        logger.exception("playermarket_handler error")
        try:
            await update.message.reply_text("⚠️ Error loading market. Try again.")
        except Exception:
            pass
    finally:
        session.close()


async def playermarket_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User tapped a player button → show that player's card with Buy + Cancel."""
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        owner_tg = int(parts[1])
        slot_index = int(parts[2])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return

    if tg.id != owner_tg:
        await q.answer("Open your own /playermarket!", show_alert=True)
        return

    session = get_session()
    try:
        from models import GlobalPlayerMarket
        slot = (session.query(GlobalPlayerMarket)
                .filter(GlobalPlayerMarket.slot_index == slot_index,
                        GlobalPlayerMarket.is_active == True).first())
        if not slot:
            await q.answer("Slot no longer available.", show_alert=True)
            return
        player = session.query(Player).get(slot.player_id)
        if not player:
            await q.answer("Player gone.", show_alert=True)
            return

        sold = (slot.purchased_count >= slot.quantity)

        # Build the player's full card
        try:
            card_bytes = generate_card(player)
        except Exception:
            card_bytes = None

        cap_lines = [
            f"<b>{player.name}</b>",
            f"⭐ {player.rating} OVR · {player.category} · {player.country or '—'}",
            "",
            f"💸 Price: <b>{slot.final_price:,}</b> 🪙",
        ]
        if slot.base_price > slot.final_price:
            disc = int((1 - slot.final_price / slot.base_price) * 100)
            cap_lines.insert(3, f"<s>{slot.base_price:,}</s> 🪙  <i>(-{disc}%)</i>")
        if sold:
            cap_lines.append("\n❌ <i>Sold out</i>")
        cap = "\n".join(cap_lines)

        if sold:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("← Back",
                                     callback_data=f"pmback_{tg.id}"),
            ]])
        else:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    f"✅ Buy ({slot.final_price:,} 🪙)",
                    callback_data=f"pmbuy_{tg.id}_{slot_index}",
                ),
                InlineKeyboardButton("❌ Cancel",
                                     callback_data=f"pmback_{tg.id}"),
            ]])

        # Reply with the card (new message), then expire the parent's buttons
        await q.answer()
        if card_bytes:
            sent = await context.bot.send_photo(
                chat_id=q.message.chat_id,
                photo=io.BytesIO(card_bytes),
                caption=cap,
                parse_mode="HTML",
                reply_markup=kb,
            )
        else:
            sent = await context.bot.send_message(
                chat_id=q.message.chat_id,
                text=cap,
                parse_mode="HTML",
                reply_markup=kb,
            )
        try:
            schedule_button_timeout(
                context, sent.chat_id, sent.message_id, delay_seconds=120,
            )
        except Exception:
            pass

    except Exception:
        logger.exception("playermarket_select_callback error")
        await q.answer("⚠️ Error", show_alert=True)
    finally:
        session.close()


async def playermarket_buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User confirmed buy on the per-player screen."""
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        owner_tg = int(parts[1]); slot = int(parts[2])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return

    if tg.id != owner_tg:
        await q.answer("Not your market!", show_alert=True)
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await q.answer("Do /debut first")
            return

        ok, msg = buy_player(session, user, slot)
        if ok:
            log_activity(session, user.id, "buy_market",
                         f"Bought {msg} from market",
                         coins_change=0, player_name=msg)
            try:
                from services.quest_service import safe_track
                safe_track(session, user.id, "market_buy", 1)
            except Exception:
                pass
            session.commit()
            await q.answer("✅ Purchased!", show_alert=False)

            # Update the buy-screen message to a success state
            try:
                if q.message.caption:
                    await q.edit_message_caption(
                        caption=q.message.caption + f"\n\n✅ <b>Purchased!</b>",
                        parse_mode="HTML",
                        reply_markup=None,
                    )
                else:
                    await q.edit_message_text(
                        text=(q.message.text or "") + f"\n\n✅ <b>Purchased!</b>",
                        parse_mode="HTML",
                        reply_markup=None,
                    )
            except Exception:
                pass

            await context.bot.send_message(
                chat_id=q.message.chat_id,
                text=(f"🎉 <b>{msg}</b> added to your roster!\n"
                      f"💰 Balance: <b>{user.total_coins:,}</b> 🪙\n"
                      f"📊 Roster: {user.roster_count}/25"),
                parse_mode="HTML",
            )

            try:
                from services.achievement_service import check_and_notify
                await check_and_notify(context, q.message.chat_id, session, user.id)
            except Exception:
                pass
        else:
            session.rollback()
            await q.answer(msg, show_alert=True)
    except Exception:
        session.rollback()
        logger.exception("playermarket_buy_callback error")
        await q.answer("⚠️ Error", show_alert=True)
    finally:
        session.close()


async def playermarket_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User tapped 'Back/Cancel' on the per-player screen — just remove the buttons."""
    q = update.callback_query
    tg = q.from_user
    try:
        owner_tg = int(q.data.split("_")[1])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if tg.id != owner_tg:
        await q.answer("Not yours!", show_alert=True)
        return
    await q.answer()
    try:
        if q.message.caption is not None:
            await q.edit_message_caption(
                caption=q.message.caption + "\n\n<i>(Closed)</i>",
                parse_mode="HTML", reply_markup=None,
            )
        else:
            await q.edit_message_text(
                text=(q.message.text or "") + "\n\n<i>(Closed)</i>",
                parse_mode="HTML", reply_markup=None,
            )
    except Exception:
        pass


async def playermarket_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User tapped 'Cancel' on the main grid — close the market."""
    q = update.callback_query
    tg = q.from_user
    try:
        owner_tg = int(q.data.split("_")[1])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if tg.id != owner_tg:
        await q.answer("Not your market!", show_alert=True)
        return
    await q.answer("Closed")
    try:
        if q.message.caption is not None:
            await q.edit_message_caption(
                caption="🌟 <i>Market closed.</i>",
                parse_mode="HTML", reply_markup=None,
            )
        else:
            await q.edit_message_text(
                text="🌟 <i>Market closed.</i>",
                parse_mode="HTML", reply_markup=None,
            )
    except Exception:
        pass


async def playermarket_noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User tapped a sold-out item button."""
    q = update.callback_query
    await q.answer("This slot is sold out!", show_alert=False)

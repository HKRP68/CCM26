"""/playermarket — shared global player market.

Replaces the per-user market with a single shared market visible to all users.
Admin manages slots, prices, and reroll via the website. Everyone sees the
same market.
"""

import io
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, Player
from services.global_market import (
    list_player_market, buy_player, reroll_player_market,
)
from services.card_generator import generate_card
from services.activity_service import log_activity
from services.button_timeout import schedule_button_timeout

logger = logging.getLogger(__name__)


async def playermarket_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        slots = list_player_market(session)

        # If empty, auto-create the first market (admin can reroll later)
        if not slots:
            try:
                reroll_player_market(session)
                session.commit()
                slots = list_player_market(session)
            except Exception:
                logger.exception("Failed initial market generation")
                session.rollback()

        if not slots:
            await update.message.reply_text(
                "❌ Market is currently empty. Check back later!")
            return

        header_lines = [
            "🌟 <b>PLAYER MARKET</b>",
            f"💰 Balance: <b>{user.total_coins:,}</b> 🪙",
            f"📊 {len(slots)} slot(s) — same for all players!",
            "━━━━━━━━━━━━━━━━━━━",
        ]
        sent_messages = []
        header_msg = await update.message.reply_text(
            "\n".join(header_lines), parse_mode="HTML")
        sent_messages.append(header_msg)

        for row in slots:
            player = session.query(Player).get(row.player_id)
            if not player:
                continue
            sold_out = row.purchased_count >= row.quantity
            stock_left = row.quantity - row.purchased_count
            discount_pct = 0
            if row.base_price > row.final_price and row.base_price > 0:
                discount_pct = int((1 - row.final_price / row.base_price) * 100)

            cap_lines = [
                f"<b>#{row.slot_index}.</b> {player.name}",
                f"⭐ {player.rating} OVR | {player.category} | {player.country or '—'}",
            ]
            if discount_pct > 0:
                cap_lines.append(f"💸 <s>{row.base_price:,}</s>  <b>{row.final_price:,}</b> 🪙  <i>(-{discount_pct}%)</i>")
            else:
                cap_lines.append(f"💸 <b>{row.final_price:,}</b> 🪙")
            if row.quantity > 1:
                cap_lines.append(f"📦 Stock: {stock_left}/{row.quantity}")
            cap = "\n".join(cap_lines)

            if sold_out:
                cap += "\n\n❌ <i>Sold out</i>"
                kb = None
            else:
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        f"💰 Buy ({row.final_price:,} 🪙)",
                        callback_data=f"pmbuy_{tg_user.id}_{row.slot_index}",
                    ),
                ]])

            try:
                card_bytes = generate_card(player)
            except Exception:
                card_bytes = None

            try:
                if card_bytes:
                    msg = await update.message.reply_photo(
                        photo=io.BytesIO(card_bytes),
                        caption=cap, parse_mode="HTML", reply_markup=kb,
                    )
                else:
                    msg = await update.message.reply_text(
                        cap, parse_mode="HTML", reply_markup=kb)
                sent_messages.append(msg)
            except Exception:
                logger.exception(f"Failed to send slot {row.slot_index}")

        # Footer
        footer_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Close Market", callback_data=f"pmcancel_{tg_user.id}"),
        ]])
        footer_msg = await update.message.reply_text(
            "⏱ Buttons expire in 2 minutes if unused.",
            parse_mode="HTML", reply_markup=footer_kb,
        )
        sent_messages.append(footer_msg)

        for m in sent_messages:
            try:
                schedule_button_timeout(
                    context, m.chat_id, m.message_id, delay_seconds=120,
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


async def playermarket_buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        owner_tg = int(parts[1]); slot = int(parts[2])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return

    if tg.id != owner_tg:
        await q.answer("Open your own /playermarket!", show_alert=True)
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

            try:
                if q.message.caption:
                    new_cap = q.message.caption + "\n\n✅ <b>Purchased</b>"
                    await q.edit_message_caption(caption=new_cap, parse_mode="HTML",
                                                 reply_markup=None)
                else:
                    new_text = (q.message.text or "") + "\n\n✅ <b>Purchased</b>"
                    await q.edit_message_text(text=new_text, parse_mode="HTML",
                                              reply_markup=None)
            except Exception:
                pass

            await context.bot.send_message(
                chat_id=q.message.chat_id,
                text=f"✅ <b>{msg}</b> added to your roster!\n"
                     f"💰 Balance: <b>{user.total_coins:,}</b> 🪙\n"
                     f"📊 Roster: {user.roster_count}/25",
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


async def playermarket_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await q.edit_message_text(
            "🌟 <i>Player Market closed.</i>", parse_mode="HTML")
    except Exception:
        pass

"""/playermarket — daily 5-slot 87+ player shop at 10% off."""

import io
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, Player
from services.player_market import (
    get_or_refresh_market, buy_from_player_market,
    PLAYER_MARKET_REFRESH_HOURS, PLAYER_MARKET_MIN_RATING, PLAYER_MARKET_DISCOUNT,
)
from services.card_generator import generate_card
from services.activity_service import log_activity
from services.button_timeout import schedule_button_timeout

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# /playermarket
# ═══════════════════════════════════════════════════════════════════════

async def playermarket_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        pairs = get_or_refresh_market(session, user.id)
        session.commit()

        if not pairs:
            await update.message.reply_text(
                f"❌ No {PLAYER_MARKET_MIN_RATING}+ rated players currently available.")
            return

        # Calculate "next refresh" time for footer info
        oldest = min(p[0].refreshed_at for p in pairs)
        from datetime import datetime, timedelta
        next_refresh = oldest + timedelta(hours=PLAYER_MARKET_REFRESH_HOURS)
        delta = next_refresh - datetime.utcnow()
        hrs = max(0, int(delta.total_seconds() // 3600))
        mins = max(0, int((delta.total_seconds() % 3600) // 60))

        # ── Header message ──
        header_lines = [
            "🌟 <b>PLAYER MARKET</b>",
            f"💎 Premium players ({PLAYER_MARKET_MIN_RATING}+) at {int(PLAYER_MARKET_DISCOUNT*100)}% off",
            f"💰 Balance: <b>{user.total_coins:,}</b> 🪙",
            f"🕛 Next refresh: ~{hrs}h {mins}m",
            "━━━━━━━━━━━━━━━━━━━",
        ]

        # Send each player as its own card image with a Buy button
        sent_messages = []

        # Header text first
        header_msg = await update.message.reply_text(
            "\n".join(header_lines), parse_mode="HTML")
        sent_messages.append(header_msg)

        for row, player in pairs:
            cap = (
                f"<b>#{row.slot_index}.</b> {player.name}\n"
                f"⭐ {player.rating} OVR | {player.category} | {player.country or '—'}\n"
                f"💸 <s>{row.base_price:,}</s>  <b>{row.final_price:,}</b> 🪙"
                f"  <i>(-{int(PLAYER_MARKET_DISCOUNT*100)}%)</i>"
            )
            if row.purchased:
                cap += "\n\n✅ <i>Purchased</i>"
                kb = None
            else:
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        f"💰 Buy #{row.slot_index} ({row.final_price:,} 🪙)",
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

        # Footer: cancel button (closes the whole market)
        footer_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Close Market", callback_data=f"pmcancel_{tg_user.id}"),
        ]])
        footer_msg = await update.message.reply_text(
            "⏱ Buttons expire in 2 minutes if unused.",
            parse_mode="HTML", reply_markup=footer_kb,
        )
        sent_messages.append(footer_msg)

        # Schedule 2-minute auto-cleanup for ALL the messages we sent
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


# ═══════════════════════════════════════════════════════════════════════
# Buy callback — pmbuy_<owner_tg>_<slot>
# ═══════════════════════════════════════════════════════════════════════

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
        await q.answer("This isn't your market!", show_alert=True)
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await q.answer("Do /debut first")
            return

        ok, msg = buy_from_player_market(session, user, slot)
        if ok:
            # Need player_name for activity log
            from models import PlayerMarket
            row = (session.query(PlayerMarket)
                   .filter(PlayerMarket.user_id == user.id,
                           PlayerMarket.slot_index == slot).first())
            player = session.query(Player).get(row.player_id) if row else None
            if player:
                log_activity(session, user.id, "buy_market",
                             f"Bought {player.name} ({player.rating}) from market for {row.final_price:,}",
                             coins_change=-row.final_price,
                             player_name=player.name, player_rating=player.rating)
            try:
                from services.quest_service import safe_track
                safe_track(session, user.id, "market_buy", 1)
            except Exception:
                pass
            session.commit()
            await q.answer("✅ Purchased!", show_alert=False)

            # Update the original message to remove buttons + show purchased state
            try:
                from telegram import InlineKeyboardMarkup
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

            # Confirmation message in chat
            await context.bot.send_message(
                chat_id=q.message.chat_id,
                text=f"{msg}\n💰 Balance: <b>{user.total_coins:,}</b> 🪙\n📊 Roster: {user.roster_count}/25",
                parse_mode="HTML",
            )
            # Achievement check
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


# ═══════════════════════════════════════════════════════════════════════
# Cancel callback — pmcancel_<owner_tg>
# ═══════════════════════════════════════════════════════════════════════

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

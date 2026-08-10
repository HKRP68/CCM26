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

from config import MAX_ROSTER
from database import get_session
from models import User, Player
from utils.idempotency import claim_once, release
from services.global_market import (
    list_player_market, buy_player, ensure_player_market_fresh,
    get_next_refresh_at, get_player_refresh_interval_hours,
    is_sold_out, is_unlimited,
)
from services.market_image import generate_market_image
from services.card_generator import generate_card
from services.activity_service import log_activity
from services.button_timeout import schedule_button_timeout
from services.roster_lock import match_lock_alert

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

        # Redirect to Mini App market when configured. Chat-type aware to
        # avoid the WebApp-in-group bug.
        import os as _os
        webapp_url = _os.getenv("WEBAPP_URL", "").strip()
        chat_type = (update.effective_chat.type
                     if update.effective_chat else "private")
        is_private = (chat_type == "private")

        if webapp_url and webapp_url.startswith("https://"):
            from telegram import WebAppInfo
            text = (
                "🌟 <b>Player Market</b>\n\n"
                "Browse today's market and buy in the Mini App for the full "
                "card view, full filters, and a faster experience.\n\n"
                "<i>Tap below to open the market.</i>"
            )
            if is_private:
                btn = InlineKeyboardButton(
                    "🌟 Open Market in Mini App",
                    web_app=WebAppInfo(url=webapp_url + "#market"),
                )
            else:
                bot_username = _os.getenv("BOT_USERNAME", "").strip().lstrip("@")
                miniapp_name = _os.getenv("MINIAPP_NAME", "").strip()
                # Encode the origin group so Mini App actions echo back here.
                _chat_id = update.effective_chat.id if update.effective_chat else None
                # Persist origin server-side too (no initData/frontend dependency).
                if _chat_id is not None and update.effective_user:
                    try:
                        from services.telegram_user_service import record_miniapp_origin
                        record_miniapp_origin(update.effective_user.id, _chat_id)
                    except Exception:
                        pass
                _sp = (f"market_c{_chat_id}"
                       if (_chat_id is not None and _chat_id < 0) else "market")
                if bot_username and miniapp_name:
                    deep = f"https://t.me/{bot_username}/{miniapp_name}?startapp={_sp}"
                elif bot_username:
                    deep = f"https://t.me/{bot_username}?startapp={_sp}"
                else:
                    deep = None
                if deep:
                    btn = InlineKeyboardButton("🌟 Open Market in Mini App", url=deep)
                    text += "\n\n<i>Group chat detected — tap will open in a DM with the bot.</i>"
                else:
                    btn = None

            if btn is not None:
                kb = InlineKeyboardMarkup([[btn]])
                await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
                return

        # Legacy fallback when Mini App isn't configured
        # Auto-refresh if a scheduled refresh has passed (or first run)
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

        # Free, Bronze and Silver see and pay the slot's sell price (== base
        # price); the discount is a membership perk (Platinum 5%, Diamond 10%).
        from services import subscription_service
        discount_pct = subscription_service.market_discount_pct(user)

        def _slot_price(slot):
            return subscription_service.market_price(
                user, slot.base_price, slot.final_price)

        # Build buttons: one per player (their name) + Cancel
        btns = []
        for slot in slots:
            player = session.query(Player).get(slot.player_id)
            if not player:
                continue
            sold = is_sold_out(slot)
            label = f"{'❌ ' if sold else ''}{player.name} • {_slot_price(slot):,} 🪙"
            cb = (f"pmsel_{tg_user.id}_{slot.slot_index}"
                  if not sold else f"pmnoop_{tg_user.id}")
            btns.append([InlineKeyboardButton(label, callback_data=cb)])
        btns.append([InlineKeyboardButton(
            "❌ Cancel", callback_data=f"pmcancel_{tg_user.id}")])

        # How often the market turns over is an admin setting now, so read it
        # rather than promising a day that may not be what's configured.
        every = get_player_refresh_interval_hours(session)
        cadence = "daily" if every >= 24 else f"every {every}h"
        discount_line = (f"🏷️ Your membership: <b>{discount_pct}% off</b> all cards "
                         f"· Refreshes {cadence}"
                         if discount_pct else
                         "🏷️ <b>Platinum</b> 5% / <b>Diamond</b> 10% off all cards "
                         f"· Refreshes {cadence}")
        # Stock is unlimited by default, so say so — the old market sold one
        # copy per card and captains learned to rush it.
        stock_line = ("♾️ <b>Unlimited stock</b> — nothing here sells out"
                      if all(is_unlimited(s) for s in slots) else
                      "📦 Limited stock on some cards — check before you plan")
        caption = (
            f"💰 <b>{user.total_coins:,}</b> 🪙 · "
            f"📊 {user.roster_count}/{MAX_ROSTER} roster\n"
            f"{discount_line}\n{stock_line}"
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
                        f"({p.rating} OVR) — {_slot_price(slot):,} 🪙")
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

        sold = is_sold_out(slot)

        # Free, Bronze and Silver pay the slot's sell price (== base price);
        # Platinum gets 5% off it and Diamond 10%.
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        from services import subscription_service
        sell_price = subscription_service.market_sell_price(
            slot.base_price, slot.final_price)
        price = subscription_service.market_price(user, slot.base_price, slot.final_price)

        # Build the player's full card
        try:
            card_bytes = generate_card(player)
        except Exception:
            card_bytes = None

        cap_lines = [
            f"<b>{player.name}</b>",
            f"⭐ {player.rating} OVR · {player.category} · {player.country or '—'}",
            "",
            f"💸 Price: <b>{price:,}</b> 🪙",
        ]
        if price < sell_price:
            disc = int(round((1 - price / sell_price) * 100))
            cap_lines.insert(3, f"<s>{sell_price:,}</s> 🪙  <i>(-{disc}%)</i>")
        if is_unlimited(slot):
            cap_lines.append("♾️ <i>Unlimited stock — no rush</i>")
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
                    f"✅ Buy ({price:,} 🪙)",
                    callback_data=f"pmbuy_{tg.id}_{slot_index}",
                ),
                InlineKeyboardButton("❌ Cancel",
                                     callback_data=f"pmback_{tg.id}"),
            ]])

        # Reply with the card (new message), then expire the parent's buttons
        await q.answer()
        from services.card_sender import send_player_card
        sent = await send_player_card(
            bot=context.bot, chat_id=q.message.chat_id, player=player,
            caption=cap, reply_markup=kb, session=session,
        )
        if sent is None:
            sent = await context.bot.send_message(
                chat_id=q.message.chat_id, text=cap,
                parse_mode="HTML", reply_markup=kb,
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

    # Dedup rapid taps on this Buy button instance.
    key = f"pmb_{q.message.chat_id}_{q.message.message_id}"
    if not claim_once(key):
        await q.answer("Already processing…")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            release(key)
            await q.answer("Do /debut first")
            return

        locked = match_lock_alert(session, user.id, "buy players")
        if locked:
            release(key)
            await q.answer(locked, show_alert=True)
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
                      f"📊 Roster: {user.roster_count}/{MAX_ROSTER}"),
                parse_mode="HTML",
            )

            try:
                from services.achievement_service import check_and_notify
                await check_and_notify(context, q.message.chat_id, session, user.id)
            except Exception:
                pass
        else:
            session.rollback()
            release(key)
            await q.answer(msg, show_alert=True)
    except Exception:
        # Keep the claim (may be post-commit) so a stale tap can't re-buy.
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

"""/buypack — list packs, view detail, buy.

Workflow per spec:
  /buypack
    → list of pack buttons (1, 2, 3, ...) + Close
  Click a number
    → pack detail with Buy / Cancel / Back
  Click Buy
    → "Opening pack..." → reveal main player card + bonus players
  Click Cancel
    → close (remove buttons)
  Click Back
    → return to list

All buttons expire after 45s. Owner-only triggers.
"""

import asyncio
import io
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, Pack, UserRoster, Player
from services.pack_service import (
    list_packs, open_pack, get_user_pack_purchases_today,
)
from services.card_generator import generate_card
from services.button_timeout import schedule_button_timeout
from services.activity_service import log_activity

logger = logging.getLogger(__name__)

PACK_BUTTON_TIMEOUT = 45  # seconds, per spec


# ════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════

def _format_cost(pack):
    parts = []
    if pack.cost_coins:    parts.append(f"<b>{pack.cost_coins:,}</b> 🪙")
    if pack.cost_quest_points: parts.append(f"<b>{pack.cost_quest_points:,}</b> Quest Points")
    if pack.cost_gems:     parts.append(f"<b>{pack.cost_gems}</b> 💎")
    return " or ".join(parts) if parts else "FREE"


def _can_afford(user, pack):
    """Returns True if user can afford the pack via any of its currencies."""
    if pack.cost_coins and user.total_coins >= pack.cost_coins:
        return True
    if pack.cost_quest_points and (user.quest_points or 0) >= pack.cost_quest_points:
        return True
    if pack.cost_gems and user.total_gems >= pack.cost_gems:
        return True
    if not (pack.cost_coins or pack.cost_quest_points or pack.cost_gems):
        return True  # free
    return False


def _build_list_keyboard(packs, owner_tg):
    """List screen: numbered buttons, two per row, + Close."""
    rows = []
    pair = []
    for p in packs:
        label = f"{p.emoji} {p.slot_number}. {p.name}"
        pair.append(InlineKeyboardButton(
            label, callback_data=f"pkv_{owner_tg}_{p.slot_number}"))
        if len(pair) == 2:
            rows.append(pair); pair = []
    if pair:
        rows.append(pair)
    rows.append([InlineKeyboardButton("❌ Close", callback_data=f"pkc_{owner_tg}")])
    return InlineKeyboardMarkup(rows)


def _build_detail_keyboard(owner_tg, pack, can_afford):
    rows = []
    if can_afford:
        rows.append([InlineKeyboardButton(
            f"💰 Buy", callback_data=f"pkb_{owner_tg}_{pack.slot_number}")])
    else:
        rows.append([InlineKeyboardButton(
            "🔒 Cannot afford", callback_data=f"pknoop_{owner_tg}")])
    rows.append([
        InlineKeyboardButton("◀ Back", callback_data=f"pkbk_{owner_tg}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"pkc_{owner_tg}"),
    ])
    return InlineKeyboardMarkup(rows)


def _list_message(packs, user):
    lines = [
        "📦 <b>PACK STORE</b>",
        f"💰 <b>{user.total_coins:,}</b> 🪙 · "
        f"💎 <b>{user.total_gems}</b> · "
        f"🎯 <b>{user.quest_points or 0}</b> Quest Points",
        "━━━━━━━━━━━━━━━━━━━",
    ]
    if not packs:
        lines.append("\n<i>No packs available right now.</i>")
        return "\n".join(lines)
    for p in packs:
        lines.append(
            f"\n{p.emoji} <b>{p.slot_number}. {p.name}</b>"
            f"  ·  {_format_cost(p)}\n"
            f"   <i>Main: {p.main_count}× ({p.main_min_rating}-{p.main_max_rating})  ·  "
            f"Bonus: {p.bonus_count}× ({p.bonus_min_rating}-{p.bonus_max_rating})</i>"
        )
    lines.append("\nTap a pack to see details.")
    return "\n".join(lines)


def _detail_message(pack, user, bought_today):
    lines = [
        f"{pack.emoji} <b>{pack.name}</b>",
        f"💸 Cost: {_format_cost(pack)}",
        "━━━━━━━━━━━━━━━━━━━",
    ]
    if pack.description:
        lines.append(f"<i>{pack.description}</i>\n")

    # Build the "Main" content line based on filter mode
    mode = (pack.main_filter_mode or "rating").lower()
    versions_label = ""
    if pack.main_versions_json:
        try:
            import json as _j
            vlist = _j.loads(pack.main_versions_json)
            if isinstance(vlist, list) and vlist:
                versions_label = " / ".join(vlist)
        except Exception:
            pass

    if mode == "version":
        main_line = (
            f"   • <b>{pack.main_count}× Main</b>: any "
            f"<i>{versions_label or 'any'}</i> version (any rating)"
        )
    elif mode == "both":
        main_line = (
            f"   • <b>{pack.main_count}× Main</b>: "
            f"<i>{versions_label or 'any'}</i> version, "
            f"rated <b>{pack.main_min_rating}-{pack.main_max_rating}</b>"
        )
    else:  # rating
        main_line = (
            f"   • <b>{pack.main_count}× Main</b> player "
            f"(<b>{pack.main_min_rating}-{pack.main_max_rating}</b> rated)"
        )

    lines.append(
        f"🎁 <b>Contents:</b>\n"
        f"{main_line}\n"
        f"   • <b>{pack.bonus_count}× Bonus</b> player ({pack.bonus_min_rating}-{pack.bonus_max_rating} rated)"
    )

    # Probability hint — only useful in rating/both mode
    if pack.main_weights_json and mode in ("rating", "both"):
        try:
            import json as _j
            weights = _j.loads(pack.main_weights_json)
            ratings = list(range(pack.main_min_rating, pack.main_max_rating + 1))
            if isinstance(weights, list) and len(weights) == len(ratings):
                total = sum(weights) or 1
                probs = "  ".join(
                    f"{r}: <b>{int(w * 100 / total)}%</b>"
                    for r, w in zip(ratings, weights)
                )
                lines.append(f"\n📊 <b>Main odds:</b> {probs}")
        except Exception:
            pass

    if pack.daily_limit:
        remaining = max(0, pack.daily_limit - bought_today)
        lines.append(f"\n⏳ Daily limit: <b>{bought_today}/{pack.daily_limit}</b> "
                     f"(<b>{remaining}</b> remaining today)")

    # Affordability
    lines.append("\n━━━━━━━━━━━━━━━━━━━")
    lines.append(f"💰 <b>{user.total_coins:,}</b> 🪙 · "
                 f"💎 <b>{user.total_gems}</b> · "
                 f"🎯 <b>{user.quest_points or 0}</b> QP")

    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════
# /buypack handler
# ════════════════════════════════════════════════════════════════════

async def buypack_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        packs = list_packs(session, only_active=True)
        if not packs:
            await update.message.reply_text(
                "📦 No packs available right now. Check back soon!")
            return

        text = _list_message(packs, user)
        kb = _build_list_keyboard(packs, tg.id)
        sent = await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

        try:
            schedule_button_timeout(context, sent.chat_id, sent.message_id,
                                    delay_seconds=PACK_BUTTON_TIMEOUT,
                                    custom_text="📦 <i>Pack store closed (45s inactive).</i>")
        except Exception:
            pass

    except Exception:
        logger.exception("buypack_handler error")
        await update.message.reply_text("⚠️ Error loading packs. Try again.")
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# pkv_<owner_tg>_<slot_number>  — view a pack
# ════════════════════════════════════════════════════════════════════

async def pack_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        owner_tg = int(parts[1])
        slot = int(parts[2])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return

    if tg.id != owner_tg:
        await q.answer("Not your pack store!", show_alert=True)
        return

    await q.answer()
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            return
        pack = session.query(Pack).filter(
            Pack.slot_number == slot, Pack.is_active == True).first()
        if not pack:
            await q.answer("Pack not found.", show_alert=True)
            return

        bought_today = get_user_pack_purchases_today(session, user.id, pack.id)
        text = _detail_message(pack, user, bought_today)
        kb = _build_detail_keyboard(tg.id, pack, _can_afford(user, pack))

        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
            # Reset the 45s timer for the new screen
            try:
                schedule_button_timeout(context, q.message.chat_id, q.message.message_id,
                                        delay_seconds=PACK_BUTTON_TIMEOUT,
                                        custom_text="📦 <i>Pack store closed (45s inactive).</i>")
            except Exception:
                pass
        except Exception:
            logger.exception("pack_view_callback edit failed")
    except Exception:
        logger.exception("pack_view_callback error")
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# pkbk_<owner_tg> — back to list
# ════════════════════════════════════════════════════════════════════

async def pack_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            return
        packs = list_packs(session, only_active=True)
        text = _list_message(packs, user)
        kb = _build_list_keyboard(packs, tg.id)
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
            try:
                schedule_button_timeout(context, q.message.chat_id, q.message.message_id,
                                        delay_seconds=PACK_BUTTON_TIMEOUT,
                                        custom_text="📦 <i>Pack store closed (45s inactive).</i>")
            except Exception:
                pass
        except Exception:
            pass
    except Exception:
        logger.exception("pack_back_callback error")
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# pkc_<owner_tg> — close
# ════════════════════════════════════════════════════════════════════

async def pack_close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    await q.answer("Closed")
    try:
        await q.edit_message_text(
            "📦 <i>Pack store closed.</i>",
            parse_mode="HTML",
        )
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════
# pkb_<owner_tg>_<slot> — buy
# ════════════════════════════════════════════════════════════════════

async def pack_buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buy a pack — adds to user's inventory. Players are rolled later via /openpack."""
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        owner_tg = int(parts[1])
        slot = int(parts[2])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return

    if tg.id != owner_tg:
        await q.answer("Not your pack store!", show_alert=True)
        return

    await q.answer()
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await context.bot.send_message(q.message.chat_id, "❌ Do /debut first!")
            return
        pack = session.query(Pack).filter(
            Pack.slot_number == slot, Pack.is_active == True).first()
        if not pack:
            await q.answer("Pack not found.", show_alert=True)
            return

        from services.pack_service import buy_pack, list_user_inventory
        result = buy_pack(session, user, pack)
        if not result["success"]:
            session.rollback()
            try:
                await q.edit_message_text(result["message"], parse_mode="HTML")
            except Exception:
                pass
            return

        session.commit()

        # Activity log
        try:
            log_activity(session, user.id, "buy_pack",
                         f"Bought {pack.name} (added to inventory)",
                         coins_change=-(result["cost_paid"] if result["currency"] == "coins" else 0))
            session.commit()
        except Exception:
            session.rollback()

        # Quest tracking — fires on purchase
        try:
            from services.quest_service import safe_track
            safe_track(session, user.id, "pack_buy", 1)
            session.commit()
        except Exception:
            session.rollback()

        inventory = list_user_inventory(session, user.id)
        currency_label = {
            "coins": "🪙 coins", "quest_points": "Quest Points",
            "gems": "💎 gems", "free": "FREE",
        }.get(result["currency"], "")
        bal_lines = [f"💰 Balance: <b>{user.total_coins:,}</b> 🪙"]
        if pack.cost_quest_points:
            bal_lines.append(f"🎯 Quest Points: <b>{user.quest_points or 0:,}</b>")
        if pack.cost_gems:
            bal_lines.append(f"💎 Gems: <b>{user.total_gems}</b>")

        text = (
            f"✅ <b>{pack.emoji} {pack.name}</b> added to your inventory!\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"💸 Spent: <b>{result['cost_paid']:,}</b> {currency_label}\n"
            + "\n".join(bal_lines) + "\n"
            f"📦 Inventory: <b>{len(inventory)}/50</b> unopened packs\n\n"
            f"<b>👉 Use /openpack to open it!</b>"
        )
        try:
            await q.edit_message_text(text, parse_mode="HTML")
        except Exception:
            await context.bot.send_message(q.message.chat_id, text, parse_mode="HTML")

    except Exception:
        session.rollback()
        logger.exception("pack_buy_callback error")
        try:
            await context.bot.send_message(
                q.message.chat_id, "⚠️ Error buying pack. Try again.")
        except Exception:
            pass
    finally:
        session.close()


async def pack_noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """For sold-out / cannot-afford buttons."""
    q = update.callback_query
    await q.answer("You don't have enough to buy this pack.", show_alert=True)


# ════════════════════════════════════════════════════════════════════
# /openpack — open packs from inventory with reveal animation
# ════════════════════════════════════════════════════════════════════

# Country → flag emoji
COUNTRY_FLAGS = {
    "India": "🇮🇳", "Australia": "🇦🇺", "England": "🏴󠁧󠁢󠁥󠁮󠁧󠁿",
    "Pakistan": "🇵🇰", "New Zealand": "🇳🇿", "South Africa": "🇿🇦",
    "West Indies": "🌴", "Sri Lanka": "🇱🇰", "Bangladesh": "🇧🇩",
    "Afghanistan": "🇦🇫", "Zimbabwe": "🇿🇼", "Ireland": "🇮🇪",
    "Netherlands": "🇳🇱", "Scotland": "🏴󠁧󠁢󠁳󠁣󠁴󠁿", "Nepal": "🇳🇵",
    "UAE": "🇦🇪", "USA": "🇺🇸", "Canada": "🇨🇦", "Oman": "🇴🇲",
    "Namibia": "🇳🇦", "Kenya": "🇰🇪",
}


def _flag_for(country: str) -> str:
    return COUNTRY_FLAGS.get(country, "🏏")


def _rating_glow(rating: int) -> str:
    """Sparkle decoration that scales with rating tier."""
    if rating >= 95: return "💎✨💎"
    if rating >= 90: return "⭐✨⭐"
    if rating >= 85: return "🌟"
    if rating >= 80: return "✨"
    return "·"


def _build_openpack_list_kb(inventory, owner_tg):
    """Buttons: 'Open Pack 1 (Bronze)' etc., one per row, plus Close."""
    rows = []
    for idx, (inv, pack) in enumerate(inventory, start=1):
        label = f"📦 Open Pack {idx} ({pack.name})"
        rows.append([InlineKeyboardButton(
            label, callback_data=f"opk_{owner_tg}_{inv.id}")])
    rows.append([InlineKeyboardButton("❌ Close", callback_data=f"opkc_{owner_tg}")])
    return InlineKeyboardMarkup(rows)


def _openpack_list_text(inventory, user):
    if not inventory:
        return (
            "📦 <b>YOUR PACK INVENTORY</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n\n"
            "<i>You have no unopened packs.</i>\n\n"
            "👉 Use /buypack to grab some!"
        )

    # Stack same-pack counts
    from collections import Counter
    pack_counts = Counter(pack.name for _, pack in inventory)
    summary = "  ·  ".join(
        f"{name} ×{c}" if c > 1 else name
        for name, c in pack_counts.most_common()
    )

    lines = [
        "📦 <b>YOUR PACK INVENTORY</b>",
        f"<i>{len(inventory)}/50 unopened</i>  ·  {summary}",
        "━━━━━━━━━━━━━━━━━━━",
    ]
    for idx, (inv, pack) in enumerate(inventory, start=1):
        age = ""
        try:
            from datetime import datetime as _dt
            delta = _dt.utcnow() - inv.acquired_at
            if delta.days >= 1:
                age = f" · {delta.days}d ago"
            elif delta.seconds >= 3600:
                age = f" · {delta.seconds // 3600}h ago"
            else:
                age = " · just now"
        except Exception:
            pass
        lines.append(
            f"\n<b>{idx}.</b> {pack.emoji} <b>{pack.name}</b>{age}"
        )
    lines.append("\nTap a pack to open it. 🎴")
    return "\n".join(lines)


async def openpack_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return
        from services.pack_service import list_user_inventory
        inventory = list_user_inventory(session, user.id)
        text = _openpack_list_text(inventory, user)
        kb = _build_openpack_list_kb(inventory, tg.id)
        sent = await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
        try:
            schedule_button_timeout(context, sent.chat_id, sent.message_id,
                                    delay_seconds=PACK_BUTTON_TIMEOUT,
                                    custom_text="📦 <i>Closed (45s inactive). Run /openpack again.</i>")
        except Exception:
            pass
    except Exception:
        logger.exception("openpack_handler error")
        await update.message.reply_text("⚠️ Error loading inventory. Try again.")
    finally:
        session.close()


async def pack_open_close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """opkc_<owner_tg> — close /openpack listing."""
    q = update.callback_query
    tg = q.from_user
    try:
        owner_tg = int(q.data.split("_")[1])
    except (IndexError, ValueError):
        await q.answer("Invalid"); return
    if tg.id != owner_tg:
        await q.answer("Not yours!", show_alert=True); return
    await q.answer("Closed")
    try:
        await q.edit_message_text("📦 <i>Pack inventory closed.</i>", parse_mode="HTML")
    except Exception:
        pass


async def pack_open_inventory_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """opk_<owner_tg>_<inventory_id> — open one pack with reveal animation."""
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        owner_tg = int(parts[1])
        inventory_id = int(parts[2])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return

    if tg.id != owner_tg:
        await q.answer("Not your pack!", show_alert=True)
        return

    await q.answer()
    session = get_session()
    chat_id = q.message.chat_id

    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await context.bot.send_message(chat_id, "❌ Do /debut first!")
            return

        from services.pack_service import open_unopened_pack
        result = open_unopened_pack(session, user, inventory_id)
        if not result["success"]:
            session.rollback()
            try:
                await q.edit_message_text(result["message"], parse_mode="HTML")
            except Exception:
                pass
            return

        pack = result["pack"]
        rolled = result["players"]
        added = result["added"]
        to_claim = result["players_to_claim"]
        pity_triggered = result.get("pity_triggered", False)
        main_players = [(p, s) for p, s in rolled if s == "main"]
        bonus_players = [(p, s) for p, s in rolled if s == "bonus"]
        added_ids = {p.id for p, _ in added}

        session.commit()

        # ── Animation: replace the listing with a sealed-pack message ──
        opening_text = (
            f"🎴 <b>OPENING…</b>\n\n"
            f"{pack.emoji} <b>{pack.name}</b>\n\n"
            f"<i>The seal cracks open…</i>"
        )
        if pity_triggered:
            opening_text += (
                f"\n\n💎 <b>PITY PULL ACTIVE!</b>\n"
                f"<i>Guaranteed max-rating main!</i>"
            )
        try:
            await q.edit_message_text(opening_text, parse_mode="HTML")
        except Exception:
            pass
        await asyncio.sleep(1.5)

        # Helper to safely edit a message and silently ignore errors
        async def _edit(msg, text):
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=msg.message_id,
                    text=text, parse_mode="HTML",
                )
            except Exception:
                pass

        # ── Reveal each MAIN player with a slow per-stat animation ──
        for idx, (player, _slot) in enumerate(main_players, start=1):
            # 1. Anticipation seed
            seed = await context.bot.send_message(
                chat_id, "🎴 <b>SHUFFLING…</b>", parse_mode="HTML")
            await asyncio.sleep(1.0)

            # 2. Country reveal
            flag = _flag_for(player.country)
            await _edit(seed,
                f"🌍 <b>NATION REVEALED</b>\n\n{flag} <b>{player.country.upper()}</b>")
            await asyncio.sleep(1.2)

            # 3. Role reveal
            role_icon = {"Batsman": "🏏", "Bowler": "🎳",
                         "All-Rounder": "⚡", "Wicket-Keeper": "🧤"}.get(
                player.category, "🌟")
            await _edit(seed,
                f"🌍 <b>{flag} {player.country.upper()}</b>\n"
                f"🎯 <b>ROLE</b>\n\n{role_icon} <b>{player.category.upper()}</b>")
            await asyncio.sleep(1.2)

            # 4. Style reveal
            bat_h = (player.bat_hand or "?").upper()
            bowl_style = (player.bowl_style or "—")
            bowl_h = (player.bowl_hand or "")
            style_line = f"{bat_h}-Hand Bat"
            if bowl_style and bowl_style != "—":
                style_line += f"  ·  {bowl_h} {bowl_style}"
            await _edit(seed,
                f"🌍 <b>{flag} {player.country.upper()}</b>\n"
                f"🎯 {role_icon} <b>{player.category.upper()}</b>\n"
                f"🎨 <b>STYLE</b>\n\n{style_line}")
            await asyncio.sleep(1.0)

            # 5. Anticipation frame before rating (gold sparkle for high tier)
            await _edit(seed,
                f"🌍 <b>{flag} {player.country.upper()}</b>\n"
                f"🎯 {role_icon} <b>{player.category.upper()}</b>\n"
                f"🎨 {style_line}\n\n"
                f"⭐ <b>RATING…</b>  ✨ ✨ ✨")
            await asyncio.sleep(0.8)

            # 6. Rating reveal with glow
            glow = _rating_glow(player.rating)
            await _edit(seed,
                f"🌍 <b>{flag} {player.country.upper()}</b>\n"
                f"🎯 {role_icon} <b>{player.category.upper()}</b>\n"
                f"🎨 {style_line}\n\n"
                f"{glow}  <b>{player.rating} OVR</b>  {glow}")
            await asyncio.sleep(1.5)

            roster_status = ("✅ Added to your roster"
                             if player.id in added_ids
                             else "⚠️ Squad full — release some via /releasepl")
            cap = (
                f"⭐ <b>MAIN PULL #{idx}</b>\n"
                f"🌟 <b>{player.name}</b> — <b>{player.rating}</b> OVR\n"
                f"{flag} {player.country}  ·  {role_icon} {player.category}\n"
                f"{roster_status}"
            )
            try:
                from services.card_sender import send_player_card
                await send_player_card(
                    bot=context.bot, chat_id=chat_id, player=player,
                    caption=cap, session=session,
                )
            except Exception:
                logger.exception("send card failed")

        # ── Reveal BONUS players faster (compact, one message per player) ──
        if bonus_players:
            await asyncio.sleep(0.3)
            await context.bot.send_message(
                chat_id, "🎁 <b>BONUS PLAYERS…</b>", parse_mode="HTML")
            await asyncio.sleep(0.6)
            for player, _slot in bonus_players:
                flag = _flag_for(player.country)
                role_icon = {"Batsman": "🏏", "Bowler": "🎳",
                             "All-Rounder": "⚡", "Wicket-Keeper": "🧤"}.get(
                    player.category, "🌟")
                marker = "✅" if player.id in added_ids else "⚠️"
                seed = await context.bot.send_message(
                    chat_id, f"{flag} <b>{player.country.upper()}</b>",
                    parse_mode="HTML")
                await asyncio.sleep(0.7)
                await _edit(seed,
                    f"{flag} <b>{player.country.upper()}</b>  ·  "
                    f"{role_icon} {player.category}\n"
                    f"{marker} <b>{player.name}</b> — <b>{player.rating} OVR</b>")
                await asyncio.sleep(0.5)

        # ── Final summary ──
        from services.pack_service import list_user_inventory
        remaining_inv = list_user_inventory(session, user.id)
        summary = [
            f"{pack.emoji} <b>{pack.name}</b> — opened!",
            "━━━━━━━━━━━━━━━━━━━",
            f"📊 Roster: <b>{user.roster_count}/25</b>",
            f"📦 Inventory: <b>{len(remaining_inv)}/50</b> unopened packs",
        ]
        if to_claim:
            summary.append(
                f"\n⚠️ <b>{len(to_claim)} player{'s' if len(to_claim) != 1 else ''} didn't fit</b> — "
                f"release some with /releasepl, then re-pull from /myroster.")
        if remaining_inv:
            summary.append(f"\n👉 Use /openpack again to open the next one.")

        await asyncio.sleep(0.3)
        await context.bot.send_message(
            chat_id, "\n".join(summary), parse_mode="HTML")

        # Activity log + quest tracking
        try:
            names = ", ".join(p.name for p, _ in rolled)
            log_activity(session, user.id, "open_pack",
                         f"Opened {pack.name}: {names}", coins_change=0)
            session.commit()
        except Exception:
            session.rollback()
        try:
            from services.quest_service import safe_track
            safe_track(session, user.id, "pack_open", 1)
            session.commit()
        except Exception:
            session.rollback()

    except Exception:
        session.rollback()
        logger.exception("pack_open_inventory_callback error")
        try:
            await context.bot.send_message(
                chat_id, "⚠️ Error opening pack. Pack remains in inventory.")
        except Exception:
            pass
    finally:
        session.close()


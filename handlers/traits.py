"""Trait command handlers.

Commands:
  /traits             — show all your players and their traits + inventory
  /traitshop          — daily 5-slot gem shop
  /traitbuy <slot>    — buy trait from shop slot
  /traitreroll        — reroll the shop (30 gems)
  /traitapply         — apply an inventory trait to a player (callback picker)
  /traitupgrade       — upgrade a trait on a player (callback picker)
  /traitreplace       — replace a trait on a player (callback picker)
"""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, Player, UserRoster, Trait, PlayerTrait, TraitInventory, TraitDaily
from services.trait_service import (
    TRAIT_DEFINITIONS, refresh_shop, reroll_shop, buy_trait_from_shop,
    apply_trait_to_player, replace_trait_on_player,
    upgrade_player_trait, upgrade_inventory_trait,
    get_player_traits, _today_key, _get_or_create_daily,
)
from services.flags import get_flag
from config import (
    TRAIT_SHOP_DAILY_PURCHASE_LIMIT, TRAIT_REROLL_COST,
    TRAIT_UPGRADE_COSTS, TRAIT_REPLACE_COST,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# /traits — overview of player traits and inventory
# ═══════════════════════════════════════════════════════════════════════

async def traits_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        # Inventory
        inv_rows = (session.query(TraitInventory, Trait)
                    .join(Trait, TraitInventory.trait_id == Trait.id)
                    .filter(TraitInventory.user_id == user.id)
                    .order_by(TraitInventory.acquired_at).all())

        # Equipped traits grouped by player
        equipped = (session.query(PlayerTrait, Trait, UserRoster, Player)
                    .join(Trait, PlayerTrait.trait_id == Trait.id)
                    .join(UserRoster, PlayerTrait.roster_id == UserRoster.id)
                    .join(Player, UserRoster.player_id == Player.id)
                    .filter(PlayerTrait.user_id == user.id)
                    .order_by(UserRoster.order_position).all())

        lines = [
            "✨ <b>YOUR TRAITS</b>",
            f"💎 Gems: <b>{user.total_gems:,}</b>",
            "━━━━━━━━━━━━━━━━━━━",
        ]

        # Inventory
        if inv_rows:
            lines.append("📦 <b>INVENTORY</b>")
            for inv, t in inv_rows:
                max_badge = " 🌟" if inv.level == 5 else ""
                lines.append(f"  <code>#{inv.id}</code> {t.emoji} {t.name} Lv.{inv.level}{max_badge}")
            lines.append("<i>Use /traitapply to attach to a player</i>")
            lines.append("")
        else:
            lines.append("📦 <i>Inventory empty — buy traits via /traitshop</i>")
            lines.append("")

        # Equipped traits
        if equipped:
            lines.append("🏏 <b>EQUIPPED TRAITS</b>")
            # Group by roster_id
            by_roster = {}
            for pt, t, ur, p in equipped:
                by_roster.setdefault((ur.id, p.name, p.country, ur.order_position), []).append((pt, t))
            for (rid, pname, pcountry, pos), traits in sorted(by_roster.items(), key=lambda x: x[0][3] or 999):
                flag = get_flag(pcountry) if pcountry else ""
                lines.append(f"\n<b>#{pos}. {pname}</b> {flag}")
                for pt, t in traits:
                    max_badge = " 🌟" if pt.level == 5 else ""
                    lines.append(f"  <code>#{pt.id}</code> {t.emoji} {t.name} Lv.{pt.level}{max_badge}")
            lines.append("")
        else:
            lines.append("🏏 <i>No equipped traits yet</i>\n")

        # Shortcuts
        lines.append("━━━━━━━━━━━━━━━━━━━")
        lines.append("🏪 /traitshop — buy traits")
        lines.append("🎯 /traitapply — attach to player")
        lines.append("⬆️ /traitupgrade — level up")
        lines.append("🔄 /traitreplace — swap trait")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    except Exception:
        logger.exception("traits_handler error")
        await update.message.reply_text("⚠️ Error loading traits.")
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════════════════
# /traitshop — daily shop
# ═══════════════════════════════════════════════════════════════════════

async def traitshop_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        rows = refresh_shop(session, user.id)
        session.commit()
        daily = _get_or_create_daily(session, user.id)
        remaining = max(0, TRAIT_SHOP_DAILY_PURCHASE_LIMIT - daily.purchases)

        lines = [
            "🏪 <b>TRAIT MARKET</b>",
            f"💎 Gems: <b>{user.total_gems:,}</b>  |  Purchases left today: <b>{remaining}/{TRAIT_SHOP_DAILY_PURCHASE_LIMIT}</b>",
            "━━━━━━━━━━━━━━━━━━━",
        ]

        btns = []
        for row in rows:
            trait = session.query(Trait).get(row.trait_id)
            if not trait:
                continue
            status = " ✅" if row.purchased else ""
            discount_tag = f"  🏷️ -{row.discount_pct}%" if row.discount_pct > 0 else ""
            lines.append(
                f"\n<b>Slot {row.slot_index + 1}.</b> {trait.emoji} <b>{trait.name}</b> "
                f"Lv.1 — <b>{row.final_price}</b> 💎{discount_tag}{status}\n"
                f"   <i>{trait.description}</i>"
            )
            if not row.purchased:
                # Embed owner tg_id in callback so we can verify on click
                btns.append([InlineKeyboardButton(
                    f"Buy Slot {row.slot_index + 1}: {trait.emoji} {trait.name} ({row.final_price} 💎)",
                    callback_data=f"trbuy_{tg.id}_{row.slot_index}"
                )])

        if btns:
            btns.append([
                InlineKeyboardButton(f"🔄 Reroll ({TRAIT_REROLL_COST} 💎)",
                                     callback_data=f"trreroll_{tg.id}"),
                InlineKeyboardButton("❌ Cancel",
                                     callback_data=f"trshopcancel_{tg.id}"),
            ])

        lines.append("\n━━━━━━━━━━━━━━━━━━━")
        lines.append("🕛 Shop refreshes every 24 hours.")
        lines.append("⏱ Buttons expire in 60 seconds if unused.")

        kb = InlineKeyboardMarkup(btns) if btns else None
        sent = await update.message.reply_text(
            "\n".join(lines), parse_mode="HTML", reply_markup=kb)

        # 60s auto-expire
        if kb:
            from services.button_timeout import schedule_button_timeout
            schedule_button_timeout(
                context, sent.chat_id, sent.message_id,
                delay_seconds=60,
                custom_text="🏪 <b>TRAIT MARKET</b>\n\n⏱ <i>Shop session expired. Type /traitshop to reopen.</i>",
                timeout_key=f"trshop_{tg.id}_{sent.message_id}",
            )

    except Exception:
        logger.exception("traitshop_handler error")
        await update.message.reply_text("⚠️ Error loading shop.")
    finally:
        session.close()


async def traitbuy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: trbuy_<owner_tg_id>_<slot_index>"""
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
        await q.answer("This is not your shop! Use /traitshop to open your own.",
                       show_alert=True)
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await q.answer("Do /debut first")
            return

        ok, msg, inv = buy_trait_from_shop(session, user, slot)
        if ok:
            session.commit()
            await q.answer(msg, show_alert=False)
            await _refresh_shop_display(q, session, user)
        else:
            session.rollback()
            await q.answer(msg, show_alert=True)
    except Exception:
        session.rollback()
        logger.exception("traitbuy_callback error")
        await q.answer("⚠️ Error", show_alert=True)
    finally:
        session.close()


async def traitreroll_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: trreroll_<owner_tg_id>"""
    q = update.callback_query
    tg = q.from_user
    try:
        owner_tg = int(q.data.split("_")[1])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if tg.id != owner_tg:
        await q.answer("Not your shop!", show_alert=True)
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await q.answer("Do /debut first")
            return
        ok, msg = reroll_shop(session, user)
        if ok:
            session.commit()
            await q.answer(msg)
            await _refresh_shop_display(q, session, user)
        else:
            session.rollback()
            await q.answer(msg, show_alert=True)
    except Exception:
        session.rollback()
        logger.exception("traitreroll_callback error")
        await q.answer("⚠️ Error", show_alert=True)
    finally:
        session.close()


async def traitshop_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: trshopcancel_<owner_tg_id> — closes shop, removes all buttons."""
    q = update.callback_query
    tg = q.from_user
    try:
        owner_tg = int(q.data.split("_")[1])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if tg.id != owner_tg:
        await q.answer("Not your shop!", show_alert=True)
        return
    await q.answer("Shop closed")
    try:
        await q.edit_message_text(
            "🏪 <b>TRAIT MARKET</b>\n\n❌ <i>Shop closed.</i>\nType /traitshop to reopen.",
            parse_mode="HTML")
    except Exception:
        pass


async def _refresh_shop_display(q, session, user):
    """Re-render the shop message after a buy/reroll."""
    rows = refresh_shop(session, user.id)
    session.commit()
    daily = _get_or_create_daily(session, user.id)
    remaining = max(0, TRAIT_SHOP_DAILY_PURCHASE_LIMIT - daily.purchases)
    owner_tg = user.telegram_id

    lines = [
        "🏪 <b>TRAIT MARKET</b>",
        f"💎 Gems: <b>{user.total_gems:,}</b>  |  Purchases left today: <b>{remaining}/{TRAIT_SHOP_DAILY_PURCHASE_LIMIT}</b>",
        "━━━━━━━━━━━━━━━━━━━",
    ]
    btns = []
    for row in rows:
        trait = session.query(Trait).get(row.trait_id)
        if not trait:
            continue
        status = " ✅" if row.purchased else ""
        discount_tag = f"  🏷️ -{row.discount_pct}%" if row.discount_pct > 0 else ""
        lines.append(
            f"\n<b>Slot {row.slot_index + 1}.</b> {trait.emoji} <b>{trait.name}</b> "
            f"Lv.1 — <b>{row.final_price}</b> 💎{discount_tag}{status}\n"
            f"   <i>{trait.description}</i>"
        )
        if not row.purchased:
            btns.append([InlineKeyboardButton(
                f"Buy Slot {row.slot_index + 1}: {trait.emoji} {trait.name} ({row.final_price} 💎)",
                callback_data=f"trbuy_{owner_tg}_{row.slot_index}"
            )])
    if btns:
        btns.append([
            InlineKeyboardButton(f"🔄 Reroll ({TRAIT_REROLL_COST} 💎)",
                                 callback_data=f"trreroll_{owner_tg}"),
            InlineKeyboardButton("❌ Cancel",
                                 callback_data=f"trshopcancel_{owner_tg}"),
        ])
    lines.append("\n━━━━━━━━━━━━━━━━━━━")
    lines.append("🕛 Shop refreshes every 24 hours.")
    lines.append("⏱ Buttons expire in 60 seconds if unused.")

    try:
        await q.edit_message_text(
            "\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(btns) if btns else None,
        )
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════
# /traitapply — pick inventory trait → pick player
# ═══════════════════════════════════════════════════════════════════════

async def traitapply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return
        inv_rows = (session.query(TraitInventory, Trait)
                    .join(Trait, TraitInventory.trait_id == Trait.id)
                    .filter(TraitInventory.user_id == user.id)
                    .all())
        if not inv_rows:
            await update.message.reply_text(
                "📦 Your inventory is empty. Buy traits via /traitshop first.")
            return

        btns = []
        for inv, t in inv_rows:
            btns.append([InlineKeyboardButton(
                f"{t.emoji} {t.name} Lv.{inv.level}",
                callback_data=f"trapply_inv_{inv.id}"
            )])
        await update.message.reply_text(
            "🎯 <b>APPLY TRAIT</b>\n\nChoose a trait from your inventory:",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    except Exception:
        logger.exception("traitapply_handler error")
        await update.message.reply_text("⚠️ Error.")
    finally:
        session.close()


async def trapply_inv_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """After picking inventory trait → show player picker. Callback: trapply_inv_<inv_id>"""
    q = update.callback_query
    tg = q.from_user
    try:
        inv_id = int(q.data.split("_")[2])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await q.answer("Not authorized")
            return
        inv = session.query(TraitInventory).filter(
            TraitInventory.id == inv_id, TraitInventory.user_id == user.id).first()
        if not inv:
            await q.answer("Trait not found")
            try: await q.edit_message_text("❌ Trait no longer in inventory.")
            except Exception: pass
            return

        trait = session.query(Trait).get(inv.trait_id)
        await q.answer()

        # Show top-11 players (the most likely targets)
        roster = (session.query(UserRoster, Player)
                  .join(Player, UserRoster.player_id == Player.id)
                  .filter(UserRoster.user_id == user.id)
                  .order_by(UserRoster.order_position)
                  .limit(15).all())

        btns = []
        for ur, p in roster:
            # Show current trait count
            count = session.query(PlayerTrait).filter(PlayerTrait.roster_id == ur.id).count()
            label = f"#{ur.order_position} {p.name} ({count}/3)"
            btns.append([InlineKeyboardButton(label, callback_data=f"trapply_pl_{inv_id}_{ur.id}")])

        btns.append([InlineKeyboardButton("❌ Cancel", callback_data="trcancel")])

        await q.edit_message_text(
            f"🎯 Applying <b>{trait.emoji} {trait.name} Lv.{inv.level}</b>\n\n"
            f"Pick a player:",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    except Exception:
        logger.exception("trapply_inv_callback error")
        await q.answer("⚠️ Error", show_alert=True)
    finally:
        session.close()


async def trapply_pl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Apply the trait. Callback: trapply_pl_<inv_id>_<roster_id>"""
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        inv_id = int(parts[2]); roster_id = int(parts[3])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await q.answer("Not authorized")
            return
        ok, msg = apply_trait_to_player(session, user, inv_id, roster_id)
        if ok:
            session.commit()
            await q.answer("Applied!")
            try: await q.edit_message_text(f"✅ {msg}", parse_mode="HTML")
            except Exception: pass
        else:
            session.rollback()
            await q.answer(msg, show_alert=True)
    except Exception:
        session.rollback()
        logger.exception("trapply_pl_callback error")
        await q.answer("⚠️ Error", show_alert=True)
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════════════════
# /traitupgrade — pick equipped trait → upgrade
# ═══════════════════════════════════════════════════════════════════════

async def traitupgrade_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        # Get all equipped traits that are not at max level
        equipped = (session.query(PlayerTrait, Trait, UserRoster, Player)
                    .join(Trait, PlayerTrait.trait_id == Trait.id)
                    .join(UserRoster, PlayerTrait.roster_id == UserRoster.id)
                    .join(Player, UserRoster.player_id == Player.id)
                    .filter(PlayerTrait.user_id == user.id,
                            PlayerTrait.level < 5)
                    .all())

        # Also include inventory traits
        inv_rows = (session.query(TraitInventory, Trait)
                    .join(Trait, TraitInventory.trait_id == Trait.id)
                    .filter(TraitInventory.user_id == user.id,
                            TraitInventory.level < 5)
                    .all())

        if not equipped and not inv_rows:
            await update.message.reply_text(
                "❌ No upgradeable traits. "
                "Either buy some via /traitshop or all your traits are at Max Level.")
            return

        btns = []
        lines = [f"⬆️ <b>UPGRADE TRAIT</b>  |  💎 {user.total_gems:,}", "━━━━━━━━━━━━━━━━━━━"]

        if equipped:
            lines.append("<b>Equipped:</b>")
            for pt, t, ur, p in equipped:
                cost = TRAIT_UPGRADE_COSTS.get(pt.level, 0)
                lines.append(f"  {t.emoji} {t.name} Lv.{pt.level} on {p.name}  →  Lv.{pt.level + 1} ({cost} 💎)")
                btns.append([InlineKeyboardButton(
                    f"⬆️ {t.emoji} {t.name} Lv.{pt.level}→{pt.level + 1} on {p.name} ({cost} 💎)",
                    callback_data=f"trup_pt_{pt.id}"
                )])

        if inv_rows:
            lines.append("\n<b>Inventory (not equipped):</b>")
            for inv, t in inv_rows:
                cost = TRAIT_UPGRADE_COSTS.get(inv.level, 0)
                lines.append(f"  {t.emoji} {t.name} Lv.{inv.level}  →  Lv.{inv.level + 1} ({cost} 💎)")
                btns.append([InlineKeyboardButton(
                    f"⬆️ {t.emoji} {t.name} Lv.{inv.level}→{inv.level + 1} (INV) ({cost} 💎)",
                    callback_data=f"trup_inv_{inv.id}"
                )])

        btns.append([InlineKeyboardButton("❌ Cancel", callback_data="trcancel")])
        await update.message.reply_text(
            "\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    except Exception:
        logger.exception("traitupgrade_handler error")
        await update.message.reply_text("⚠️ Error.")
    finally:
        session.close()


async def trup_pt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Upgrade equipped trait. Callback: trup_pt_<player_trait_id>"""
    q = update.callback_query
    tg = q.from_user
    try:
        pt_id = int(q.data.split("_")[2])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await q.answer("Not authorized")
            return
        ok, msg = upgrade_player_trait(session, user, pt_id)
        if ok:
            session.commit()
            await q.answer("Upgraded!")
            try: await q.edit_message_text(msg, parse_mode="HTML")
            except Exception: pass
        else:
            session.rollback()
            await q.answer(msg, show_alert=True)
    except Exception:
        session.rollback()
        logger.exception("trup_pt_callback error")
        await q.answer("⚠️ Error", show_alert=True)
    finally:
        session.close()


async def trup_inv_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Upgrade inventory trait. Callback: trup_inv_<inv_id>"""
    q = update.callback_query
    tg = q.from_user
    try:
        inv_id = int(q.data.split("_")[2])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await q.answer("Not authorized")
            return
        ok, msg = upgrade_inventory_trait(session, user, inv_id)
        if ok:
            session.commit()
            await q.answer("Upgraded!")
            try: await q.edit_message_text(msg, parse_mode="HTML")
            except Exception: pass
        else:
            session.rollback()
            await q.answer(msg, show_alert=True)
    except Exception:
        session.rollback()
        logger.exception("trup_inv_callback error")
        await q.answer("⚠️ Error", show_alert=True)
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════════════════
# /traitreplace — swap equipped trait with inventory trait
# ═══════════════════════════════════════════════════════════════════════

async def traitreplace_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        equipped = (session.query(PlayerTrait, Trait, UserRoster, Player)
                    .join(Trait, PlayerTrait.trait_id == Trait.id)
                    .join(UserRoster, PlayerTrait.roster_id == UserRoster.id)
                    .join(Player, UserRoster.player_id == Player.id)
                    .filter(PlayerTrait.user_id == user.id).all())

        if not equipped:
            await update.message.reply_text(
                "❌ No equipped traits to replace. Apply some first with /traitapply.")
            return

        inv_rows = (session.query(TraitInventory).filter(
            TraitInventory.user_id == user.id).count())
        if inv_rows == 0:
            await update.message.reply_text(
                "❌ No inventory traits to replace with. Buy one via /traitshop first.")
            return

        btns = []
        lines = [
            f"🔄 <b>REPLACE TRAIT</b>",
            f"💎 Gems: {user.total_gems:,}  |  Cost: <b>{TRAIT_REPLACE_COST} 💎</b>",
            "━━━━━━━━━━━━━━━━━━━",
            "Pick the EQUIPPED trait to replace:",
        ]
        for pt, t, ur, p in equipped:
            btns.append([InlineKeyboardButton(
                f"{t.emoji} {t.name} Lv.{pt.level} on {p.name}",
                callback_data=f"trrep_pt_{pt.id}"
            )])
        btns.append([InlineKeyboardButton("❌ Cancel", callback_data="trcancel")])
        await update.message.reply_text(
            "\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    except Exception:
        logger.exception("traitreplace_handler error")
        await update.message.reply_text("⚠️ Error.")
    finally:
        session.close()


async def trrep_pt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """After picking the equipped trait, show inventory picker.
    Callback: trrep_pt_<player_trait_id>"""
    q = update.callback_query
    tg = q.from_user
    try:
        pt_id = int(q.data.split("_")[2])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await q.answer("Not authorized")
            return
        pt = session.query(PlayerTrait).filter(
            PlayerTrait.id == pt_id, PlayerTrait.user_id == user.id).first()
        if not pt:
            await q.answer("Trait not found")
            return
        await q.answer()
        old_trait = session.query(Trait).get(pt.trait_id)
        inv_rows = (session.query(TraitInventory, Trait)
                    .join(Trait, TraitInventory.trait_id == Trait.id)
                    .filter(TraitInventory.user_id == user.id).all())

        btns = []
        for inv, t in inv_rows:
            btns.append([InlineKeyboardButton(
                f"{t.emoji} {t.name} Lv.{inv.level}",
                callback_data=f"trrep_inv_{pt.id}_{inv.id}"
            )])
        btns.append([InlineKeyboardButton("❌ Cancel", callback_data="trcancel")])
        await q.edit_message_text(
            f"🔄 Replacing <b>{old_trait.emoji} {old_trait.name} Lv.{pt.level}</b>\n\n"
            f"Pick the replacement from inventory  ({TRAIT_REPLACE_COST} 💎):",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    except Exception:
        logger.exception("trrep_pt_callback error")
        await q.answer("⚠️ Error", show_alert=True)
    finally:
        session.close()


async def trrep_inv_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Finalize replace. Callback: trrep_inv_<player_trait_id>_<inv_id>"""
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        pt_id = int(parts[2]); inv_id = int(parts[3])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await q.answer("Not authorized")
            return
        ok, msg = replace_trait_on_player(session, user, pt_id, inv_id)
        if ok:
            session.commit()
            await q.answer("Replaced!")
            try: await q.edit_message_text(msg, parse_mode="HTML")
            except Exception: pass
        else:
            session.rollback()
            await q.answer(msg, show_alert=True)
    except Exception:
        session.rollback()
        logger.exception("trrep_inv_callback error")
        await q.answer("⚠️ Error", show_alert=True)
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════════════════
# Shared cancel callback
# ═══════════════════════════════════════════════════════════════════════

async def trait_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Cancelled")
    try: await q.edit_message_text("❌ Cancelled.")
    except Exception: pass

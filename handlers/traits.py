"""Trait command handlers.

Commands:
  /traits             — show all your players and their traits + inventory
  /traitshop          — daily 5-slot gem shop
  /traitbuy <slot>    — buy trait from shop slot
  /traitreroll        — reroll the shop (30 gems)
  /traitapply         — apply an inventory trait to a player (callback picker)
  /traitupgrade       — upgrade a trait on a player (callback picker)
  /traitreplace       — replace a trait on a player (callback picker)
  /removetrait        — unequip a trait, returning it to inventory (picker)
  /selltrait          — sell an inventory trait for gems (just below cost)

Anything that changes what a PLAYER is carrying is frozen while that user has a
match on (see ``services.roster_lock``) — you can't re-kit the XI after the
toss. Buying, rerolling and upgrading traits that are sitting in the inventory
stay open: none of those touch a player who is out on the field.
"""

import html
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, Player, UserRoster, Trait, PlayerTrait, TraitInventory, TraitDaily
from utils.idempotency import claim_once, release
from utils.message_chunks import chunk_blocks
from services.trait_service import (
    TRAIT_DEFINITIONS, refresh_shop, reroll_shop, buy_trait_from_shop,
    apply_trait_to_player, replace_trait_on_player, remove_trait_from_player,
    upgrade_player_trait, upgrade_inventory_trait,
    get_player_traits, trait_rarity_of, sorted_categories,
    _today_key, _get_or_create_daily,
)
from services.flags import get_flag
from services.roster_lock import match_lock_alert, match_lock_message
from services.trait_trading_service import list_inventory, sell_inventory_trait
from config import (
    TRAIT_SHOP_DAILY_PURCHASE_LIMIT, TRAIT_REROLL_COST,
    TRAIT_UPGRADE_COSTS, TRAIT_REPLACE_COST,
    TRAIT_MAX_PER_PLAYER, TRAIT_MAX_ELITE_PER_PLAYER,
    TRAIT_BUY_VALUE, trait_buy_value, trait_sell_value,
    trait_rarity_meta, trait_rarity_label, TRAIT_RARITY_ORDER,
)

logger = logging.getLogger(__name__)


def _rarity_tag(trait) -> str:
    """The rarity pip shown next to a trait everywhere it is listed."""
    return trait_rarity_meta(trait_rarity_of(trait))["emoji"]


def _esc(value) -> str:
    """Escape a name or description before it goes into an HTML message.

    Trait names and descriptions are free text an admin types on the website
    (``admin_trait_edit`` only strips whitespace), and player names come from
    seeded data. A single ``<`` or bare ``&`` makes Telegram reject the WHOLE
    message, so one bad description would break /traitlist or /traits for every
    user rather than just garbling its own line. Same convention as
    ``services.trait_service.remove_trait_from_player``.

    Button labels are deliberately left raw — Telegram does not parse HTML in
    them, so escaping there would show a literal "&amp;".
    """
    return html.escape(str(value or ""))


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
                # Resale price rides along so the sell decision needs no maths.
                lines.append(
                    f"  <code>#{inv.id}</code> {_rarity_tag(t)}{t.emoji} {_esc(t.name)} "
                    f"Lv.{inv.level}{max_badge} · 💰 "
                    f"{trait_sell_value(inv.level, trait_rarity_of(t)):,}💎")
            lines.append("<i>Use /traitapply to attach to a player, "
                         "/selltrait to cash one in</i>")
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
                lines.append(f"\n<b>#{pos}. {_esc(pname)}</b> {flag}")
                for pt, t in traits:
                    max_badge = " 🌟" if pt.level == 5 else ""
                    lines.append(f"  <code>#{pt.id}</code> {_rarity_tag(t)}{t.emoji} "
                                 f"{_esc(t.name)} Lv.{pt.level}{max_badge}")
            lines.append("")
        else:
            lines.append("🏏 <i>No equipped traits yet</i>\n")

        # Shortcuts
        lines.append("━━━━━━━━━━━━━━━━━━━")
        lines.append("🏪 /traitshop — buy traits")
        lines.append("🎯 /traitapply — attach to player")
        lines.append("⬆️ /traitupgrade — level up")
        lines.append("🔄 /traitreplace — swap trait")
        lines.append("📦 /removetrait — unequip, back to inventory")
        lines.append("💰 /selltrait — sell an inventory trait for gems")
        lines.append("💠 /tradetrait @user — swap a trait, same level both ways")
        lines.append("📖 /traitlist — the full catalogue and what each one does")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    except Exception:
        logger.exception("traits_handler error")
        await update.message.reply_text("⚠️ Error loading traits.")
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════════════════
# /traitlist — the whole catalogue, so nobody has to guess
# ═══════════════════════════════════════════════════════════════════════

async def traitlist_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/traitlist [category] — every trait in the game, what it does, what it costs.

    The shop only ever shows a handful of slots, so without this the catalogue
    is invisible: a captain can't plan for a trait they've never seen. Takes an
    optional category (``/traitlist bowling``) to narrow a long list.
    """
    session = get_session()
    try:
        wanted = " ".join(context.args or []).strip().lower()

        rows = (session.query(Trait)
                .filter(Trait.is_active == True)  # noqa: E712
                .order_by(Trait.name).all())
        if not rows:
            await update.message.reply_text(
                "📖 No traits are active right now — check back later.")
            return

        by_category = {}
        for t in rows:
            by_category.setdefault(t.category or "Other", []).append(t)
        categories = sorted_categories(by_category)

        if wanted:
            matched = [c for c in categories if c.lower().startswith(wanted)]
            if not matched:
                await update.message.reply_text(
                    "❓ No such category. Try one of: "
                    + ", ".join(c.lower() for c in categories))
                return
            categories = matched

        header = ("📖 <b>TRAIT CATALOGUE</b>\n"
                  f"<i>{len(rows)} active traits · rarity sets the price and how "
                  f"often the market lists it</i>\n"
                  "━━━━━━━━━━━━━━━━━━━")
        footer = ("━━━━━━━━━━━━━━━━━━━\n"
                  f"⭐ Elite: max <b>{TRAIT_MAX_ELITE_PER_PLAYER} per player</b>. "
                  f"Any card can carry {TRAIT_MAX_PER_PLAYER} traits in total.\n"
                  "🏪 /traitshop to buy · 📖 /traitlist bowling to filter")

        blocks = []
        for category in categories:
            entries = sorted(
                by_category[category],
                key=lambda t: (TRAIT_RARITY_ORDER.index(trait_rarity_of(t)), t.name))
            block = [f"<b>{category.upper()}</b>"]
            for t in entries:
                rarity = trait_rarity_of(t)
                block.append(
                    f"{_rarity_tag(t)} {t.emoji} <b>{_esc(t.name)}</b> — "
                    f"{trait_buy_value(1, rarity):,} 💎\n"
                    f"   <i>{_esc(t.description)}</i>")
            blocks.append("<blockquote expandable>" + "\n".join(block)
                          + "</blockquote>")

        # The whole catalogue is well past Telegram's 4,096-character ceiling,
        # and an over-long send fails outright rather than truncating — so pack
        # whole category blocks into messages and split when the next one won't
        # fit. Splitting BETWEEN blocks keeps every <blockquote> closed inside
        # the message that opens it; a blind character-count split would tear
        # the HTML in half and the send would be rejected for that instead.
        for chunk in chunk_blocks(blocks, header, footer):
            await update.message.reply_text(chunk, parse_mode="HTML")
    except Exception:
        logger.exception("traitlist_handler error")
        await update.message.reply_text("⚠️ Error loading the trait catalogue.")
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════════════════
# /traitshop — daily shop
# ═══════════════════════════════════════════════════════════════════════

def _trait_slot_lines(session, rows, btns, owner_tg):
    """Render the shop's slot listings and fill ``btns`` with their Buy buttons.

    Shared by the first render and the post-buy refresh so the two can't drift —
    they had two copies of this loop and the rarity/stock lines would have had
    to be written twice.
    """
    from services.global_market import is_sold_out, stock_label, is_unlimited

    slot_lines = []
    for row in rows:
        trait = session.query(Trait).get(row.trait_id)
        if not trait:
            continue
        sold_out = is_sold_out(row)
        status = " ❌ <i>sold out</i>" if sold_out else ""
        discount_tag = f"  🏷️ -{row.discount_pct}%" if row.discount_pct > 0 else ""
        # Unlimited is the norm now, so only a limited run is worth the pixels.
        stock = "" if is_unlimited(row) else f"  📦 {stock_label(row)}"
        rarity = trait_rarity_label(trait_rarity_of(trait))
        slot_lines.append(
            f"<b>Slot {row.slot_index + 1}.</b> {trait.emoji} <b>{_esc(trait.name)}</b> "
            f"Lv.1 — <b>{row.final_price}</b> 💎{discount_tag}{stock}{status}\n"
            f"   {rarity} · {trait.category}\n"
            f"   <i>{_esc(trait.description)}</i>"
        )
        if not sold_out:
            btns.append([InlineKeyboardButton(
                f"Buy: {trait.emoji} {trait.name} ({row.final_price} 💎)",
                callback_data=f"trbuy_{owner_tg}_{row.slot_index}"
            )])
    return slot_lines


async def traitshop_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        # Use shared global trait market (admin-managed, auto-refreshes daily)
        from services.global_market import (
            list_trait_market, ensure_trait_market_fresh,
        )
        ensure_trait_market_fresh(session)
        rows = list_trait_market(session)
        if not rows:
            await update.message.reply_text("❌ Trait market is empty. Check back later!")
            return

        daily = _get_or_create_daily(session, user.id)
        remaining = max(0, TRAIT_SHOP_DAILY_PURCHASE_LIMIT - daily.purchases)

        lines = [
            "🏪 <b>TRAIT MARKET</b>",
            f"💎 Gems: <b>{user.total_gems:,}</b>  |  Purchases left today: <b>{remaining}/{TRAIT_SHOP_DAILY_PURCHASE_LIMIT}</b>",
            f"📊 Same shop for all players — admin-managed.",
            "♾️ <i>Unlimited stock — nothing here sells out.</i>",
            "━━━━━━━━━━━━━━━━━━━",
        ]

        btns = []
        slot_lines = _trait_slot_lines(session, rows, btns, tg.id)

        # Slot 1–5 listings live inside an expandable quote to keep chat tidy.
        if slot_lines:
            lines.append("<blockquote expandable>" + "\n\n".join(slot_lines) + "</blockquote>")

        if btns:
            btns.append([
                InlineKeyboardButton("❌ Close",
                                     callback_data=f"trshopcancel_{tg.id}"),
            ])

        lines.append("\n━━━━━━━━━━━━━━━━━━━")
        lines.append("⏱ Buttons expire in 60 seconds if unused.")

        kb = InlineKeyboardMarkup(btns) if btns else None
        sent = await update.message.reply_text(
            "\n".join(lines), parse_mode="HTML", reply_markup=kb)

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

    # Dedup rapid taps on this slot's Buy button (per-slot so the daily-limit
    # second purchase on the refreshed shop message isn't wrongly blocked).
    key = f"trbuy_{q.message.chat_id}_{q.message.message_id}_{slot}"
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

        # The daily cap is enforced HERE, before the buy, not just counted
        # after it. The shop header has always said "Purchases left today:
        # N/2" while this path only ever incremented the counter — the shared
        # ten-copy stock was the accidental cap, and unlimited stock removes
        # it entirely, so a captain with gems could buy the same slot all day.
        # ``buy_trait_from_shop`` has always enforced it; the two shop paths
        # now agree.
        daily = _get_or_create_daily(session, user.id)
        if (daily.purchases or 0) >= TRAIT_SHOP_DAILY_PURCHASE_LIMIT:
            session.rollback()
            release(key)
            await q.answer(
                f"Daily limit reached ({TRAIT_SHOP_DAILY_PURCHASE_LIMIT} "
                f"purchases/day). Come back tomorrow!", show_alert=True)
            return

        from services.global_market import buy_trait
        ok, name_or_msg = buy_trait(session, user, slot)
        if ok:
            daily.purchases = (daily.purchases or 0) + 1
            # 'trait_buy' has been offered on the quest form all along but was
            # never fired anywhere, so a "buy a trait" quest sat at 0 forever.
            try:
                from services.quest_service import safe_track
                safe_track(session, user.id, "trait_buy", 1)
            except Exception:
                logger.exception("trait_buy quest tracking failed")
            session.commit()
            await q.answer(f"✅ {name_or_msg} added to inventory!", show_alert=False)
            await _refresh_shop_display(q, session, user)
        else:
            session.rollback()
            release(key)
            await q.answer(name_or_msg, show_alert=True)
    except Exception:
        # Keep the claim (may be post-commit) so a stale tap can't re-buy the trait.
        session.rollback()
        logger.exception("traitbuy_callback error")
        await q.answer("⚠️ Error", show_alert=True)
    finally:
        session.close()


async def traitreroll_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reroll button removed — shop is now global. Show a message if hit via stale UI."""
    q = update.callback_query
    await q.answer("Trait market is now shared by all players. Only admin can reroll.",
                   show_alert=True)


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
    """Re-render the shop message after a buy."""
    from services.global_market import list_trait_market
    rows = list_trait_market(session)
    daily = _get_or_create_daily(session, user.id)
    remaining = max(0, TRAIT_SHOP_DAILY_PURCHASE_LIMIT - daily.purchases)
    owner_tg = user.telegram_id

    lines = [
        "🏪 <b>TRAIT MARKET</b>",
        f"💎 Gems: <b>{user.total_gems:,}</b>  |  Purchases left today: <b>{remaining}/{TRAIT_SHOP_DAILY_PURCHASE_LIMIT}</b>",
        "♾️ <i>Unlimited stock — nothing here sells out.</i>",
        "━━━━━━━━━━━━━━━━━━━",
    ]
    btns = []
    slot_lines = _trait_slot_lines(session, rows, btns, owner_tg)
    # Slot 1–5 listings live inside an expandable quote to keep chat tidy.
    if slot_lines:
        lines.append("<blockquote expandable>" + "\n\n".join(slot_lines) + "</blockquote>")
    if btns:
        btns.append([
            InlineKeyboardButton("❌ Close",
                                 callback_data=f"trshopcancel_{owner_tg}"),
        ])
    lines.append("\n━━━━━━━━━━━━━━━━━━━")
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
        locked = match_lock_message(session, user.id, "apply a trait to a player")
        if locked:
            await update.message.reply_text(locked, parse_mode="HTML")
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
            f"🎯 Applying <b>{trait.emoji} {_esc(trait.name)} Lv.{inv.level}</b>\n\n"
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
        # The picker may have been opened before the match started — check again
        # at the moment it would actually change the player.
        locked = match_lock_alert(session, user.id, "apply a trait")
        if locked:
            await q.answer(locked, show_alert=True)
            return
        ok, msg = apply_trait_to_player(session, user, inv_id, roster_id)
        if ok:
            session.commit()
            await q.answer("Applied!")
            try: await q.edit_message_text(f"✅ {msg}", parse_mode="HTML")
            except Exception: pass
            # Quest tracking
            try:
                from services.quest_service import safe_track
                safe_track(session, user.id, "trait_apply", 1)
                session.commit()
            except Exception:
                pass
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

        # Upgrading a trait a player is WEARING changes that player, so it is
        # frozen mid-match — but inventory upgrades touch nobody on the field
        # and stay available. Hide the equipped half rather than refusing the
        # whole command.
        locked_msg = match_lock_message(session, user.id,
                                        "upgrade an equipped trait")
        in_match = bool(locked_msg)

        # Get all equipped traits that are not at max level
        equipped = [] if in_match else (
            session.query(PlayerTrait, Trait, UserRoster, Player)
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
            if in_match:
                await update.message.reply_text(locked_msg, parse_mode="HTML")
                return
            await update.message.reply_text(
                "❌ No upgradeable traits. "
                "Either buy some via /traitshop or all your traits are at Max Level.")
            return

        btns = []
        lines = [f"⬆️ <b>UPGRADE TRAIT</b>  |  💎 {user.total_gems:,}", "━━━━━━━━━━━━━━━━━━━"]
        if in_match:
            lines.append("🔒 <i>Equipped traits are locked while your match is "
                         "on — inventory upgrades only.</i>")

        if equipped:
            lines.append("<b>Equipped:</b>")
            for pt, t, ur, p in equipped:
                cost = TRAIT_UPGRADE_COSTS.get(pt.level, 0)
                lines.append(f"  {t.emoji} {_esc(t.name)} Lv.{pt.level} on "
                             f"{_esc(p.name)}  →  Lv.{pt.level + 1} ({cost} 💎)")
                btns.append([InlineKeyboardButton(
                    f"⬆️ {t.emoji} {t.name} Lv.{pt.level}→{pt.level + 1} on {p.name} ({cost} 💎)",
                    callback_data=f"trup_pt_{pt.id}"
                )])

        if inv_rows:
            lines.append("\n<b>Inventory (not equipped):</b>")
            for inv, t in inv_rows:
                cost = TRAIT_UPGRADE_COSTS.get(inv.level, 0)
                lines.append(f"  {t.emoji} {_esc(t.name)} Lv.{inv.level}  →  "
                             f"Lv.{inv.level + 1} ({cost} 💎)")
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
        locked = match_lock_alert(session, user.id, "upgrade an equipped trait")
        if locked:
            await q.answer(locked, show_alert=True)
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

        locked = match_lock_message(session, user.id, "replace a player's trait")
        if locked:
            await update.message.reply_text(locked, parse_mode="HTML")
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
            "⚠️ <i>The trait you replace is discarded. To keep it, use "
            "/removetrait (free — it goes back to your inventory) and then "
            "/traitapply the new one.</i>",
            "",
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
        locked = match_lock_alert(session, user.id, "replace a player's trait")
        if locked:
            await q.answer(locked, show_alert=True)
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
# /removetrait — unequip a trait, sending it back to inventory
# ═══════════════════════════════════════════════════════════════════════

async def removetrait_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/removetrait — pick an equipped trait to send back to your inventory."""
    tg = update.effective_user
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        locked = match_lock_message(session, user.id, "remove a player's trait")
        if locked:
            await update.message.reply_text(locked, parse_mode="HTML")
            return

        equipped = (session.query(PlayerTrait, Trait, UserRoster, Player)
                    .join(Trait, PlayerTrait.trait_id == Trait.id)
                    .join(UserRoster, PlayerTrait.roster_id == UserRoster.id)
                    .join(Player, UserRoster.player_id == Player.id)
                    .filter(PlayerTrait.user_id == user.id)
                    .order_by(UserRoster.order_position).all())

        if not equipped:
            await update.message.reply_text(
                "❌ You have no equipped traits to remove.\n"
                "Apply one first with /traitapply.")
            return

        lines = [
            "📦 <b>REMOVE TRAIT</b>",
            "━━━━━━━━━━━━━━━━━━━",
            "Removing is free — the trait keeps its level and goes straight "
            "back to your inventory.",
            "",
            "Pick the trait to remove:",
        ]
        btns = []
        for pt, t, ur, p in equipped:
            max_badge = " 🌟" if pt.level == 5 else ""
            btns.append([InlineKeyboardButton(
                f"{t.emoji} {t.name} Lv.{pt.level}{max_badge} — {p.name}",
                callback_data=f"trrem_pt_{pt.id}"
            )])
        btns.append([InlineKeyboardButton("❌ Cancel", callback_data="trcancel")])

        await update.message.reply_text(
            "\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(btns))
    except Exception:
        logger.exception("removetrait_handler error")
        await update.message.reply_text("⚠️ Error.")
    finally:
        session.close()


async def trrem_pt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unequip the chosen trait. Callback: trrem_pt_<player_trait_id>"""
    q = update.callback_query
    tg = q.from_user
    try:
        pt_id = int(q.data.split("_")[2])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return

    # Dedup rapid taps — a double tap must not mint two inventory copies.
    key = f"trrem_{pt_id}"
    if not claim_once(key):
        await q.answer("Already processing…")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            release(key)
            await q.answer("Not authorized")
            return
        locked = match_lock_alert(session, user.id, "remove a player's trait")
        if locked:
            release(key)
            await q.answer(locked, show_alert=True)
            return
        ok, msg = remove_trait_from_player(session, user, pt_id)
        if ok:
            session.commit()
            await q.answer("Removed!")
            try: await q.edit_message_text(msg, parse_mode="HTML")
            except Exception: pass
        else:
            session.rollback()
            release(key)
            await q.answer(msg, show_alert=True)
    except Exception:
        # Keep the claim (the commit may already have landed) so a stale tap
        # can't return the same trait to inventory twice.
        session.rollback()
        logger.exception("trrem_pt_callback error")
        await q.answer("⚠️ Error", show_alert=True)
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════════════════
# /selltrait — cash an inventory trait in for gems
# ═══════════════════════════════════════════════════════════════════════

def _sell_quote_lines(user, rarities=()):
    """The price list shown above the picker, so nobody sells on a guess.

    Priced per rarity, because a Lv.3 Elite is worth four times a Lv.3 Common
    and a single table would misquote both. Only the rarities the captain
    actually holds are listed, so the message stays short.
    """
    lines = ["💰 <b>SELL TRAIT</b>", "━━━━━━━━━━━━━━━━━━━",
             f"💎 Gems: {user.total_gems:,}", "",
             "Selling always returns <b>a little less</b> than the gems a "
             "trait cost to build — never more, however cheaply you bought "
             "it:"]
    shown = [r for r in TRAIT_RARITY_ORDER if r in set(rarities)] or ["common"]
    for rarity in shown:
        if len(shown) > 1:
            lines.append(f"<b>{trait_rarity_label(rarity)}</b>")
        for level in sorted(TRAIT_BUY_VALUE):
            lines.append(
                f"  Lv.{level}: {trait_buy_value(level, rarity):,} 💎 invested → "
                f"<b>{trait_sell_value(level, rarity):,} 💎</b> back")
    lines += ["",
              "<i>Only unequipped traits can be sold. A trait on a player? "
              "/removetrait first — that's free and keeps its level.</i>", "",
              "Pick the trait to sell:"]
    return lines


async def selltrait_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/selltrait — sell an inventory trait for gems (just below what it cost)."""
    tg = update.effective_user
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        rows = list_inventory(session, user.id)
        if not rows:
            await update.message.reply_text(
                "📦 <b>Nothing to sell.</b>\n\n"
                "Your trait inventory is empty. Traits equipped on a player "
                "aren't sellable — use /removetrait to bring one back first "
                "(free), or buy more from /traitshop.",
                parse_mode="HTML")
            return

        btns = []
        for inv, trait in rows:
            level = int(inv.level or 1)
            btns.append([InlineKeyboardButton(
                f"{trait.emoji} {trait.name} Lv.{level} → "
                f"{trait_sell_value(level, trait_rarity_of(trait)):,} 💎",
                callback_data=f"trsell_{inv.id}")])
        btns.append([InlineKeyboardButton("❌ Cancel", callback_data="trcancel")])

        rarities = [trait_rarity_of(trait) for _inv, trait in rows]
        await update.message.reply_text(
            "\n".join(_sell_quote_lines(user, rarities)), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(btns))
    except Exception:
        logger.exception("selltrait_handler error")
        await update.message.reply_text("⚠️ Error.")
    finally:
        session.close()


async def trsell_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirm screen for one trait. Callback: trsell_<inventory_id>"""
    q = update.callback_query
    tg = q.from_user
    try:
        inv_id = int(q.data.split("_")[1])
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
            TraitInventory.id == inv_id,
            TraitInventory.user_id == user.id).first()
        if not inv:
            await q.answer("Trait not found")
            try: await q.edit_message_text("❌ That trait is no longer in your inventory.")
            except Exception: pass
            return
        trait = session.query(Trait).get(inv.trait_id)
        level = int(inv.level or 1)
        await q.answer()

        rarity = trait_rarity_of(trait)
        gems = trait_sell_value(level, rarity)
        buy = trait_buy_value(level, rarity)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Sell for {gems:,} 💎",
                                 callback_data=f"trsellok_{inv.id}"),
            InlineKeyboardButton("❌ Cancel", callback_data="trcancel"),
        ]])
        await q.edit_message_text(
            f"💰 <b>SELL THIS TRAIT?</b>\n\n"
            f"{trait.emoji} <b>{trait.name}</b> Lv.{level}\n"
            f"🏷 {trait.category} · {trait_rarity_label(rarity)}\n\n"
            f"Invested: {buy:,} 💎\n"
            f"You receive: <b>{gems:,} 💎</b> "
            f"(you lose {buy - gems:,} 💎)\n\n"
            f"<i>This cannot be undone — the trait is gone for good.</i>",
            parse_mode="HTML", reply_markup=kb)
    except Exception:
        logger.exception("trsell_callback error")
        await q.answer("⚠️ Error", show_alert=True)
    finally:
        session.close()


async def trsellok_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Do the sale. Callback: trsellok_<inventory_id>"""
    q = update.callback_query
    tg = q.from_user
    try:
        inv_id = int(q.data.split("_")[1])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return

    # Dedup rapid taps — a double tap must not pay gems twice.
    key = f"trsell_{inv_id}"
    if not claim_once(key):
        await q.answer("Already processing…")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            release(key)
            await q.answer("Not authorized")
            return
        ok, msg, detail = sell_inventory_trait(session, user, inv_id)
        if not ok:
            session.rollback()
            release(key)
            await q.answer(msg, show_alert=True)
            return
        session.commit()
        await q.answer(f"+{detail['gems']:,} 💎")
        try:
            await q.edit_message_text(
                f"✅ <b>TRAIT SOLD</b>\n\n"
                f"{detail['label']}\n\n"
                f"💎 Received: <b>{detail['gems']:,}</b>\n"
                f"💰 Balance: {detail['balance']:,} 💎",
                parse_mode="HTML")
        except Exception:
            pass
    except Exception:
        # Keep the claim (the commit may already have landed) so a stale tap
        # can't pay for the same trait twice.
        session.rollback()
        logger.exception("trsellok_callback error")
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

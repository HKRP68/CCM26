"""Release player handlers — /release (supports name, position, ranges) + /releasemultiple."""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, Player, UserRoster
from config import get_sell_value
from utils.idempotency import claim_once, release
from services.activity_service import log_activity
from services.flags import get_flag

logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────

def _renumber_roster(session, user_id):
    """Close any gaps in order_position after a release."""
    from sqlalchemy import asc
    remaining = (session.query(UserRoster)
                 .filter(UserRoster.user_id == user_id)
                 .order_by(
                     asc(UserRoster.order_position).nullslast(),
                     UserRoster.acquired_date,
                     UserRoster.id,
                 ).all())
    for i, entry in enumerate(remaining, 1):
        if entry.order_position != i:
            entry.order_position = i


def _do_release(session, user, entries):
    """Release a list of (UserRoster, Player) tuples atomically.

    Cleans up all known references to the doomed roster rows BEFORE deleting,
    so foreign key constraints don't bite us:
      - PlayerTrait rows (returns each trait to inventory)
      - pending Trade rows (cancelled, FKs nulled)
      - User.captain_roster_id (nulled if it points to a doomed row)
      - any other lingering references caught by the catch-all SQL below

    Returns dict with success, released list, total_coins, new_balance, new_count.
    """
    from sqlalchemy import text
    from models import Trade, PlayerTrait, TraitInventory

    total_coins = 0
    released = []
    captain_released = False
    traits_returned = 0

    roster_ids = [e.id for e, _ in entries]
    if not roster_ids:
        return {
            "success": True, "released": [],
            "total_coins": 0, "new_balance": user.total_coins,
            "new_count": user.roster_count,
            "captain_released": False, "traits_returned": 0,
        }

    # 1. Cancel pending trades that reference these roster entries
    stale_trades = (session.query(Trade)
                    .filter(Trade.status == "pending")
                    .filter((Trade.initiator_roster_id.in_(roster_ids)) |
                            (Trade.receiver_roster_id.in_(roster_ids)))
                    .all())
    for t in stale_trades:
        t.status = "cancelled"
        if t.initiator_roster_id in roster_ids:
            t.initiator_roster_id = None
        if t.receiver_roster_id in roster_ids:
            t.receiver_roster_id = None

    # ALSO null FK on completed/cancelled trades — even those still hold a FK
    # pointer in Postgres, and "ON DELETE NO ACTION" (default) blocks the delete.
    historical_trades = (session.query(Trade)
                         .filter(Trade.status != "pending")
                         .filter((Trade.initiator_roster_id.in_(roster_ids)) |
                                 (Trade.receiver_roster_id.in_(roster_ids)))
                         .all())
    for t in historical_trades:
        if t.initiator_roster_id in roster_ids:
            t.initiator_roster_id = None
        if t.receiver_roster_id in roster_ids:
            t.receiver_roster_id = None
    session.flush()

    # 2. Return any equipped traits to inventory
    equipped = (session.query(PlayerTrait)
                .filter(PlayerTrait.roster_id.in_(roster_ids)).all())
    for pt in equipped:
        inv = TraitInventory(
            user_id=pt.user_id,
            trait_id=pt.trait_id,
            level=pt.level,
        )
        session.add(inv)
        session.delete(pt)
        traits_returned += 1
    session.flush()

    # 3. Captain check: null user.captain_roster_id BEFORE deleting roster rows.
    # We check ALL the entries being deleted up front so the captain reference
    # is gone before any row delete is flushed.
    if user.captain_roster_id in roster_ids:
        user.captain_roster_id = None
        captain_released = True
        session.flush()

    # 4. Catch-all: production DB may have FK constraints not in the model
    # (e.g. legacy users.captain_roster_id FK from an old migration). Use raw
    # SQL UPDATE that's safe-on-no-match — these are idempotent best-effort
    # cleanups. Wrapped in try/except so missing tables don't crash the release.
    safety_updates = [
        ("UPDATE users SET captain_roster_id = NULL WHERE captain_roster_id IN :ids",
         {"ids": tuple(roster_ids) if len(roster_ids) > 1 else (roster_ids[0],)}),
    ]
    for sql, params in safety_updates:
        try:
            session.execute(text(sql), params)
        except Exception:
            pass  # best-effort

    # 5. Delete the roster entries
    undo_items = []  # for /cmuundo
    for entry, player in entries:
        sv = get_sell_value(player.rating)
        undo_items.append({
            "player_id": player.id,
            "player_name": player.name,
            "rating": player.rating,
            "price": sv,
        })
        session.delete(entry)
        user.total_coins += sv
        user.roster_count = max(0, user.roster_count - 1)
        total_coins += sv
        released.append({"name": player.name, "rating": player.rating, "value": sv})
        log_activity(session, user.id, "release",
                     f"Released {player.name} ({player.rating}) for {sv:,}",
                     coins_change=sv, player_name=player.name, player_rating=player.rating)

    session.flush()
    _renumber_roster(session, user.id)

    # Record for /cmuundo
    if undo_items:
        try:
            from services.undo_service import record_release
            record_release(session, user.id,
                           items=undo_items, total_refund=total_coins)
        except Exception:
            import logging as _lg
            _lg.getLogger(__name__).exception("record_release failed (non-fatal)")

    return {
        "success": True,
        "released": released,
        "total_coins": total_coins,
        "new_balance": user.total_coins,
        "new_count": user.roster_count,
        "captain_released": captain_released,
        "traits_returned": traits_returned,
    }


def _find_by_arg(session, user_id, arg_str):
    """Find roster entries matching the argument.
    - If arg is a number, returns entry at that position.
    - If arg is a name, returns all matching entries (for disambiguation).
    Returns list of (UserRoster, Player).
    """
    arg_str = arg_str.strip()

    # Try position first — use DISPLAY order (matches /pxi numbering)
    if arg_str.isdigit():
        pos = int(arg_str)
        from handlers.lineup import _build_display_order
        raw_entries = (session.query(UserRoster, Player)
                       .join(Player, UserRoster.player_id == Player.id)
                       .filter(UserRoster.user_id == user_id)
                       .order_by(UserRoster.order_position).all())
        display_entries = _build_display_order(raw_entries)
        if 1 <= pos <= len(display_entries):
            return [display_entries[pos - 1]]
        return []

    # Name search — exact match first, then substring
    exact = (session.query(UserRoster, Player)
             .join(Player, UserRoster.player_id == Player.id)
             .filter(UserRoster.user_id == user_id, Player.name.ilike(arg_str))
             .order_by(UserRoster.order_position).all())
    if exact:
        return exact

    substr = (session.query(UserRoster, Player)
              .join(Player, UserRoster.player_id == Player.id)
              .filter(UserRoster.user_id == user_id,
                      Player.name.ilike(f"%{arg_str}%"))
              .order_by(UserRoster.order_position).all())
    return substr


def _fmt_player_line(entry, player):
    sv = get_sell_value(player.rating)
    flag = get_flag(player.country) if player.country else ""
    return f"#{entry.order_position}. {player.name} {flag} | {player.rating} OVR | 💸 {sv:,}"


# ── /release — smart single/name/position release ────────────────────

async def releasepl_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /release <name>     → release by name (disambiguates duplicates)
    /release <position> → release by roster position (1-based)
    /release            → show usage
    """
    tg_user = update.effective_user

    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "<code>/release &lt;player name&gt;</code> — by name\n"
            "<code>/release &lt;position&gt;</code> — by roster position\n"
            "<code>/releasemultiple &lt;from&gt; &lt;to&gt;</code> — range",
            parse_mode="HTML")
        return

    arg = " ".join(context.args).strip()

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        matches = _find_by_arg(session, user.id, arg)

        if not matches:
            await update.message.reply_text(f"❌ No match for '<code>{arg}</code>'", parse_mode="HTML")
            return

        # Multiple matches — let user pick
        if len(matches) > 1:
            # Show up to 10 choices
            btns = []
            for entry, player in matches[:10]:
                sv = get_sell_value(player.rating)
                btns.append([InlineKeyboardButton(
                    f"#{entry.order_position} {player.name} ({player.rating}) — 💸 {sv:,}",
                    callback_data=f"rlone_{entry.id}")])
            btns.append([InlineKeyboardButton("❌ Cancel", callback_data="rlcancel")])

            text = f"🔍 Found <b>{len(matches)}</b> matching players:\n\nChoose one to release:"
            sent = await update.message.reply_text(text, parse_mode="HTML",
                                            reply_markup=InlineKeyboardMarkup(btns))
            try:
                from services.button_timeout import schedule_button_timeout
                schedule_button_timeout(context, sent.chat_id, sent.message_id, delay_seconds=120)
            except Exception:
                pass
            return

        # Single match — show confirm
        entry, player = matches[0]
        sv = get_sell_value(player.rating)
        flag = get_flag(player.country) if player.country else ""
        captain_warn = ""
        if user.captain_roster_id == entry.id:
            captain_warn = "\n\n⚠️ <b>This is your Captain!</b> You'll need to set a new one."

        text = (
            "🔴 <b>RELEASE PLAYER?</b>\n\n"
            f"#{entry.order_position}. {player.name} {flag}\n"
            f"⭐ Rating: {player.rating} OVR\n"
            f"🏷 {player.category}\n\n"
            f"💸 You will receive: <b>{sv:,}</b> 🪙"
            f"{captain_warn}"
        )

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Release", callback_data=f"rlone_{entry.id}"),
            InlineKeyboardButton("❌ Cancel", callback_data="rlcancel"),
        ]])
        sent = await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
        try:
            from services.button_timeout import schedule_button_timeout
            schedule_button_timeout(context, sent.chat_id, sent.message_id, delay_seconds=120)
        except Exception:
            pass

    except Exception:
        logger.exception("Release error")
        await update.message.reply_text("⚠️ Error.")
    finally:
        session.close()


async def release_one_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirm single release — callback: rlone_<roster_id>"""
    query = update.callback_query
    tg_user = query.from_user

    try:
        roster_id = int(query.data.split("_")[1])
    except (IndexError, ValueError):
        await query.answer("Invalid")
        return

    # Dedup rapid taps so a player can't be released (and credited) twice.
    key = f"rlone_{query.message.chat_id}_{query.message.message_id}"
    if not claim_once(key):
        await query.answer("Already processing…")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            release(key)
            await query.answer("Not authorized")
            return

        entry = session.query(UserRoster).filter(
            UserRoster.id == roster_id, UserRoster.user_id == user.id).first()
        if not entry:
            release(key)
            await query.answer("Not yours or already released")
            try: await query.edit_message_text("❌ This player is no longer in your roster.")
            except Exception: pass
            return

        await query.answer()
        player = session.query(Player).get(entry.player_id)
        if not player:
            # Roster entry orphaned — just delete it
            session.delete(entry)
            user.roster_count = max(0, user.roster_count - 1)
            session.commit()
            try: await query.edit_message_text("⚠️ Player data missing — roster entry cleaned up.")
            except Exception: pass
            return

        result = _do_release(session, user, [(entry, player)])
        session.commit()

        r = result["released"][0]
        text = (
            f"✅ <b>PLAYER RELEASED</b>\n\n"
            f"{r['name']} ({r['rating']} OVR)\n\n"
            f"💸 Received: <b>{r['value']:,}</b> 🪙\n"
            f"💰 Balance: {result['new_balance']:,}\n"
            f"📊 Roster: {result['new_count']}/25"
        )
        if result["captain_released"]:
            text += "\n\n⚠️ Captain slot cleared. Use /setcaptain to assign new one."
        if result.get("traits_returned"):
            text += f"\n💎 {result['traits_returned']} trait(s) returned to inventory."
        text += "\n\n<i>↩️ Made a mistake? /cmuundo within 60 seconds to reverse.</i>"

        await query.edit_message_text(text, parse_mode="HTML")

    except Exception as e:
        session.rollback()
        release(key)
        logger.exception(f"Release one callback FAILED: {type(e).__name__}: {e}")
        msg = str(e)
        import re
        m = re.search(r'constraint "([^"]+)"', msg)
        constraint_hint = f"\nConstraint: <code>{m.group(1)}</code>" if m else ""
        try:
            await query.edit_message_text(
                f"⚠️ Error releasing player.\n"
                f"<code>{type(e).__name__}</code>{constraint_hint}\n\n"
                f"<i>{msg[:300]}</i>",
                parse_mode="HTML")
        except Exception:
            pass
    finally:
        session.close()


async def release_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Cancelled")
    try:
        await query.edit_message_text("❌ Release cancelled.")
    except Exception:
        pass


# ── /releasemultiple — range release ─────────────────────────────────

async def releasemultiple_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/releasemultiple <from> <to> — release roster positions in range."""
    tg_user = update.effective_user

    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: <code>/releasemultiple &lt;from&gt; &lt;to&gt;</code>\n"
            "Example: <code>/releasemultiple 7 11</code>",
            parse_mode="HTML")
        return

    try:
        pos_from = int(context.args[0])
        pos_to = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Positions must be numbers.")
        return

    if pos_from > pos_to:
        pos_from, pos_to = pos_to, pos_from

    if pos_from < 1:
        await update.message.reply_text("❌ Position must be 1 or higher.")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        from handlers.lineup import _build_display_order
        raw_entries = (session.query(UserRoster, Player)
                       .join(Player, UserRoster.player_id == Player.id)
                       .filter(UserRoster.user_id == user.id)
                       .order_by(UserRoster.order_position).all())
        entries = _build_display_order(raw_entries)

        if pos_to > len(entries):
            await update.message.reply_text(
                f"❌ You only have {len(entries)} players. Max position is {len(entries)}.")
            return

        to_release = entries[pos_from - 1:pos_to]
        if not to_release:
            await update.message.reply_text("❌ Nothing to release in that range.")
            return

        total_sell = 0
        lines = []
        captain_in_range = False
        for entry, player in to_release:
            sv = get_sell_value(player.rating)
            total_sell += sv
            lines.append(_fmt_player_line(entry, player))
            if user.captain_roster_id == entry.id:
                captain_in_range = True

        # Build preview (truncate if too long)
        preview = "\n".join(lines[:15])
        if len(lines) > 15:
            preview += f"\n<i>... and {len(lines) - 15} more</i>"

        captain_warn = "\n\n⚠️ <b>Captain is in this range!</b>" if captain_in_range else ""

        text = (
            f"🔴 <b>RELEASE {len(to_release)} PLAYERS?</b>\n\n"
            f"<b>Positions {pos_from} → {pos_to}</b>\n\n"
            f"{preview}\n\n"
            f"💸 Total: <b>{total_sell:,}</b> 🪙"
            f"{captain_warn}"
        )

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Release All",
                callback_data=f"rlm_{user.telegram_id}_{pos_from}_{pos_to}"),
            InlineKeyboardButton("❌ Cancel", callback_data="rlcancel"),
        ]])
        sent = await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
        try:
            from services.button_timeout import schedule_button_timeout
            schedule_button_timeout(context, sent.chat_id, sent.message_id, delay_seconds=120)
        except Exception:
            pass

    except Exception:
        logger.exception("ReleaseMultiple handler error")
        await update.message.reply_text("⚠️ Error.")
    finally:
        session.close()


async def releasemultiple_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: rlm_<tg_user_id>_<from>_<to>"""
    query = update.callback_query
    tg_user = query.from_user

    parts = query.data.split("_")
    if len(parts) < 4:
        await query.answer("Expired")
        try: await query.edit_message_text("❌ Expired. Run /releasemultiple again.")
        except Exception: pass
        return

    try:
        authorized_tg_id = int(parts[1])
        pos_from = int(parts[2])
        pos_to = int(parts[3])
    except ValueError:
        await query.answer("Bad data")
        return

    # Authorization — only the user who issued the command can confirm
    if tg_user.id != authorized_tg_id:
        await query.answer("Not your release!", show_alert=True)
        return

    # Dedup rapid taps so a batch can't be released (and credited) twice.
    key = f"rlm_{query.message.chat_id}_{query.message.message_id}"
    if not claim_once(key):
        await query.answer("Already processing…")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            release(key)
            await query.answer("Not authorized")
            return

        await query.answer()

        from handlers.lineup import _build_display_order
        raw_entries = (session.query(UserRoster, Player)
                       .join(Player, UserRoster.player_id == Player.id)
                       .filter(UserRoster.user_id == user.id)
                       .order_by(UserRoster.order_position).all())
        entries = _build_display_order(raw_entries)

        if pos_from < 1 or pos_to > len(entries) or pos_from > pos_to:
            release(key)
            try: await query.edit_message_text(
                f"❌ Roster changed. You now have {len(entries)} players.\n"
                f"Please run /releasemultiple again.")
            except Exception: pass
            return

        to_release = entries[pos_from - 1:pos_to]
        if not to_release:
            release(key)
            try: await query.edit_message_text("❌ Nothing to release.")
            except Exception: pass
            return

        result = _do_release(session, user, to_release)
        session.commit()

        released = result["released"]
        names_str = ", ".join(r["name"] for r in released[:8])
        if len(released) > 8:
            names_str += f", +{len(released) - 8} more"

        text = (
            f"✅ <b>RELEASED {len(released)} PLAYERS</b>\n\n"
            f"{names_str}\n\n"
            f"💸 Total: <b>{result['total_coins']:,}</b> 🪙\n"
            f"💰 Balance: {result['new_balance']:,}\n"
            f"📊 Roster: {result['new_count']}/25"
        )
        if result["captain_released"]:
            text += "\n\n⚠️ Captain slot cleared. Use /setcaptain."
        if result.get("traits_returned"):
            text += f"\n💎 {result['traits_returned']} trait(s) returned to inventory."
        text += "\n\n<i>↩️ Made a mistake? /cmuundo within 60 seconds to reverse.</i>"

        await query.edit_message_text(text, parse_mode="HTML")

    except Exception as e:
        session.rollback()
        release(key)
        logger.exception(f"ReleaseMultiple confirm FAILED: {type(e).__name__}: {e}")
        # Extract the actual constraint name from psycopg2 errors so we can
        # diagnose which lingering reference is blocking the delete.
        msg = str(e)
        # Postgres FK violations look like:
        #   ... violates foreign key constraint "fk_name" on table "x"
        constraint_hint = ""
        import re
        m = re.search(r'constraint "([^"]+)"', msg)
        if m:
            constraint_hint = f"\nConstraint: <code>{m.group(1)}</code>"
        # Snippet of underlying error (longer than before)
        try: await query.edit_message_text(
            f"⚠️ Error releasing players.\n"
            f"<code>{type(e).__name__}</code>{constraint_hint}\n\n"
            f"<i>{msg[:300]}</i>",
            parse_mode="HTML")
        except Exception: pass
    finally:
        session.close()

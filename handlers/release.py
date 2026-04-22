"""Release player handlers — /release (supports name, position, ranges) + /releasemultiple."""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, Player, UserRoster
from config import get_sell_value
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
    Returns dict with success, released list, total_coins, new_balance, new_count.
    """
    from models import Trade

    total_coins = 0
    released = []
    captain_released = False

    # First: clean up any pending trades that reference these roster entries
    roster_ids = [e.id for e, _ in entries]
    if roster_ids:
        stale_trades = (session.query(Trade)
                        .filter(Trade.status == "pending")
                        .filter((Trade.initiator_roster_id.in_(roster_ids)) |
                                (Trade.receiver_roster_id.in_(roster_ids)))
                        .all())
        for t in stale_trades:
            t.status = "cancelled"
            # Null out the FK so deletion doesn't cascade or block
            if t.initiator_roster_id in roster_ids:
                t.initiator_roster_id = None
            if t.receiver_roster_id in roster_ids:
                t.receiver_roster_id = None
        session.flush()

    for entry, player in entries:
        sv = get_sell_value(player.rating)
        # Captain check — clear captain if captain is released
        if user.captain_roster_id == entry.id:
            user.captain_roster_id = None
            captain_released = True
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

    return {
        "success": True,
        "released": released,
        "total_coins": total_coins,
        "new_balance": user.total_coins,
        "new_count": user.roster_count,
        "captain_released": captain_released,
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
            await update.message.reply_text(text, parse_mode="HTML",
                                            reply_markup=InlineKeyboardMarkup(btns))
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
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

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

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await query.answer("Not authorized")
            return

        entry = session.query(UserRoster).filter(
            UserRoster.id == roster_id, UserRoster.user_id == user.id).first()
        if not entry:
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

        await query.edit_message_text(text, parse_mode="HTML")

    except Exception as e:
        session.rollback()
        logger.exception(f"Release one callback FAILED: {type(e).__name__}: {e}")
        try:
            await query.edit_message_text(
                f"⚠️ Error releasing player.\n<code>{type(e).__name__}: {str(e)[:80]}</code>",
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
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

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

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
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
            try: await query.edit_message_text(
                f"❌ Roster changed. You now have {len(entries)} players.\n"
                f"Please run /releasemultiple again.")
            except Exception: pass
            return

        to_release = entries[pos_from - 1:pos_to]
        if not to_release:
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

        await query.edit_message_text(text, parse_mode="HTML")

    except Exception as e:
        session.rollback()
        logger.exception(f"ReleaseMultiple confirm FAILED: {type(e).__name__}: {e}")
        try: await query.edit_message_text(
            f"⚠️ Error releasing players.\n<code>{type(e).__name__}: {str(e)[:80]}</code>",
            parse_mode="HTML")
        except Exception: pass
    finally:
        session.close()

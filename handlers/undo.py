"""/cmuundo command — undo the last /buy or /release within 60 seconds.

Strict reversal:
  - Buy → remove player from roster, refund buy price
  - Release → restore player(s) to roster, deduct release refund

Refuses if:
  - No pending undo
  - Window expired (>60s)
  - Reversal blocked: player traded away (buy undo), roster full (release undo)
"""

import logging
from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import User, UserRoster, Player, PendingUndo
from services.undo_service import (
    get_pending, clear_pending, seconds_remaining, UNDO_WINDOW_SECONDS,
)

logger = logging.getLogger(__name__)

from config import MAX_ROSTER


async def cmuundo_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    session = get_session()
    try:
        user = (session.query(User)
                .filter(User.telegram_id == tg_user.id).first())
        if not user:
            from services.message_service import get_msg
            await update.message.reply_text(get_msg("not_debuted"))
            return

        row, payload = get_pending(session, user.id)
        if not row:
            await update.message.reply_text(
                f"⚠️ Nothing to undo.\n"
                f"<i>/cmuundo works for {UNDO_WINDOW_SECONDS}s after your last "
                f"/buy or /release.</i>",
                parse_mode="HTML")
            session.commit()  # commit the cleanup of any expired row
            return

        action = row.action_type

        if action == "buy":
            await _undo_buy(update, session, user, payload)
        elif action == "release":
            await _undo_release(update, session, user, payload)
        else:
            await update.message.reply_text(
                f"⚠️ Unknown action: {action}. Nothing undone.")
            clear_pending(session, user.id)
            session.commit()
    except Exception:
        session.rollback()
        logger.exception("cmuundo failed")
        try:
            await update.message.reply_text(
                "⚠️ Couldn't undo. Try again."
            )
        except Exception:
            pass
    finally:
        session.close()


async def _undo_buy(update, session, user, payload):
    """Undo a buy: remove player from roster, refund price."""
    roster_id = payload.get("roster_id")
    player_id = payload.get("player_id")
    price = payload.get("price", 0)
    player_name = payload.get("player_name", "—")
    rating = payload.get("rating", 0)
    gem_bonus = int(payload.get("gem_bonus") or 0)

    # Verify the roster entry still exists AND still belongs to this user
    entry = (session.query(UserRoster)
             .filter(UserRoster.id == roster_id,
                     UserRoster.user_id == user.id,
                     UserRoster.player_id == player_id)
             .first())
    if not entry:
        # Player was traded, released, or otherwise removed already
        await update.message.reply_text(
            f"⚠️ <b>Can't undo buy</b>\n\n"
            f"<b>{player_name}</b> is no longer in your roster "
            f"(traded or released?).\n\n"
            f"<i>The undo window has been cleared.</i>",
            parse_mode="HTML")
        clear_pending(session, user.id)
        session.commit()
        return

    # An elite signing paid a gem rebate — undoing the buy has to hand those
    # gems back, so refuse while they're already spent rather than letting a
    # buy-and-undo loop mint them. Same rule the release undo uses for coins.
    if gem_bonus and (user.total_gems or 0) < gem_bonus:
        await update.message.reply_text(
            f"⚠️ <b>Can't undo buy</b>\n\n"
            f"<b>{player_name}</b> paid a <b>{gem_bonus:,}</b> 💎 signing bonus "
            f"and you only have <b>{user.total_gems or 0:,}</b> 💎 left to "
            f"return.\n\n"
            f"<i>Top up the gems before the undo window closes.</i>",
            parse_mode="HTML")
        # Don't clear — they can retry once the gems are back.
        session.commit()
        return

    # Clean up any references to this roster row before deleting it
    from sqlalchemy import text as _sa_text
    from models import Trade
    try:
        stale_trades = (session.query(Trade)
                        .filter(Trade.status == "pending")
                        .filter((Trade.initiator_roster_id == roster_id) |
                                (Trade.receiver_roster_id == roster_id))
                        .all())
        for t in stale_trades:
            t.status = "cancelled"
            if t.initiator_roster_id == roster_id:
                t.initiator_roster_id = None
            if t.receiver_roster_id == roster_id:
                t.receiver_roster_id = None

        # Null FKs on historical trades too
        hist_trades = (session.query(Trade)
                       .filter(Trade.status != "pending")
                       .filter((Trade.initiator_roster_id == roster_id) |
                               (Trade.receiver_roster_id == roster_id))
                       .all())
        for t in hist_trades:
            if t.initiator_roster_id == roster_id:
                t.initiator_roster_id = None
            if t.receiver_roster_id == roster_id:
                t.receiver_roster_id = None
    except Exception:
        logger.exception("Trade cleanup failed (non-fatal)")

    # Unequip any traits on this roster row — they go BACK to the user's
    # inventory (at the level they were on) rather than being destroyed, exactly
    # like a release does. The player leaves; the traits the user paid gems for
    # stay theirs.
    traits_returned = 0
    try:
        from services.trait_service import return_traits_to_inventory
        traits_returned = return_traits_to_inventory(session, roster_id)
    except Exception:
        logger.exception("Trait return failed (non-fatal)")

    # Null captain pointer if it was this entry
    if getattr(user, "captain_roster_id", None) == roster_id:
        user.captain_roster_id = None

    session.flush()

    # Now delete the entry, refund the price, fix counters
    session.delete(entry)
    user.total_coins += price
    if gem_bonus:
        user.total_gems = (user.total_gems or 0) - gem_bonus
    user.roster_count = max(0, user.roster_count - 1)

    # Re-number remaining roster positions
    from handlers.release import _renumber_roster
    _renumber_roster(session, user.id)

    # Audit log
    from services.activity_service import log_activity
    log_activity(session, user.id, "undo_buy",
                 f"Undo buy of {player_name} ({rating} OVR) — refunded {price:,}"
                 + (f", reclaimed {gem_bonus:,} gems" if gem_bonus else ""),
                 coins_change=price, gems_change=-gem_bonus,
                 player_name=player_name, player_rating=rating)

    clear_pending(session, user.id)
    session.commit()

    traits_line = (f"\n💎 {traits_returned} trait(s) returned to inventory."
                   if traits_returned else "")
    bonus_back = (f"\n💎 Signing bonus reclaimed: <b>{gem_bonus:,}</b> gems"
                  if gem_bonus else "")
    await update.message.reply_text(
        f"↩️ <b>UNDO COMPLETE</b>\n\n"
        f"Removed: <b>{player_name}</b> ({rating} OVR)\n"
        f"💰 Refunded: <b>{price:,}</b> 🪙{bonus_back}\n"
        f"💳 Balance: {user.total_coins:,} 🪙\n"
        f"📊 Roster: {user.roster_count}/{MAX_ROSTER}{traits_line}",
        parse_mode="HTML")


async def _undo_release(update, session, user, payload):
    """Undo a release: restore player(s) to roster, deduct refund."""
    items = payload.get("items", [])
    total_refund = payload.get("total_refund", 0)

    if not items:
        await update.message.reply_text(
            "⚠️ Nothing to restore in this undo.")
        clear_pending(session, user.id)
        session.commit()
        return

    # Check roster space — abort if not enough room
    needed = len(items)
    current = user.roster_count or 0
    if current + needed > MAX_ROSTER:
        names = ", ".join(it["player_name"] for it in items[:5])
        if len(items) > 5:
            names += f", +{len(items) - 5} more"
        await update.message.reply_text(
            f"⚠️ <b>Can't undo release</b>\n\n"
            f"Roster has {current}/{MAX_ROSTER} slots — "
            f"can't fit {needed} more players ({names}).\n\n"
            f"<i>Release some other players first, or the undo window will "
            f"expire.</i>",
            parse_mode="HTML")
        # Don't clear — let user try again after making room
        session.commit()
        return

    # Check the user has enough coins for the refund
    if (user.total_coins or 0) < total_refund:
        await update.message.reply_text(
            f"⚠️ <b>Can't undo release</b>\n\n"
            f"Need <b>{total_refund:,}</b> 🪙 to undo, but you only have "
            f"<b>{user.total_coins:,}</b> 🪙.\n\n"
            f"<i>Spent the release money already? Top up before undo expires.</i>",
            parse_mode="HTML")
        session.commit()
        return

    # Verify all players still exist (haven't been deleted)
    missing = []
    for it in items:
        pl = session.query(Player).get(it["player_id"])
        if not pl:
            missing.append(it["player_name"])
    if missing:
        await update.message.reply_text(
            f"⚠️ <b>Can't undo release</b>\n\n"
            f"Some players no longer exist in the database: "
            f"{', '.join(missing)}.\n\n"
            f"<i>This shouldn't normally happen.</i>",
            parse_mode="HTML")
        clear_pending(session, user.id)
        session.commit()
        return

    # Check user doesn't already own a different version of any of these
    # (rare — could happen if user bought a version of a released player in
    # the 60s window)
    blocked = []
    for it in items:
        try:
            from services.version_service import user_owns_any_version
            if user_owns_any_version(session, user.id, it["player_id"]):
                blocked.append(it["player_name"])
        except Exception:
            # If the helper isn't available, just check exact player_id
            exists = (session.query(UserRoster)
                      .filter(UserRoster.user_id == user.id,
                              UserRoster.player_id == it["player_id"]).first())
            if exists:
                blocked.append(it["player_name"])
    if blocked:
        await update.message.reply_text(
            f"⚠️ <b>Can't undo release</b>\n\n"
            f"You already own a version of: {', '.join(blocked)}\n\n"
            f"<i>The undo would create duplicate ownership.</i>",
            parse_mode="HTML")
        session.commit()
        return

    # All checks passed — restore players
    from datetime import datetime as _dt
    restored = []
    next_pos = (user.roster_count or 0) + 1
    for it in items:
        entry = UserRoster(
            user_id=user.id, player_id=it["player_id"],
            order_position=next_pos,
            acquired_date=_dt.utcnow(),
        )
        session.add(entry)
        next_pos += 1
        user.roster_count = (user.roster_count or 0) + 1
        restored.append(it["player_name"])

    user.total_coins = (user.total_coins or 0) - total_refund

    from services.activity_service import log_activity
    log_activity(session, user.id, "undo_release",
                 f"Undo release of {len(items)} player(s) — deducted {total_refund:,}",
                 coins_change=-total_refund)

    clear_pending(session, user.id)
    session.commit()

    names_str = ", ".join(restored[:8])
    if len(restored) > 8:
        names_str += f", +{len(restored) - 8} more"

    await update.message.reply_text(
        f"↩️ <b>UNDO COMPLETE</b>\n\n"
        f"Restored {len(items)} player(s): {names_str}\n\n"
        f"💸 Deducted: <b>{total_refund:,}</b> 🪙\n"
        f"💳 Balance: {user.total_coins:,} 🪙\n"
        f"📊 Roster: {user.roster_count}/{MAX_ROSTER}",
        parse_mode="HTML")

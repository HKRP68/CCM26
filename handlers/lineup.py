"""Handlers for /playingxi (/pxi), /swapplayers (/swappl), /setcaptain."""

import logging
from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import User, Player, UserRoster
from services.activity_service import log_activity

logger = logging.getLogger(__name__)


def _ensure_order(session, user_id):
    """Make sure all roster entries have sequential order_position."""
    entries = (
        session.query(UserRoster)
        .filter(UserRoster.user_id == user_id)
        .order_by(UserRoster.order_position, UserRoster.acquired_date)
        .all()
    )
    for i, e in enumerate(entries, 1):
        if e.order_position != i:
            e.order_position = i
    session.flush()
    return entries


def _get_ordered_roster(session, user_id):
    """Return list of (UserRoster, Player) ordered by position."""
    _ensure_order(session, user_id)
    return (
        session.query(UserRoster, Player)
        .join(Player, UserRoster.player_id == Player.id)
        .filter(UserRoster.user_id == user_id)
        .order_by(UserRoster.order_position)
        .all()
    )


async def playingxi_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        roster = _get_ordered_roster(session, user.id)
        session.commit()

        if len(roster) < 11:
            await update.message.reply_text(
                f"❌ You need at least 11 players. You have {len(roster)}.\nUse /claim to get more!")
            return

        top_11 = roster[:11]
        team_name = user.team_name or f"@{user.username or user.first_name}'s XI"

        # Find captain
        captain_id = user.captain_roster_id

        # Group by category
        batsmen = []
        keepers = []
        allrounders = []
        bowlers = []

        total_ovr = 0
        for entry, player in top_11:
            total_ovr += player.rating
            cap = " ©️" if entry.id == captain_id else ""
            line = f"• {player.name} — {player.rating}{cap}"

            cat = player.category
            if cat == "Batsman":
                batsmen.append(line)
            elif cat == "Wicket Keeper":
                keepers.append(line)
            elif cat == "All-rounder":
                allrounders.append(line)
            elif cat == "Bowler":
                bowlers.append(line)
            else:
                batsmen.append(line)  # fallback

        avg_ovr = round(total_ovr / 11, 1)

        lines = [
            f"🏏 <b>PLAYING XI</b>\n",
            f"👑 <b>{team_name}</b>",
            f"⭐ <b>Avg Rating:</b> {avg_ovr}\n",
            "━━━━━━━━━━━━━━━━━━━\n",
        ]

        if batsmen:
            lines.append("🏏 <b>BATSMEN</b>")
            lines.extend(batsmen)
            lines.append("")

        if keepers:
            lines.append("🧤 <b>WICKET-KEEPER</b>")
            lines.extend(keepers)
            lines.append("")

        if allrounders:
            lines.append("🏏 <b>ALL-ROUNDERS</b>")
            lines.extend(allrounders)
            lines.append("")

        if bowlers:
            lines.append("⚾ <b>BOWLERS</b>")
            lines.extend(bowlers)
            lines.append("")

        lines.append("━━━━━━━━━━━━━━━━━━━\n")
        lines.append(f"⚡ <b>Total OVR:</b> {total_ovr}")
        lines.append(f"📈 <b>Avg per Player:</b> {avg_ovr}")

        # Show bench if exists
        bench = roster[11:]
        if bench:
            lines.append(f"\n📋 <b>Bench ({len(bench)}):</b>")
            for entry, player in bench:
                pos = entry.order_position
                lines.append(f"  {pos}. {player.name} — {player.rating} | {player.category}")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    except Exception:
        logger.exception(f"PlayingXI error for {tg_user.id}")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


async def swapplayers_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user

    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: /swapplayers <pos1> <pos2>\nExample: /swapplayers 9 13")
        return

    try:
        pos1 = int(context.args[0])
        pos2 = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Positions must be numbers.")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        entries = _ensure_order(session, user.id)

        if pos1 < 1 or pos2 < 1 or pos1 > len(entries) or pos2 > len(entries):
            await update.message.reply_text(f"❌ Positions must be between 1 and {len(entries)}")
            return
        if pos1 == pos2:
            await update.message.reply_text("❌ Same position")
            return

        e1 = entries[pos1 - 1]
        e2 = entries[pos2 - 1]
        e1.order_position, e2.order_position = e2.order_position, e1.order_position

        p1 = session.query(Player).get(e1.player_id)
        p2 = session.query(Player).get(e2.player_id)

        log_activity(session, user.id, "swap", f"Swapped #{pos1} {p1.name} ↔ #{pos2} {p2.name}")
        session.commit()

        xi_note = ""
        if pos1 <= 11 or pos2 <= 11:
            xi_note = "\n🏏 Playing XI updated!"

        await update.message.reply_text(
            f"✅ Swapped #{pos1} {p1.name} ↔ #{pos2} {p2.name}{xi_note}")

    except Exception:
        session.rollback()
        logger.exception(f"Swap error")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


async def setcaptain_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user

    if not context.args:
        await update.message.reply_text("Usage: /setcaptain <player name>")
        return

    search_name = " ".join(context.args).strip()
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        result = (
            session.query(UserRoster, Player)
            .join(Player, UserRoster.player_id == Player.id)
            .filter(UserRoster.user_id == user.id, Player.name.ilike(f"%{search_name}%"))
            .first()
        )
        if not result:
            await update.message.reply_text(f"❌ Player '{search_name}' not in your roster")
            return

        entry, player = result
        user.captain_roster_id = entry.id
        log_activity(session, user.id, "captain", f"Set captain: {player.name}",
                     player_name=player.name, player_rating=player.rating)
        session.commit()

        await update.message.reply_text(
            f"👑 <b>{player.name}</b> is now your team captain!", parse_mode="HTML")

    except Exception:
        session.rollback()
        logger.exception(f"SetCaptain error")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()

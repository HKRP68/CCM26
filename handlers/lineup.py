"""Handlers for /playingxi (/pxi), /swapplayers (/swappl), /setcaptain."""

import logging
from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import User, Player, UserRoster
from services.activity_service import log_activity
from services.flags import get_flag
from services.bowling_service import is_spinner as _is_spin

logger = logging.getLogger(__name__)


def _ensure_order(session, user_id):
    entries = (session.query(UserRoster).filter(UserRoster.user_id == user_id)
               .order_by(UserRoster.order_position, UserRoster.acquired_date).all())
    for i, e in enumerate(entries, 1):
        if e.order_position != i:
            e.order_position = i
    session.flush()
    return entries


def _get_ordered_roster(session, user_id):
    _ensure_order(session, user_id)
    return (session.query(UserRoster, Player).join(Player, UserRoster.player_id == Player.id)
            .filter(UserRoster.user_id == user_id).order_by(UserRoster.order_position).all())


def format_xi_text(roster_list, team_name, captain_rid=None):
    """Build the 5-section Playing XI text.
    roster_list: list of (UserRoster, Player) — first 11 are XI.
    Returns formatted HTML string.
    """
    top_11 = roster_list[:11]
    bench = roster_list[11:]
    count = len(top_11)

    batsmen, keepers, allrounders, pacers, spinners = [], [], [], [], []
    total_ovr = 0
    for entry, player in top_11:
        total_ovr += player.rating
        flag = get_flag(player.country)
        cap = " ©️" if entry.id == captain_rid else ""
        pos = entry.order_position
        line = f"{pos}. {player.name} | {player.rating} | {player.bat_rating} | {player.bowl_rating} | {flag}{cap}"

        cat = player.category
        if cat == "Batsman":
            batsmen.append(line)
        elif cat == "Wicket Keeper":
            keepers.append(line)
        elif cat == "All-rounder":
            allrounders.append(line)
        elif cat == "Bowler":
            if _is_spin(player.bowl_style):
                spinners.append(line)
            else:
                pacers.append(line)
        else:
            batsmen.append(line)

    avg_ovr = round(total_ovr / count, 1) if count else 0

    lines = [
        f"🏏 <b>PLAYING XI</b>\n",
        f"👑 <b>{team_name}</b>",
        f"⭐ Avg Rating: {avg_ovr}\n",
        "━━━━━━━━━━━━━━━━━━━\n",
    ]

    # Each section in blockquote format
    if batsmen:
        lines.append("🏏 <b>BATSMEN</b>")
        lines.append("<blockquote>" + "\n".join(batsmen) + "</blockquote>\n")

    if keepers:
        lines.append("🧤 <b>WICKET-KEEPERS</b>")
        lines.append("<blockquote>" + "\n".join(keepers) + "</blockquote>\n")

    if allrounders:
        lines.append("👥 <b>ALL-ROUNDERS</b>")
        lines.append("<blockquote>" + "\n".join(allrounders) + "</blockquote>\n")

    if pacers:
        lines.append("🔥 <b>PACERS</b>")
        lines.append("<blockquote>" + "\n".join(pacers) + "</blockquote>\n")

    if spinners:
        lines.append("🌀 <b>SPINNERS</b>")
        lines.append("<blockquote>" + "\n".join(spinners) + "</blockquote>\n")

    lines.append("━━━━━━━━━━━━━━━━━━━\n")
    lines.append(f"⚡ Total OVR: {total_ovr}")
    lines.append(f"📈 Avg per Player: {avg_ovr}")

    if bench:
        lines.append(f"\n📋 <b>Bench ({len(bench)}):</b>")
        for entry, player in bench:
            flag = get_flag(player.country)
            lines.append(f"  {entry.order_position}. {player.name} | {player.rating} | {flag}")

    return "\n".join(lines)


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

        if not roster:
            await update.message.reply_text("❌ No players. Use /claim to get some!")
            return

        team_name = user.team_name or f"@{user.username or user.first_name}'s XI"
        text = format_xi_text(roster, team_name, user.captain_roster_id)
        await update.message.reply_text(text, parse_mode="HTML")

    except Exception:
        logger.exception(f"PlayingXI error")
        await update.message.reply_text("⚠️ Error.")
    finally:
        session.close()


async def swapplayers_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: /swapplayers <pos1> <pos2>")
        return
    try:
        pos1, pos2 = int(context.args[0]), int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Numbers only.")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return
        entries = _ensure_order(session, user.id)
        if pos1 < 1 or pos2 < 1 or pos1 > len(entries) or pos2 > len(entries) or pos1 == pos2:
            await update.message.reply_text(f"❌ Positions 1-{len(entries)}, different.")
            return
        e1, e2 = entries[pos1 - 1], entries[pos2 - 1]
        e1.order_position, e2.order_position = e2.order_position, e1.order_position
        p1 = session.query(Player).get(e1.player_id)
        p2 = session.query(Player).get(e2.player_id)
        log_activity(session, user.id, "swap", f"Swapped #{pos1} {p1.name} ↔ #{pos2} {p2.name}")
        session.commit()
        xi_note = "\n🏏 Playing XI updated!" if pos1 <= 11 or pos2 <= 11 else ""
        await update.message.reply_text(f"✅ Swapped #{pos1} {p1.name} ↔ #{pos2} {p2.name}{xi_note}")
    except Exception:
        session.rollback()
        logger.exception("Swap err")
        await update.message.reply_text("⚠️ Error.")
    finally:
        session.close()


async def setcaptain_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    if not context.args:
        await update.message.reply_text("Usage: /setcaptain <player name>")
        return
    search = " ".join(context.args).strip()
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return
        result = (session.query(UserRoster, Player).join(Player, UserRoster.player_id == Player.id)
                  .filter(UserRoster.user_id == user.id, Player.name.ilike(f"%{search}%")).first())
        if not result:
            await update.message.reply_text(f"❌ '{search}' not in roster")
            return
        entry, player = result
        user.captain_roster_id = entry.id
        log_activity(session, user.id, "captain", f"Captain: {player.name}", player_name=player.name)
        session.commit()
        await update.message.reply_text(f"👑 <b>{player.name}</b> is now captain!", parse_mode="HTML")
    except Exception:
        session.rollback()
        logger.exception("Captain err")
        await update.message.reply_text("⚠️ Error.")
    finally:
        session.close()

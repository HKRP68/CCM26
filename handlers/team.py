"""Handlers for /teamname, /purse, /stats."""

import re
import logging
from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import User, Player, UserRoster, PlayerGameStats
from config import get_buy_value, get_sell_value
from services.activity_service import log_activity
from services.flags import get_flag

logger = logging.getLogger(__name__)

TEAM_NAME_REGEX = re.compile(r"^[a-zA-Z0-9 '\-]{3,50}$")


async def teamname_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user

    if not context.args:
        await update.message.reply_text("Usage: /teamname <name>\nExample: /teamname Royal Challengers")
        return

    name = " ".join(context.args).strip()
    if not TEAM_NAME_REGEX.match(name):
        await update.message.reply_text(
            "❌ Team name must be 3-50 characters, letters/numbers/spaces only"
        )
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        old = user.team_name or "None"
        user.team_name = name
        log_activity(session, user.id, "teamname", f"Team name: {old} → {name}")
        session.commit()

        await update.message.reply_text(
            f"✅ Team name set to: <b>{name}</b>", parse_mode="HTML"
        )
    except Exception:
        session.rollback()
        logger.exception(f"Teamname error for {tg_user.id}")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


async def purse_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        team = user.team_name or "No team name set"
        await update.message.reply_text(
            f"👤 <b>@{user.username or user.first_name}</b>\n"
            f"🏏 Team: {team}\n\n"
            f"💰 <b>Coins:</b> {user.total_coins:,}\n"
            f"💎 <b>Gems:</b> {user.total_gems}\n"
            f"📊 Roster: {user.roster_count}/25",
            parse_mode="HTML",
        )
    except Exception:
        logger.exception(f"Purse error for {tg_user.id}")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stats <player_name> — show per-owner game stats."""
    tg_user = update.effective_user

    if not context.args:
        await update.message.reply_text("Usage: /stats <player name>\nExample: /stats Virat Kohli")
        return

    search = " ".join(context.args).strip()
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        # Check user owns this player
        result = (
            session.query(UserRoster, Player)
            .join(Player, UserRoster.player_id == Player.id)
            .filter(UserRoster.user_id == user.id, Player.name.ilike(f"%{search}%"))
            .first()
        )

        if not result:
            await update.message.reply_text(
                f"❌ Player '{search}' not in your roster. You can only view stats of players you own."
            )
            return

        entry, player = result
        flag = get_flag(player.country)

        # Get or create game stats
        gs = (
            session.query(PlayerGameStats)
            .filter(PlayerGameStats.user_id == user.id, PlayerGameStats.player_id == player.id)
            .first()
        )

        if not gs:
            # No stats yet — never played
            gs = PlayerGameStats(user_id=user.id, player_id=player.id)
            session.add(gs)
            session.commit()

        buy_val = get_buy_value(player.rating)
        owner_name = f"@{user.username}" if user.username else user.first_name

        text = (
            f"📛 <b>{player.name}</b> {flag}\n"
            f"⭐ {player.rating} OVR | {player.category}\n\n"
            f"<code>"
            f"Owner: {owner_name}\n"
            f"Value: {buy_val:,} 🪙\n"
            f"POTM(s): {gs.potm}\n"
            f"\n"
            f"{'🏏 BATTING':<20}{'🎯 BOWLING'}\n"
            f"{'─' * 38}\n"
            f"Inns: {gs.bat_inns:<14}Inns: {gs.bowl_inns}\n"
            f"Runs: {gs.runs:<14}Wickets: {gs.wickets_taken}\n"
            f"50s: {gs.fifties:<15}3-Fers: {gs.three_fers}\n"
            f"100s: {gs.hundreds:<14}5-Fers: {gs.five_fers}\n"
            f"4/6: {gs.fours}/{gs.sixes:<12}Hattricks: {gs.hattricks}\n"
            f"Avg: {gs.bat_avg:<14}Avg: {gs.bowl_avg}\n"
            f"SR: {gs.bat_sr:<15}Economy: {gs.bowl_economy}\n"
            f"Ducks: {gs.ducks:<13}SR: {gs.bowl_sr}\n"
            f"HS: {gs.hs_str:<15}BBF: {gs.bbf_str}\n"
            f"</code>"
        )

        await update.message.reply_text(text, parse_mode="HTML")

    except Exception:
        logger.exception(f"Stats error for {tg_user.id}")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()

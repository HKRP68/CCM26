"""Handlers for /searchpl and /searchovr."""

import logging
from telegram import Update
from telegram.ext import ContextTypes
from sqlalchemy import and_

from database import get_session
from models import Player, User
from config import get_buy_value, get_sell_value

logger = logging.getLogger(__name__)

MAX_RESULTS = 20


async def searchpl_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/searchpl <name> — search players by name."""
    tg_user = update.effective_user

    if not context.args:
        await update.message.reply_text("Usage: /searchpl <player name>\nExample: /searchpl Virat")
        return

    search = " ".join(context.args).strip()
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        players = (
            session.query(Player)
            .filter(Player.name.ilike(f"%{search}%"), Player.is_active == True)
            .order_by(Player.rating.desc())
            .limit(MAX_RESULTS)
            .all()
        )

        if not players:
            await update.message.reply_text(f"❌ No players found matching '{search}'")
            return

        lines = [f"🔍 <b>Search: '{search}'</b> ({len(players)} results)\n"]
        for p in players:
            buy = get_buy_value(p.rating)
            lines.append(
                f"• <b>{p.name}</b> - {p.rating} OVR | {p.category}\n"
                f"  {p.country} | 💰 {buy:,} 🪙"
            )

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    except Exception:
        logger.exception(f"SearchPl error for {tg_user.id}")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


async def searchovr_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/searchovr <rating> [category] [country]"""
    tg_user = update.effective_user

    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "/searchovr 85\n"
            "/searchovr 85 Batsman\n"
            "/searchovr 85 Bowler India\n"
            "/searchovr 90 ALR\n"
            "/searchovr 79 WK England"
        )
        return

    try:
        rating = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ First argument must be a rating number")
        return

    # Parse optional category and country
    category = None
    country = None
    remaining = context.args[1:]

    # Map short aliases
    cat_map = {
        "batsman": "Batsman", "bat": "Batsman",
        "bowler": "Bowler", "bowl": "Bowler",
        "all-rounder": "All-rounder", "alr": "All-rounder", "allrounder": "All-rounder",
        "wicket keeper": "Wicket Keeper", "wk": "Wicket Keeper", "wicketkeeper": "Wicket Keeper",
    }

    if remaining:
        first = remaining[0].lower()
        if first in cat_map:
            category = cat_map[first]
            remaining = remaining[1:]
        elif len(remaining) >= 2:
            # Try two-word category like "wicket keeper"
            two_word = (remaining[0] + " " + remaining[1]).lower()
            if two_word in cat_map:
                category = cat_map[two_word]
                remaining = remaining[2:]

    if remaining:
        country = " ".join(remaining).strip()

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        query = session.query(Player).filter(Player.rating == rating, Player.is_active == True)
        if category:
            query = query.filter(Player.category == category)
        if country:
            query = query.filter(Player.country.ilike(f"%{country}%"))

        players = query.order_by(Player.name).limit(MAX_RESULTS).all()
        total = query.count()

        if not players:
            filter_desc = f"{rating} OVR"
            if category:
                filter_desc += f" {category}"
            if country:
                filter_desc += f" {country}"
            await update.message.reply_text(f"❌ No players found at {filter_desc}")
            return

        header = f"🔍 <b>{rating} OVR"
        if category:
            header += f" — {category}"
        if country:
            header += f" — {country}"
        header += f"</b> ({total} found)\n"

        lines = [header]
        for p in players:
            buy = get_buy_value(p.rating)
            lines.append(
                f"• <b>{p.name}</b> | {p.category}\n"
                f"  {p.country} | 💰 {buy:,} 🪙"
            )

        if total > MAX_RESULTS:
            lines.append(f"\n... and {total - MAX_RESULTS} more")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    except Exception:
        logger.exception(f"SearchOVR error for {tg_user.id}")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()

"""Platinum recurring drops: /cmuweekly (weekly guaranteed card) and
/cmuchest (coin chests every 10 days)."""

import html
import logging
import random
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import User, UserStats, UserRoster
from config import (
    MAX_ROSTER, get_sell_value,
    WEEKLY_CARD_MIN_OVR, WEEKLY_CARD_MAX_OVR, WEEKLY_CARD_COOLDOWN_DAYS,
)
from services import subscription_service
from services.activity_service import log_activity
from services.cooldown_service import check_cooldown, format_remaining
from services.player_service import get_random_player_by_rating_range

logger = logging.getLogger(__name__)


def _ensure_stats(session, user_id: int) -> UserStats:
    """Return the user's UserStats row, creating it if missing."""
    stats = session.query(UserStats).filter(UserStats.user_id == user_id).first()
    if stats is None:
        stats = UserStats(user_id=user_id)
        session.add(stats)
        session.flush()
    return stats


# ── /cmuweekly ──────────────────────────────────────────────────────

async def cmuweekly_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Grant a Platinum subscriber their weekly guaranteed 85+ card (7-day cd)."""
    uid = update.effective_user.id
    session = get_session()
    try:
        # Row-lock the user so concurrent claims serialize on the cooldown check
        # (real lock on Postgres, no-op on SQLite).
        user = (session.query(User).filter(User.telegram_id == uid)
                .with_for_update().first())
        if not user:
            await update.message.reply_text("❌ Use /debut first.")
            return
        if not subscription_service.has_weekly_card(user):
            await update.message.reply_text(
                subscription_service.premium_required_message(
                    "The Weekly Card (🏆 Platinum only)"),
                parse_mode="HTML")
            return

        stats = _ensure_stats(session, user.id)
        cooldown = WEEKLY_CARD_COOLDOWN_DAYS * 86400
        ready, remaining = check_cooldown(stats, "last_weekly", cooldown)
        if not ready:
            await update.message.reply_text(
                f"⏳ Your next Weekly Card unlocks in "
                f"<b>{format_remaining(remaining)}</b>.",
                parse_mode="HTML")
            return

        player = get_random_player_by_rating_range(
            session, WEEKLY_CARD_MIN_OVR, WEEKLY_CARD_MAX_OVR)
        line, extra = _grant_player(session, user, player)
        stats.last_weekly = datetime.utcnow()
        log_activity(session, user.id, "cmuweekly",
                     f"Weekly card: {player.name if player else 'none'}",
                     coins_change=extra)
        session.commit()

        await update.message.reply_text(
            "🏆 <b>Weekly Bonus Card!</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            f"{line}\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "<i>Guaranteed 85+ OVR. See you next week!</i>",
            parse_mode="HTML")
    finally:
        session.close()


# ── /cmuchest ───────────────────────────────────────────────────────

async def cmuchest_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Grant a Platinum subscriber their recurring coin chests (10-day cd)."""
    uid = update.effective_user.id
    session = get_session()
    try:
        # Row-lock the user so concurrent claims serialize on the cooldown check
        # (real lock on Postgres, no-op on SQLite).
        user = (session.query(User).filter(User.telegram_id == uid)
                .with_for_update().first())
        if not user:
            await update.message.reply_text("❌ Use /debut first.")
            return
        chest_cfg = subscription_service.coin_chest_config(user)
        if not chest_cfg:
            await update.message.reply_text(
                subscription_service.premium_required_message(
                    "Coin Chests (🏆 Platinum only)"),
                parse_mode="HTML")
            return

        stats = _ensure_stats(session, user.id)
        cooldown = int(chest_cfg.get("cooldown_days", 10)) * 86400
        ready, remaining = check_cooldown(stats, "last_coinchest", cooldown)
        if not ready:
            await update.message.reply_text(
                f"⏳ Your next Coin Chests unlock in "
                f"<b>{format_remaining(remaining)}</b>.",
                parse_mode="HTML")
            return

        count = int(chest_cfg.get("count", 3))
        lo = int(chest_cfg.get("min", 60000))
        hi = int(chest_cfg.get("max", 99000))
        amounts = [random.randint(lo, hi) for _ in range(count)]
        total = sum(amounts)
        user.total_coins = (user.total_coins or 0) + total
        stats.last_coinchest = datetime.utcnow()
        log_activity(session, user.id, "cmuchest",
                     f"Coin chests: {amounts} = +{total}", coins_change=total)
        session.commit()

        chest_lines = "\n".join(
            f"📦 Chest {i + 1}: <b>+{amt:,}</b> coins"
            for i, amt in enumerate(amounts))
        await update.message.reply_text(
            "🪙 <b>Coin Chests Opened!</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            f"{chest_lines}\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Total: <b>+{total:,}</b> coins\n"
            f"💵 Balance: <b>{user.total_coins:,}</b>",
            parse_mode="HTML")
    finally:
        session.close()


def _grant_player(session, user, player):
    """Add the player to the roster, or convert to coins if the squad is full.
    Returns (display_line, extra_coins_credited)."""
    if player is None:
        return ("🃏 Player: <i>none available</i>", 0)
    name = html.escape(player.name)  # names are rendered with parse_mode=HTML
    if (user.roster_count or 0) < MAX_ROSTER:
        entry = UserRoster(user_id=user.id, player_id=player.id,
                           order_position=(user.roster_count or 0) + 1,
                           acquired_date=datetime.utcnow())
        session.add(entry)
        user.roster_count = (user.roster_count or 0) + 1
        return (f"🃏 <b>{name}</b> — {player.rating} OVR added to squad", 0)
    sell_val = get_sell_value(player.rating)
    user.total_coins = (user.total_coins or 0) + sell_val
    return (f"🃏 <b>{name}</b> — {player.rating} OVR (squad full → "
            f"+{sell_val:,} coins)", sell_val)

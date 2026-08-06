"""Top-tier recurring drops: /cmuweekly (weekly guaranteed card) and /cmuchest
(coin chests — Platinum 3 per 10 days, Diamond 5 per 7 days).

Both are config-driven perks: any tier whose SUBSCRIPTION_TIERS entry sets
``weekly_card`` / ``coin_chests`` unlocks them, so tiers are never hard-coded
here."""

import html
import logging
import random
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import User, UserStats, UserRoster
from config import (
    MAX_ROSTER, get_sell_value, MYSTERYBOX_PLAYER_BANDS,
    WEEKLY_CARD_COOLDOWN_DAYS,
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


def _weighted_weekly_band() -> tuple[int, int]:
    """Roll an 85+ OVR band weighted so 85-87 is most likely (reuses the
    Mystery Box player-band weights); returns (ovr_low, ovr_high)."""
    roll = random.random() * 100.0
    for ceiling, low, high, _label in MYSTERYBOX_PLAYER_BANDS:
        if roll <= ceiling:
            return low, high
    _, low, high, _ = MYSTERYBOX_PLAYER_BANDS[-1]
    return low, high


# ── /cmuweekly ──────────────────────────────────────────────────────

async def cmuweekly_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Grant an eligible subscriber their weekly guaranteed 85+ card (7-day cd)."""
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
                    "The Weekly Card", min_tier="platinum"),
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

        low, high = _weighted_weekly_band()
        # source=None: this card is part of a paid subscription, so the
        # website's blocked ranges (claim/daily/gspin only) never trim it.
        player = get_random_player_by_rating_range(session, low, high,
                                                   source=None)
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
    """Grant an eligible subscriber their recurring coin chests."""
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
                    "Coin Chests", min_tier="platinum"),
                parse_mode="HTML")
            return

        stats = _ensure_stats(session, user.id)
        count = int(chest_cfg.get("count", 3))
        lo = int(chest_cfg.get("min", 60000))
        hi = int(chest_cfg.get("max", 99000))
        cooldown = int(chest_cfg.get("cooldown_days", 10)) * 86400

        # Quota cycle: up to `count` chests per `cooldown` window, ONE per open.
        # last_coinchest marks the current cycle's start; coinchest_used counts
        # opens within it. A new cycle begins on the first open after the window.
        now = datetime.utcnow()
        cycle_start = stats.last_coinchest
        used = stats.coinchest_used or 0
        if cycle_start is None or (now - cycle_start).total_seconds() >= cooldown:
            stats.last_coinchest = now
            stats.coinchest_used = 1
        elif used < count:
            stats.coinchest_used = used + 1
        else:
            reset_in = int(cooldown - (now - cycle_start).total_seconds())
            await update.message.reply_text(
                f"📦 You've opened all <b>{count}</b> coin chests this cycle.\n"
                f"⏳ Your next chests unlock in <b>{format_remaining(reset_in)}</b>.",
                parse_mode="HTML")
            return

        remaining_this_cycle = count - stats.coinchest_used
        amount = random.randint(lo, hi)
        user.total_coins = (user.total_coins or 0) + amount
        log_activity(session, user.id, "cmuchest",
                     f"Coin chest {stats.coinchest_used}/{count}: +{amount}",
                     coins_change=amount)
        session.commit()

        await update.message.reply_text(
            "🪙 <b>Coin Chest Opened!</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>+{amount:,}</b> coins\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            f"🎁 Chests left this cycle: <b>{remaining_this_cycle}</b>\n"
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

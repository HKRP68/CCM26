"""Handler for /daily — button first, claim flow for players if squad full."""

import io
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, Player, UserRoster, UserStats
from services.player_service import get_random_player_by_rarity, get_random_player_by_rating_range
from utils.idempotency import claim_once, release
from services.cooldown_service import check_cooldown, format_remaining
from services.miniapp_buttons import has_miniapp_url, miniapp_button
from services.quota_service import get_quota_status
from services.streak_service import update_streak
from services.card_text import format_player_card
from services.activity_service import log_activity
from config import DAILY_COOLDOWN, DAILY_COINS, STREAK_MILESTONE, MAX_ROSTER, get_sell_value

logger = logging.getLogger(__name__)


async def daily_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    session = get_session()
    try:
        from services.command_config_service import (
            is_command_enabled, get_disabled_message, get_cooldown,
            get_coin_amount, get_player_count,
        )
        if not is_command_enabled(session, "daily"):
            await update.message.reply_text(
                get_disabled_message(session, "daily"), parse_mode="HTML")
            return

        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            from services.message_service import get_msg
            await update.message.reply_text(get_msg("not_debuted"))
            return

        stats = session.query(UserStats).filter(UserStats.user_id == user.id).first()

        # Preview the actual amounts
        coin_amt = get_coin_amount(session, "daily", DAILY_COINS)
        player_amt = get_player_count(session, "daily", 2)

        # When the Mini App is configured, use the same quota availability that
        # /api/webapp/daily uses (1 free slot + ad-gated slots per cycle).
        chat_type = (update.effective_chat.type
                     if update.effective_chat else "private")
        is_private = (chat_type == "private")
        if has_miniapp_url():
            quota = get_quota_status(stats, "daily", session=session)
            if quota["all_used"]:
                await update.message.reply_text(
                    "⏳ All daily claims are used. New claims in "
                    f"{format_remaining(quota['cycle_reset_in'])}.",
                    parse_mode="HTML")
                return

            btn = miniapp_button("📅 Open Mini App to Claim", "daily",
                                 is_private=is_private,
                                 origin_chat_id=(update.effective_chat.id
                                                 if update.effective_chat else None))
            if btn is not None:
                text = (
                    "📅 <b>Your daily reward is ready!</b>\n\n"
                    f"💰 +{coin_amt:,} coins, {player_amt} player card(s), "
                    "and streak rewards await. Use your free claim or watch "
                    "a quick ad in the Mini App.\n\n"
                    "<i>Tap below to open Daily.</i>"
                )
                await update.message.reply_text(
                    text, parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[btn]]))
                return

        effective_cooldown = get_cooldown(session, "daily", DAILY_COOLDOWN)
        ready, remaining = check_cooldown(stats, "last_daily", effective_cooldown)
        if not ready:
            from services.message_service import get_msg
            await update.message.reply_text(
                get_msg("daily_cooldown", remaining=format_remaining(remaining)),
                parse_mode="HTML")
            return

        # Legacy fallback when no Mini App URL configured (or group with no bot username)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🎁 Claim Daily Reward",
                                 callback_data=f"dailyclaim_{user.id}")
        ]])

        from services.message_service import get_msg
        await update.message.reply_text(
            get_msg("daily_available", coins=coin_amt, players=player_amt),
            parse_mode="HTML", reply_markup=keyboard)

    except Exception:
        logger.exception(f"Daily error")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


async def daily_claim_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    # Dedup rapid taps that race the cooldown write (the cooldown itself is the
    # once-per-day gate; this stops two concurrent clicks both passing it).
    key = f"daily_{query.message.chat_id}_{query.message.message_id}"
    if not claim_once(key):
        await query.answer("Already processing…")
        return
    await query.answer()
    tg_user = query.from_user

    user_id = int(query.data.split("_")[1])

    session = get_session()
    try:
        user = session.query(User).get(user_id)
        if not user or user.telegram_id != tg_user.id:
            release(key)
            return

        stats = session.query(UserStats).filter(UserStats.user_id == user.id).first()
        from services.command_config_service import get_cooldown
        effective_cooldown = get_cooldown(session, "daily", DAILY_COOLDOWN)
        ready, _ = check_cooldown(stats, "last_daily", effective_cooldown)
        if not ready:
            release(key)
            await query.edit_message_text("⏳ Already claimed today!")
            return

        # Update streak
        streak_count, milestone = update_streak(stats)

        # Pull daily coins/gems — prefer CommandReward, fall back to GameConfig
        from services.config_service import get_config
        from services.command_config_service import get_coin_amount, get_player_count, get_reward
        cfg = get_config(session)

        # Coins: CommandReward if set, else GameConfig
        coins_award = get_coin_amount(session, "daily", cfg["daily_coins"])
        # Gems: CommandReward if set, else GameConfig
        cr = get_reward(session, "daily")
        gems_award = (cr.gem_amount if cr and cr.gem_amount > 0
                       else cfg["daily_gems"])

        # Streak bonus (uses GameConfig values for now)
        if streak_count > 1:
            coins_award += cfg["daily_streak_bonus_coins"] * (streak_count - 1)
            gems_award += cfg["daily_streak_bonus_gems"] * (streak_count - 1)

        # Award coins + gems
        user.total_coins += coins_award
        if gems_award:
            user.total_gems = (user.total_gems or 0) + gems_award

        # Number of players to grant (admin-tunable)
        player_count = get_player_count(session, "daily", 2)

        # Generate players
        players = []
        for _ in range(player_count):
            p = get_random_player_by_rarity(session)
            if p:
                players.append(p)

        # Milestone bonus
        milestone_player = None
        if milestone:
            milestone_player = get_random_player_by_rating_range(session, 81, 85)

        stats.last_daily = datetime.utcnow()

        remaining_days = STREAK_MILESTONE - streak_count if streak_count > 0 else STREAK_MILESTONE

        # Build result text
        gems_line = f"💎 +{gems_award} gems\n" if gems_award else ""
        lines = [
            f"📅 <b>Daily Reward Claimed!</b>\n",
            f"✅ +{coins_award:,} coins",
            *([f"💎 +{gems_award} gems"] if gems_award else []),
            f"📊 Streak: {streak_count}/{STREAK_MILESTONE}",
            f"⏳ {remaining_days} days until bonus card\n",
        ]

        if milestone and milestone_player:
            lines.append(f"🎉 <b>MILESTONE!</b> 🏆 {milestone_player.name} - {milestone_player.rating} OVR\n")

        # Add players to roster if space, otherwise queue for claim flow
        players_to_claim = []
        all_players = players + ([milestone_player] if milestone_player else [])

        for p in all_players:
            if not p:
                continue
            if user.roster_count < MAX_ROSTER:
                entry = UserRoster(user_id=user.id, player_id=p.id,
                                   order_position=user.roster_count + 1,
                                   acquired_date=datetime.utcnow())
                session.add(entry)
                user.roster_count += 1
                lines.append(f"✅ {p.name} - {p.rating} OVR added to squad")
            else:
                players_to_claim.append(p)
                lines.append(f"⚠️ {p.name} - {p.rating} OVR (squad full — choose below)")

        pnames = ", ".join(p.name for p in all_players if p)
        log_activity(session, user.id, "daily",
                     f"Daily: +{coins_award} coins{', +' + str(gems_award) + ' gems' if gems_award else ''}, players: {pnames}, streak {streak_count}",
                     coins_change=coins_award, gems_change=gems_award)
        try:
            from services.quest_service import safe_track
            safe_track(session, user.id, "daily", 1)
        except Exception:
            pass
        session.commit()

        await query.edit_message_text("\n".join(lines), parse_mode="HTML")

        # For each player that couldn't auto-add (squad full), send a claim card
        for p in players_to_claim:
            sell_val = get_sell_value(p.rating)
            text = (
                f"⚠️ <b>Squad full — decide for this player:</b>\n\n"
                + format_player_card(p)
            )
            buttons = [
                [InlineKeyboardButton("🔴 Release", callback_data=f"release_{p.id}_{user.id}_{sell_val}")],
                [InlineKeyboardButton("⚪ Replace", callback_data=f"replace_{p.id}_{user.id}_{sell_val}")],
            ]
            await query.message.reply_text(text, parse_mode="HTML",
                                           reply_markup=InlineKeyboardMarkup(buttons))

    except Exception:
        # Keep the claim (may be post-commit) so a stale tap can't re-claim daily.
        session.rollback()
        logger.exception("Daily claim error")
    finally:
        session.close()

"""Handler for /debut command."""

import logging
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import User, UserRoster, UserStats
from services.player_service import get_players_for_debut
from config import DEBUT_COINS, DEBUT_GEMS
from services.activity_service import log_activity

logger = logging.getLogger(__name__)


async def debut_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    logger.info(f"/debut from user {tg_user.id} ({tg_user.username})")

    session = get_session()
    try:
        # Check enabled flag from BotCommand table
        from services.command_config_service import is_command_enabled, get_disabled_message, get_reward
        if not is_command_enabled(session, "debut"):
            await update.message.reply_text(
                get_disabled_message(session, "debut"), parse_mode="HTML")
            return

        existing = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if existing:
            from services.message_service import get_msg
            await update.message.reply_text(
                get_msg("debut_already"), parse_mode="HTML")
            return

        # Reward amounts: prefer CommandReward (DB), fall back to GameConfig.
        from services.config_service import get_config
        cfg = get_config(session)
        cr = get_reward(session, "debut")
        debut_coins = (cr.coin_amount if cr and cr.coin_amount > 0
                        else cfg["debut_coins"])
        debut_gems = (cr.gem_amount if cr and cr.gem_amount > 0
                       else cfg["debut_gems"])

        user = User(
            telegram_id=tg_user.id,
            username=tg_user.username or "",
            first_name=tg_user.first_name or "",
            total_coins=debut_coins,
            total_gems=debut_gems,
            roster_count=0,
        )
        session.add(user)
        session.flush()

        stats = UserStats(user_id=user.id)
        session.add(stats)

        players = get_players_for_debut(session)
        if not players:
            await update.message.reply_text(
                "⚠️ No players available in the database. Please contact admin."
            )
            session.rollback()
            return

        for p in players:
            entry = UserRoster(
                user_id=user.id, player_id=p.id, acquired_date=datetime.utcnow(),
            )
            session.add(entry)

        user.roster_count = len(players)
        log_activity(session, user.id, 'debut', f'Debut: {len(players)} players, {debut_coins} coins, {debut_gems} gems', coins_change=debut_coins, gems_change=debut_gems)

        # ── Referral completion ──
        # Record any pending referral (stashed from /start ref<id>) and
        # complete it now that the user has a real account.
        referral_reward_text = ""
        referral_completed_now = False
        try:
            from services.referral_service import record_referral, complete_referral
            from models import User as _U
            pending_inviter_tg = context.user_data.get("pending_inviter_tg_id") if hasattr(context, 'user_data') else None
            if pending_inviter_tg:
                inviter = session.query(_U).filter(_U.telegram_id == pending_inviter_tg).first()
                if inviter:
                    record_referral(session, inviter.id, user.id)
                context.user_data.pop("pending_inviter_tg_id", None)
            # Complete any referral pointing at this invitee (whether just
            # created or pre-existing from the start handler)
            cr = complete_referral(session, user.id)
            if cr:
                referral_completed_now = True
                if cr["paid_coins"] > 0 or cr["paid_gems"] > 0:
                    parts = []
                    if cr["paid_coins"]: parts.append(f"+{cr['paid_coins']:,} 🪙")
                    if cr["paid_gems"]: parts.append(f"+{cr['paid_gems']} 💎")
                    referral_reward_text = (
                        "\n\n🎁 Your inviter received " + " ".join(parts) + " for inviting you!"
                    )
        except Exception:
            logger.exception("Referral completion failed (non-fatal)")

        # Branding (admin-configurable)
        try:
            from services.referral_service import format_branding_html
            branding = format_branding_html(session)
        except Exception:
            branding = ""

        session.commit()

        lines = []
        for i, p in enumerate(players, 1):
            lines.append(f"  {i}. {p.name} - {p.rating} OVR | {p.category}")

        text = (
            "🎉 <b>Welcome to Cricket Bot!</b>\n"
            "✅ Your debut is complete!\n"
            f"✅ You received {len(players)} starting players\n\n"
            + "\n".join(lines) + "\n\n"
            f"📊 Your Roster: {len(players)}/25 players\n"
            f"💰 Coins: {debut_coins:,}\n"
            f"💎 Gems: {debut_gems}\n\n"
            "<b>Commands:</b>\n"
            "/claim - Get 1 player + 500 coins (hourly)\n"
            "/myroster - View your players\n"
            "/playerinfo [name] - Player details\n"
            "/daily - Daily reward (24h cooldown)\n"
            "/gspin - Spin wheel (8h cooldown)"
            + referral_reward_text
            + branding
        )
        await update.message.reply_text(
            text, parse_mode="HTML", disable_web_page_preview=True)

        # Return Mini App visitors to the app so the in-app starter guide can
        # walk them through their squad, XI, first reward and casual match.
        try:
            import os
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
            webapp_url = os.getenv("WEBAPP_URL", "").strip()
            if webapp_url.startswith("https://"):
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "🏏 Continue in Mini App",
                        web_app=WebAppInfo(url=webapp_url + "#onboarding"),
                    )
                ]])
                await update.message.reply_text(
                    "✅ <b>Your squad is ready.</b> Return to the Mini App to set "
                    "your Playing XI and play your first casual match.",
                    parse_mode="HTML", reply_markup=kb,
                )
        except Exception:
            logger.exception("Post-debut Mini App link failed (non-fatal)")

        # If no referral was completed by the link path, ask for a code.
        # Sets a flag so the text-message catcher knows they're a fresh
        # debutant whose next typed line might be a referral code.
        if not referral_completed_now:
            try:
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                context.user_data["awaiting_referral_code"] = True
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("⏭️ Skip", callback_data=f"refcode_skip_{user.id}"),
                ]])
                await update.message.reply_text(
                    "🎟️ <b>Got a referral code?</b>\n\n"
                    "If a friend gave you their 6-character code, send it now "
                    "and they'll earn a reward.\n\n"
                    "Reply with: <code>/redeem CODE</code>\n"
                    "or just type the code itself.\n\n"
                    "<i>Skip if you don't have one — you can always /redeem later.</i>",
                    parse_mode="HTML", reply_markup=kb,
                )
            except Exception:
                logger.exception("Referral code prompt failed (non-fatal)")

        logger.info(f"Debut complete for user {tg_user.id}, {len(players)} players assigned")

    except Exception:
        session.rollback()
        logger.exception(f"Debut error for user {tg_user.id}")
        await update.message.reply_text("⚠️ Database error. Please try again later.")
    finally:
        session.close()

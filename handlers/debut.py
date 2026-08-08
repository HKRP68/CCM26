"""Handler for /debut command."""

import logging
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from models import User, UserRoster, UserStats
from services.player_service import get_players_for_debut
from config import DEBUT_COINS, DEBUT_GEMS, MAX_ROSTER
from services.activity_service import log_activity
from services.miniapp_buttons import miniapp_button

logger = logging.getLogger(__name__)


def _build_post_debut_onboarding_text(players_count: int) -> str:
    """Return the short starter roadmap shown after a successful /debut."""
    return (
        "🏁 <b>Rookie Roadmap</b>\n"
        "Your account is live, but Cricket Bot has a lot to explore. "
        "Follow these first steps in order:\n\n"
        f"✅ <b>1. Here is your starter XI.</b> You now have {players_count} players ready to play.\n"
        "🎁 <b>2. Claim your first reward.</b> Use /claim for an hourly player + coins, "
        "then /daily for your daily streak reward.\n"
        "🏏 <b>3. Set your Playing XI.</b> Use /playingxi to review the team, "
        "/autobuild for a quick best XI, and /swapplayers if you want to adjust it.\n"
        "🤖 <b>4. Play your first bot match.</b> Use /vsbot 1 for a quick chat match "
        "or /wpmbot 1 for a Mini App match.\n"
        "🎯 <b>5. Open your first quest.</b> Use /myquest to see goals, progress, and rewards.\n\n"
        "Tip: if you are unsure what to do next, open /myquest or /howto."
    )


def _build_post_debut_onboarding_markup(chat=None):
    """Return an optional Mini App continuation button for the starter guide.

    Telegram only accepts native Web App buttons in private chats. Group and
    supergroup messages must use the Mini App deep-link fallback provided by
    ``miniapp_button``. The post-debut starter guide is rendered on the home
    dashboard, so target that routable screen instead of the pre-debut setup
    screen.
    """
    chat_type = getattr(chat, "type", "private") if chat else "private"
    is_private = chat_type == "private"
    btn = miniapp_button(
        "🏏 Continue Starter Guide in Mini App",
        "home",
        is_private=is_private,
        origin_chat_id=(getattr(chat, "id", None) if chat else None),
    )
    if btn is None:
        return None

    return InlineKeyboardMarkup([[btn]])


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
            f"✅ You received {len(players)} starting players\n"
            "🏏 <b>Here is your starter XI.</b>\n\n"
            + "\n".join(lines) + "\n\n"
            f"📊 Your Roster: {len(players)}/{MAX_ROSTER} players\n"
            f"💰 Coins: {debut_coins:,}\n"
            f"💎 Gems: {debut_gems}\n\n"
            "<b>Commands:</b>\n"
            "/claim - Get 1 player + 500 coins (hourly)\n"
            "/myroster - View your players\n"
            "/playerinfo [name] - Player details\n"
            "/daily - Daily reward (24h cooldown)\n"
            "/gspin - Lucky Card Pick (8h cooldown)"
            + referral_reward_text
            + branding
        )
        await update.message.reply_text(
            text, parse_mode="HTML", disable_web_page_preview=True)

        # Follow the account creation with a short, actionable starter guide so
        # new users know which system to try next. Keep the text deliverable even
        # if Telegram rejects the optional Mini App button.
        onboarding_text = _build_post_debut_onboarding_text(len(players))
        onboarding_markup = _build_post_debut_onboarding_markup(update.effective_chat)
        try:
            await update.message.reply_text(
                onboarding_text,
                parse_mode="HTML",
                reply_markup=onboarding_markup,
                disable_web_page_preview=True,
            )
        except Exception:
            if onboarding_markup is None:
                logger.exception("Post-debut onboarding guide failed (non-fatal)")
            else:
                logger.exception(
                    "Post-debut onboarding guide button failed; retrying without markup"
                )
                try:
                    await update.message.reply_text(
                        onboarding_text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    logger.exception("Post-debut onboarding guide retry failed (non-fatal)")

        # If no referral was completed by the link path, ask for a code.
        # Sets a flag so the text-message catcher knows they're a fresh
        # debutant whose next typed line might be a referral code.
        if not referral_completed_now:
            try:
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

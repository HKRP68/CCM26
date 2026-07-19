"""/grant — owner-only subscription activation from Telegram.

Usage:
    /grant Silver <telegram_id>            → activate Silver
    /grant Platinum <telegram_id>          → activate Platinum
    /grant Silver2Platinum <telegram_id>   → upgrade Silver → Platinum

Owner-gated via services.admin_ids.is_owner. On success the granted user is
DM'd exactly as if the grant came from the website (reuses
subscription_service.activation_dm_text + the bot DM bridge), and the owner gets
a confirmation with the granted bundle + new expiry.
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import User
from services.admin_ids import is_owner
from services import subscription_service

logger = logging.getLogger(__name__)

USAGE = (
    "🛡️ <b>/grant</b> — activate a subscription (owner only)\n\n"
    "<code>/grant Silver &lt;telegram_id&gt;</code>\n"
    "<code>/grant Platinum &lt;telegram_id&gt;</code>\n"
    "<code>/grant Silver2Platinum &lt;telegram_id&gt;</code>"
)

# Normalised tier tokens → action.
_ACTIVATE = {"silver": "silver", "platinum": "platinum"}
_UPGRADE_TOKENS = {"silver2platinum", "silver->platinum", "silver-platinum",
                   "s2p", "silvertoplatinum"}


async def grant_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return  # Silent for non-owners.

    if len(ctx.args) < 2:
        await update.message.reply_text(USAGE, parse_mode="HTML")
        return

    token = ctx.args[0].strip().lower()
    raw_id = ctx.args[1].strip()
    try:
        target_tg_id = int(raw_id)
    except ValueError:
        await update.message.reply_text(
            "⚠️ The second argument must be a numeric Telegram ID.\n\n" + USAGE,
            parse_mode="HTML")
        return

    is_upgrade = token in _UPGRADE_TOKENS
    activate_tier = _ACTIVATE.get(token)
    if not is_upgrade and activate_tier is None:
        await update.message.reply_text(
            f"⚠️ Unknown tier '{ctx.args[0]}'.\n\n" + USAGE, parse_mode="HTML")
        return

    session = get_session()
    try:
        user = (session.query(User)
                .filter(User.telegram_id == target_tg_id).first())
        if not user:
            await update.message.reply_text(
                f"⚠️ No user found with Telegram ID <code>{target_tg_id}</code>. "
                "They must /debut first.",
                parse_mode="HTML")
            return

        name = user.username or user.first_name or f"#{user.id}"

        if is_upgrade:
            try:
                result = subscription_service.upgrade(session, user, "platinum")
            except ValueError as ve:
                session.rollback()
                await update.message.reply_text(f"⚠️ {ve}")
                return
            tier = result.get("tier", "platinum")
            from_tier = result.get("from_tier")
        else:
            result = subscription_service.activate(session, user, activate_tier)
            tier = result.get("tier", activate_tier)
            from_tier = None

        session.commit()

        granted = result.get("instant_granted")
        expires_at = result.get("expires_at")

        # DM the granted user (reuse the website activation notification).
        dm_ok = False
        try:
            from bot import _send_bot_dm_blocking
            text = subscription_service.activation_dm_text(
                tier, granted, expires_at=expires_at, upgraded_from=from_tier)
            dm_ok = _send_bot_dm_blocking(user.telegram_id, text)
        except Exception:
            logger.exception("grant DM failed")

        extra = ""
        if granted:
            extra = (f"\n+{granted['coins']:,} coins, +{granted['gems']} gems, "
                     f"+{granted['quest_points']} QP"
                     + (f", packs: {', '.join(granted['packs'])}"
                        if granted.get('packs') else ""))
        head = (f"⬆️ Upgraded {name} {(from_tier or '').title()} → {tier.title()}"
                if is_upgrade else f"⭐ Activated {tier.title()} for {name}")
        dm_line = "\n📬 User notified." if dm_ok else "\n⚠️ Could not DM the user."
        await update.message.reply_text(
            f"{head}{extra}{dm_line}", parse_mode="HTML")

        logger.info("Owner %s granted %s to user %s (tg %s)",
                    update.effective_user.id, tier, user.id, target_tg_id)
    except Exception:
        session.rollback()
        logger.exception("grant handler failed")
        await update.message.reply_text("⚠️ Grant failed. Please try again.")
    finally:
        session.close()

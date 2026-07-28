"""/grant — owner-only subscription activation from Telegram.

Usage:
    /grant Bronze <telegram_id>            → activate Bronze
    /grant Diamond <telegram_id>           → activate Diamond
    /grant Silver2Diamond <telegram_id>    → upgrade Silver → Diamond
    /grant Upgrade Diamond <telegram_id>   → upgrade the user's active tier → Diamond

Tier names come straight from ``config.SUBSCRIPTION_TIERS``, so a tier added
there is grantable here with no code change. Owner-gated via
services.admin_ids.is_owner. On success the granted user is DM'd exactly as if
the grant came from the website (reuses subscription_service.activation_dm_text
+ the bot DM bridge), and the owner gets a confirmation with the granted bundle
+ new expiry.
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import User
from services.admin_ids import is_owner
from services import subscription_service

logger = logging.getLogger(__name__)


def _usage() -> str:
    tiers = " | ".join(t.title() for t in subscription_service.VALID_TIERS)
    return (
        "🛡️ <b>/grant</b> — activate a subscription (owner only)\n\n"
        f"<code>/grant &lt;{tiers}&gt; &lt;telegram_id&gt;</code>\n"
        "<code>/grant Silver2Diamond &lt;telegram_id&gt;</code>\n"
        "<code>/grant Upgrade Diamond &lt;telegram_id&gt;</code>"
    )


# Single-letter shorthands so "s2p", "p2d", "b2s" keep working.
def _tier_aliases() -> dict:
    aliases = {}
    for name in subscription_service.VALID_TIERS:
        aliases[name] = name
        aliases.setdefault(name[0], name)   # b/s/p/d → bronze/silver/platinum/diamond
    return aliases


# Separators accepted between the source and target tier of an upgrade token.
_UPGRADE_SEPARATORS = ("->", "=>", ">", "2", "_to_", "-to-", "to", "-", "_")


def _parse_token(token: str):
    """Parse a /grant tier token.

    Returns ``("activate", tier)`` for a plain tier name, ``("upgrade", tier)``
    for an upgrade token (only the TARGET matters — the source is always the
    user's live tier), or ``None`` if the token isn't recognised.
    """
    aliases = _tier_aliases()
    t = (token or "").strip().lower().replace(" ", "")
    if t in aliases:
        return "activate", aliases[t]
    if t.startswith("upgrade"):
        rest = t[len("upgrade"):].lstrip("-_>")
        if rest in aliases:
            return "upgrade", aliases[rest]
        return None
    for sep in _UPGRADE_SEPARATORS:
        if sep in t:
            src, _, dst = t.partition(sep)
            if src in aliases and dst in aliases:
                return "upgrade", aliases[dst]
    return None


async def grant_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return  # Silent for non-owners.

    if len(ctx.args) < 2:
        await update.message.reply_text(_usage(), parse_mode="HTML")
        return

    # "/grant Upgrade Diamond <id>" spells the action and the target as two
    # separate words; everything else is "<token> <id>".
    args = list(ctx.args)
    if len(args) >= 3 and args[0].strip().lower() in ("upgrade", "up"):
        token, raw_id = f"upgrade{args[1].strip()}", args[2].strip()
    else:
        token, raw_id = args[0].strip(), args[1].strip()

    try:
        target_tg_id = int(raw_id)
    except ValueError:
        await update.message.reply_text(
            "⚠️ The last argument must be a numeric Telegram ID.\n\n" + _usage(),
            parse_mode="HTML")
        return

    parsed = _parse_token(token)
    if parsed is None:
        await update.message.reply_text(
            f"⚠️ Unknown tier '{token}'.\n\n" + _usage(), parse_mode="HTML")
        return
    action, tier_arg = parsed
    is_upgrade = (action == "upgrade")

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
                result = subscription_service.upgrade(session, user, tier_arg)
            except ValueError as ve:
                session.rollback()
                await update.message.reply_text(f"⚠️ {ve}")
                return
            tier = result.get("tier", tier_arg)
            from_tier = result.get("from_tier")
        else:
            result = subscription_service.activate(session, user, tier_arg)
            tier = result.get("tier", tier_arg)
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
                if from_tier else f"⭐ Activated {tier.title()} for {name}")
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

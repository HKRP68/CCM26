"""/membership — your subscription status and the price list.

Everything here is read from ``config.SUBSCRIPTION_TIERS`` and
``services/subscription_service.py``, so a tier added or re-priced there shows
up in this command with no code change.

The command is deliberately one of the doorway commands that keep working while
Rookie mode is on (``services/rookie_gate.py``): it is what a locked-out player
is told to send, and it has to be able to answer them.
"""

import logging
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

from config import SUBSCRIPTION_TIERS
from database import get_session
from models import User
from services import rookie_gate, subscription_service

logger = logging.getLogger(__name__)


def _perk_lines(tier: str) -> list[str]:
    """The perks a tier actually unlocks, as display bullets.

    Built from the tier's own config flags rather than a hand-written list, so
    it stays honest when a perk is moved up or down the ladder.
    """
    cfg = subscription_service.tier_config(tier) or {}
    lines = ["✅ Full access to every command and the Mini App"]

    box_days = cfg.get("mysterybox_cooldown_days") or 0
    if box_days:
        lines.append(f"🎁 Mystery Box every <b>{box_days}</b> day"
                     f"{'s' if box_days != 1 else ''} (/cmumysterybox)")
    if cfg.get("premium_commands"):
        lines.append("⭐ Premium commands: /autobuild, /wpmbot")
    if cfg.get("autoplay"):
        lines.append("▶️ Mini App Autoplay")
    reduction = cfg.get("cooldown_reduction_min_per_hour") or 0
    if reduction:
        lines.append(f"⏱️ <b>{reduction} min/hour</b> off every command cooldown")
    discount = cfg.get("market_discount_pct") or 0
    if discount:
        lines.append(f"🏷️ <b>{discount}%</b> off every Player Market card")
    if cfg.get("weekly_card"):
        lines.append("🏆 Weekly guaranteed 85+ card (/cmuweekly)")
    chests = cfg.get("coin_chests") or {}
    if chests:
        lines.append(f"🪙 <b>{chests.get('count', 0)}</b> coin chests every "
                     f"<b>{chests.get('cooldown_days', 0)}</b> days (/cmuchest)")
    multiplier = cfg.get("daily_login_multiplier") or 1
    if multiplier > 1:
        lines.append(f"📅 <b>{multiplier}×</b> Mini App daily login reward")
    return lines


def _plan_lines(names=None) -> list[str]:
    """One line per tier — label, price and its richest perk — cheapest first.

    ``names`` limits the list (used to show only the tiers a member can step
    up into); None lists the whole ladder.
    """
    lines = []
    for name, cfg in SUBSCRIPTION_TIERS.items():
        if names is not None and name not in names:
            continue
        label = cfg.get("label", name.title())
        # The last bullet is the richest perk the tier adds — its selling point.
        headline = _perk_lines(name)[-1]
        lines.append(f"• <b>{label}</b> — ₹{cfg.get('price_inr')}/mo — {headline}")
    return lines


def _status_block(user) -> list[str]:
    """The "your membership" section for a user (or a free/no-account user)."""
    tier = subscription_service.get_tier(user)
    if tier == "none":
        held = (getattr(user, "subscription_tier", None) or "none").lower()
        if user is not None and held in SUBSCRIPTION_TIERS:
            # They had a tier that has since lapsed — say so plainly, since
            # "Free" alone reads like the grant never happened.
            return ["📋 Your membership: <b>Expired</b> "
                    f"({subscription_service.tier_label(held)})"]
        return ["📋 Your membership: <b>Free</b> (no active plan)"]

    lines = [f"📋 Your membership: <b>{subscription_service.tier_label(tier)}</b>"]
    expires = getattr(user, "subscription_expires_at", None)
    if expires:
        days_left = max(0, (expires - datetime.utcnow()).days)
        lines.append(f"⏳ Active until <b>{expires:%d %b %Y}</b> "
                     f"({days_left} day{'s' if days_left != 1 else ''} left)")
    lines.append("")
    lines.append("<b>What you get:</b>")
    lines.extend(_perk_lines(tier))
    return lines


def build_membership_text(user, *, rookie_mode: bool) -> str:
    """Render the /membership reply. Pure — unit-testable without Telegram."""
    parts = ["💳 <b>Membership</b>", ""]
    parts.extend(_status_block(user))
    parts.append("")

    tier = subscription_service.get_tier(user)
    if rookie_mode and tier == "none":
        parts.append(
            f"🔒 The bot is <b>members-only</b> right now — "
            f"{rookie_gate.rookie_price_label()} is the cheapest way in, and "
            "every higher plan includes that access.")
        parts.append("")

    upgrades = subscription_service.upgrade_targets(tier)
    if upgrades:
        parts.append("<b>Plans</b>" if tier == "none" else "<b>Upgrade to</b>")
        parts.extend(_plan_lines(None if tier == "none" else upgrades))
        parts.append("")
        parts.append("<i>Ask an admin to activate or upgrade your membership.</i>")
    else:
        parts.append("<i>You're on the top plan — thanks for supporting CMU! 🏏</i>")

    return "\n".join(parts).strip()


async def membership_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the sender their membership status, perks and the price list."""
    tg_user = update.effective_user
    session = get_session()
    try:
        user = (session.query(User)
                .filter(User.telegram_id == tg_user.id).first())
        rookie_mode = rookie_gate.is_rookie_mode_active()
        text = build_membership_text(user, rookie_mode=rookie_mode)
        if user is None:
            text += "\n\n<i>You don't have an account yet — send /debut first.</i>"
    except Exception:
        logger.exception("/membership failed")
        await update.message.reply_text(
            "⚠️ Could not read your membership right now. Please try again.")
        return
    finally:
        session.close()

    await update.message.reply_text(text, parse_mode="HTML",
                                    disable_web_page_preview=True)

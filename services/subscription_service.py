"""Paid subscription tiers — single source of truth.

Subscriptions are granted manually by an admin from the website (there is no
self-serve payment). A user's ``subscription_tier`` is one of ``none``,
``silver`` or ``platinum`` and stays active until ``subscription_expires_at``.
An expired tier is treated as ``none`` everywhere WITHOUT needing a background
job — every access check goes through :func:`get_tier`, which compares the
expiry to ``utcnow()`` on read.

All tier perks (instant signup rewards, mystery-box cadence, cooldown
reduction, market discount, premium commands, autoplay) are configured in
``config.SUBSCRIPTION_TIERS`` and read through the helpers here so callers never
hard-code tier logic.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from config import SUBSCRIPTION_TIERS

logger = logging.getLogger(__name__)

VALID_TIERS = tuple(SUBSCRIPTION_TIERS.keys())  # ("silver", "platinum")


# ── Tier state ──────────────────────────────────────────────────────

def get_tier(user) -> str:
    """Return the user's ACTIVE tier: ``none`` if unset, unknown or expired."""
    if user is None:
        return "none"
    tier = (getattr(user, "subscription_tier", None) or "none").lower()
    if tier not in SUBSCRIPTION_TIERS:
        return "none"
    expires = getattr(user, "subscription_expires_at", None)
    if expires is not None and expires < datetime.utcnow():
        return "none"
    return tier


def tier_config(tier: str) -> dict | None:
    """Return the config dict for a tier name, or None for 'none'/unknown."""
    return SUBSCRIPTION_TIERS.get((tier or "").lower())


def is_subscribed(user) -> bool:
    """True if the user has any active paid tier."""
    return get_tier(user) != "none"


def is_platinum(user) -> bool:
    """True if the user's active tier is Platinum."""
    return get_tier(user) == "platinum"


def has_premium_commands(user) -> bool:
    """True if the active tier unlocks /autobuild and /wpmbot."""
    cfg = tier_config(get_tier(user))
    return bool(cfg and cfg.get("premium_commands"))


def has_autoplay(user) -> bool:
    """True if the active tier unlocks the Mini App Autoplay toggle."""
    cfg = tier_config(get_tier(user))
    return bool(cfg and cfg.get("autoplay"))


def has_weekly_card(user) -> bool:
    """True if the active tier grants the /cmuweekly card."""
    cfg = tier_config(get_tier(user))
    return bool(cfg and cfg.get("weekly_card"))


def coin_chest_config(user) -> dict | None:
    """Return the active tier's coin-chest config (/cmuchest), or None."""
    cfg = tier_config(get_tier(user))
    return cfg.get("coin_chests") if cfg else None


def mysterybox_cooldown_seconds(user) -> int | None:
    """Seconds between /cmumysterybox opens for this user's tier, or None."""
    cfg = tier_config(get_tier(user))
    if not cfg:
        return None
    return int(cfg.get("mysterybox_cooldown_days", 0)) * 86400


# ── Perks applied to existing features ──────────────────────────────

def cooldown_seconds(user, base_seconds: int) -> int:
    """Apply the tier's cooldown reduction to a base command cooldown.

    "N minutes less cooldown per hour" → multiply by (1 - N/60). Never returns
    below zero. Free users get the base value unchanged.
    """
    cfg = tier_config(get_tier(user))
    if not cfg:
        return base_seconds
    reduction = cfg.get("cooldown_reduction_min_per_hour", 0)
    if not reduction:
        return base_seconds
    factor = max(0.0, 1.0 - (reduction / 60.0))
    return int(base_seconds * factor)


def market_discount_pct(user) -> int:
    """Percent discount the active tier applies to player purchases (0 if none)."""
    cfg = tier_config(get_tier(user))
    return int(cfg.get("market_discount_pct", 0)) if cfg else 0


def discounted_price(user, price: int) -> int:
    """Return ``price`` after the tier's market discount (rounded down)."""
    pct = market_discount_pct(user)
    if pct <= 0:
        return price
    return price * (100 - pct) // 100


def market_price(user, base_price: int, final_price: int) -> int:
    """Player Market price for this user. The market's discount is a Platinum
    perk: Platinum pays the discounted ``final_price``; everyone else pays the
    full ``base_price``."""
    return final_price if is_platinum(user) else base_price


# ── Messaging ───────────────────────────────────────────────────────

def premium_required_message(feature: str = "This feature") -> str:
    return (
        f"🔒 <b>{feature} is a premium feature.</b>\n\n"
        "Upgrade to <b>🥈 Silver</b> (₹59/mo) or <b>🏆 Platinum</b> (₹99/mo) "
        "to unlock it, plus Mystery Boxes, faster cooldowns and more.\n\n"
        "Ask an admin to activate your subscription."
    )


# ── Admin activation / deactivation ─────────────────────────────────

def activate(session, user, tier: str, *, grant_instant: bool = True) -> dict:
    """Activate (or extend) a paid tier for ``user``.

    Sets tier + a fresh 30-day expiry. Instant signup rewards are granted only
    when this is a NEW activation of that tier (so repeated admin clicks, or a
    renewal of the same active tier, do not re-grant). Caller commits.
    """
    tier = (tier or "").lower()
    cfg = SUBSCRIPTION_TIERS.get(tier)
    if cfg is None:
        raise ValueError(f"Unknown subscription tier: {tier!r}")

    already_active_same_tier = (get_tier(user) == tier)

    now = datetime.utcnow()
    user.subscription_tier = tier
    user.subscription_activated_at = now
    # Renewing the SAME active tier stacks onto the remaining paid time instead
    # of erasing it; a new/expired/different tier starts a fresh window from now.
    duration = timedelta(days=int(cfg.get("duration_days", 30)))
    if (already_active_same_tier and user.subscription_expires_at
            and user.subscription_expires_at > now):
        user.subscription_expires_at += duration
    else:
        user.subscription_expires_at = now + duration

    granted = None
    if grant_instant and not already_active_same_tier:
        granted = grant_instant_rewards(session, user, tier)

    return {"tier": tier, "expires_at": user.subscription_expires_at,
            "instant_granted": granted}


def deactivate(session, user) -> None:
    """Clear a user's subscription immediately. Caller commits."""
    user.subscription_tier = "none"
    user.subscription_expires_at = None


def grant_instant_rewards(session, user, tier: str) -> dict:
    """Credit the tier's one-time instant rewards. Caller commits."""
    cfg = SUBSCRIPTION_TIERS.get((tier or "").lower())
    instant = (cfg or {}).get("instant") or {}
    coins = int(instant.get("coins", 0))
    gems = int(instant.get("gems", 0))
    qp = int(instant.get("quest_points", 0))

    if coins:
        user.total_coins = (user.total_coins or 0) + coins
    if gems:
        user.total_gems = (user.total_gems or 0) + gems
    if qp:
        user.quest_points = (user.quest_points or 0) + qp

    granted_packs = _grant_named_packs(session, user, instant.get("packs") or [])

    try:
        from services.activity_service import log_activity
        log_activity(session, user.id, "subscription",
                     f"Activated {tier}: +{coins} coins, +{gems} gems, "
                     f"+{qp} QP, packs: {', '.join(granted_packs) or 'none'}",
                     coins_change=coins, gems_change=gems)
    except Exception:
        logger.exception("log_activity failed during subscription grant")

    return {"coins": coins, "gems": gems, "quest_points": qp,
            "packs": granted_packs}


def _grant_named_packs(session, user, pack_names) -> list[str]:
    """Grant packs by name into the user's inventory. Missing names are skipped
    gracefully (the pack catalogue is admin-configured and may not include the
    exact name)."""
    if not pack_names:
        return []
    granted: list[str] = []
    try:
        from services import pack_service
        catalogue = {p.name.strip().lower(): p for p in pack_service.list_packs(session, only_active=True)}
        for name in pack_names:
            pack = catalogue.get(str(name).strip().lower())
            if pack is None:
                logger.warning("Subscription pack %r not found in catalogue", name)
                continue
            pack_service.grant_pack(session, user.id, pack.id, source="subscription")
            granted.append(pack.name)
    except Exception:
        logger.exception("Failed granting subscription packs")
    return granted

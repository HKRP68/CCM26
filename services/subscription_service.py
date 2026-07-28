"""Paid subscription tiers — single source of truth.

Subscriptions are granted manually by an admin from the website (there is no
self-serve payment). A user's ``subscription_tier`` is one of ``none``,
``bronze``, ``silver``, ``platinum`` or ``diamond`` and stays active until
``subscription_expires_at``. An expired tier is treated as ``none`` everywhere
WITHOUT needing a background job — every access check goes through
:func:`get_tier`, which compares the expiry to ``utcnow()`` on read.

All tier perks (instant signup rewards, mystery-box cadence, cooldown
reduction, market discount, daily-login multiplier, premium commands, autoplay)
are configured in ``config.SUBSCRIPTION_TIERS`` and read through the helpers
here so callers never hard-code tier logic.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from config import SUBSCRIPTION_TIERS

logger = logging.getLogger(__name__)

# ("bronze", "silver", "platinum", "diamond") — cheapest → richest.
VALID_TIERS = tuple(SUBSCRIPTION_TIERS.keys())


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


def tier_rank(tier: str) -> int:
    """Ordinal rank of a tier for comparisons; higher = better, ``none`` = 0.

    Ranks follow the declaration order of ``SUBSCRIPTION_TIERS`` (bronze=1,
    silver=2, platinum=3, diamond=4), so callers never hard-code which tier
    outranks which.
    """
    order = list(SUBSCRIPTION_TIERS.keys())
    tier = (tier or "").lower()
    return order.index(tier) + 1 if tier in order else 0


def tier_label(tier: str) -> str:
    """Display label for a tier ('🏆 Platinum'), or 'Free' for none/unknown."""
    cfg = tier_config(tier)
    if cfg:
        return cfg.get("label", (tier or "").title())
    return "Free"


def can_upgrade(user, target_tier: str) -> bool:
    """True if ``target_tier`` is strictly higher than the user's active tier."""
    return tier_rank(target_tier) > tier_rank(get_tier(user))


def upgrade_targets(tier: str) -> list[str]:
    """Every tier a member on ``tier`` can step UP into, cheapest first.

    Drives the admin website's upgrade buttons and /grant, so a new tier added
    to ``SUBSCRIPTION_TIERS`` becomes upgradable everywhere with no code change.
    """
    rank = tier_rank(tier)
    return [name for name in SUBSCRIPTION_TIERS if tier_rank(name) > rank]


def is_subscribed(user) -> bool:
    """True if the user has any active paid tier."""
    return get_tier(user) != "none"


def is_platinum(user) -> bool:
    """True if the user's active tier is Platinum.

    Kept for callers that genuinely mean "exactly Platinum". Perk checks should
    use the perk helpers (e.g. :func:`market_discount_pct`) instead, so higher
    tiers like Diamond inherit the perk automatically.
    """
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


def daily_login_multiplier(user) -> int:
    """Multiplier the active tier applies to the Mini App daily login reward.

    ``1`` for free users and every tier without the perk; Diamond doubles it.
    """
    cfg = tier_config(get_tier(user))
    try:
        return max(1, int((cfg or {}).get("daily_login_multiplier", 1) or 1))
    except (TypeError, ValueError):
        return 1


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


def market_price(user, base_price: int, final_price: int | None = None) -> int:
    """Player Market price for this user.

    The discount is a paid perk driven by the tier's ``market_discount_pct``
    (Platinum 5%, Diamond 10%): it comes off ``base_price``, and free/Bronze/
    Silver members pay the full ``base_price``. ``final_price`` — the market's
    own precomputed 5%-off column — caps the result, so a discounted member
    never pays more than the price the market itself advertises.
    """
    price = discounted_price(user, base_price)
    if final_price is not None and market_discount_pct(user) > 0:
        return min(price, final_price)
    return price


# ── Messaging ───────────────────────────────────────────────────────

def activation_dm_text(tier: str, granted: dict | None, *,
                       expires_at=None, upgraded_from: str | None = None) -> str:
    """Build the bot DM a user receives when an admin activates/upgrades their
    subscription. ``granted`` is the instant/top-up bundle returned by
    :func:`activate`/:func:`upgrade` (or None on a same-tier renewal). Pure
    string builder — safe to unit-test without a DB or bot.
    """
    cfg = tier_config(tier) or {}
    label = cfg.get("label", (tier or "").title() or "Subscription")

    if upgraded_from:
        from_label = (tier_config(upgraded_from) or {}).get(
            "label", (upgraded_from or "").title())
        header = f"⬆️ <b>Upgraded to {label}!</b>\n<i>from {from_label}</i>"
    else:
        header = f"🎉 <b>{label} activated!</b>"

    lines = [header, ""]

    if granted:
        reward_bits = []
        if granted.get("coins"):
            reward_bits.append(f"💰 <b>{int(granted['coins']):,}</b> coins")
        if granted.get("gems"):
            reward_bits.append(f"💎 <b>{int(granted['gems']):,}</b> gems")
        if granted.get("quest_points"):
            reward_bits.append(f"🎯 <b>{int(granted['quest_points']):,}</b> QP")
        if reward_bits:
            lines.append("You received " + ", ".join(reward_bits) + ".")
        packs = granted.get("packs") or []
        if packs:
            lines.append("📦 Packs: <b>" + ", ".join(packs) + "</b>")
        lines.append("")

    if expires_at is not None:
        try:
            lines.append(f"⏳ Active until <b>{expires_at:%d %b %Y}</b>.")
        except Exception:
            pass

    lines.append("Enjoy your perks — thanks for supporting CMU! 🏏")
    return "\n".join(lines).strip()


def tier_price_list(min_tier: str | None = None) -> str:
    """Render the tier list used by upsell messages — 🥉 Bronze (₹19/mo),
    🥈 Silver (₹59/mo), … — from the config, so a new tier shows up everywhere
    automatically. ``min_tier`` drops every tier ranked below it (use it when a
    perk starts partway up the ladder).
    """
    floor = tier_rank(min_tier) if min_tier else 0
    bits = [f"<b>{cfg.get('label', name.title())}</b> (₹{cfg.get('price_inr')}/mo)"
            for name, cfg in SUBSCRIPTION_TIERS.items()
            if tier_rank(name) >= floor]
    if len(bits) > 1:
        return ", ".join(bits[:-1]) + " or " + bits[-1]
    return bits[0] if bits else ""


def premium_required_message(feature: str = "This feature",
                             min_tier: str | None = None) -> str:
    """Upsell shown when a free (or too-low) member hits a paid feature.

    ``min_tier`` names the cheapest tier that unlocks ``feature`` so the message
    only advertises tiers that actually include it.
    """
    return (
        f"🔒 <b>{feature} is a premium feature.</b>\n\n"
        f"Upgrade to {tier_price_list(min_tier)} to unlock it, plus Mystery "
        "Boxes, faster cooldowns and more.\n\n"
        "Ask an admin to activate your subscription."
    )


# ── Admin activation / deactivation ─────────────────────────────────

def activate(session, user, tier: str, *, grant_instant: bool = True) -> dict:
    """Activate (or extend) a paid tier for ``user``.

    Sets tier + a fresh 30-day expiry. Instant signup rewards are granted only
    when this is a NEW activation of that tier (so repeated admin clicks, or a
    renewal of the same active tier, do not re-grant). Caller commits.

    Subscriptions never auto-renew: an expired tier simply lapses (see
    :func:`get_tier`, which treats it as ``none`` on read). A member who
    subscribes again after lapsing is therefore treated exactly like a
    first-time member — a fresh paid window, the instant signup rewards
    re-granted, and their subscriber recurring-drop cooldowns reset (see
    :func:`_reset_subscriber_cooldowns`) so the first Mystery Box / weekly card /
    coin chest is available immediately rather than inheriting a stale timer.
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
    if not already_active_same_tier:
        # Fresh subscription (first-time OR re-subscribing after a lapse): wipe
        # any leftover subscriber cooldowns so they start clean like a new member.
        _reset_subscriber_cooldowns(session, user)
        if grant_instant:
            granted = grant_instant_rewards(session, user, tier)

    return {"tier": tier, "expires_at": user.subscription_expires_at,
            "instant_granted": granted}


def upgrade(session, user, target_tier: str, *, grant_delta: bool = True) -> dict:
    """Upgrade an active lower tier to a higher one (e.g. Silver → Platinum).

    Unlike :func:`activate`, an upgrade credits a small top-up bundle (the
    target tier's ``upgrade_from`` config) rather than the full Platinum
    instant bundle, and keeps whatever paid time is left on the clock — so a
    Silver member stepping up gets a modest boost plus the new signature pack,
    not a second subscription. Caller commits.

    Falls back to a plain :func:`activate` when the user has no active tier
    (there is nothing already granted to subtract). Raises ``ValueError`` if
    ``target_tier`` is unknown or not strictly higher than the current tier.
    """
    target = (target_tier or "").lower()
    cfg = SUBSCRIPTION_TIERS.get(target)
    if cfg is None:
        raise ValueError(f"Unknown subscription tier: {target_tier!r}")

    current = get_tier(user)
    if current == "none":
        # No active tier to subtract from — this is a fresh activation.
        return activate(session, user, target)

    if tier_rank(target) <= tier_rank(current):
        raise ValueError(
            f"Cannot upgrade {current!r} to {target!r}: not a higher tier.")

    now = datetime.utcnow()
    user.subscription_tier = target
    user.subscription_activated_at = now
    # Keep the remaining paid time; only open a fresh window if none is left.
    if not (user.subscription_expires_at and user.subscription_expires_at > now):
        user.subscription_expires_at = now + timedelta(
            days=int(cfg.get("duration_days", 30)))

    granted = None
    if grant_delta:
        granted = grant_upgrade_rewards(session, user, current, target)

    return {"tier": target, "from_tier": current,
            "expires_at": user.subscription_expires_at,
            "instant_granted": granted}


def deactivate(session, user) -> None:
    """End a user's subscription immediately — no auto-renewal. Caller commits.

    Clears the tier and expiry and wipes the subscriber recurring-drop cooldowns
    so that if they ever subscribe again they are treated as a first-time member
    (see :func:`activate`)."""
    user.subscription_tier = "none"
    user.subscription_expires_at = None
    _reset_subscriber_cooldowns(session, user)


def _reset_subscriber_cooldowns(session, user) -> None:
    """Wipe the subscriber-only recurring-drop cooldowns so a NEW subscription
    starts clean: the first Mystery Box (/cmumysterybox), weekly card
    (/cmuweekly) and coin chests (/cmuchest) are all immediately available,
    exactly as for a first-time member.

    Only called on a fresh (re)activation or a deactivation — never on a
    same-tier renewal or an upgrade, where paid service was continuous and the
    running cooldowns should be preserved. Safe to call with ``session`` None
    (used by unit tests that don't touch a DB)."""
    if session is None or user is None:
        return
    try:
        from models import UserStats
        stats = (session.query(UserStats)
                 .filter(UserStats.user_id == user.id).first())
        if stats is None:
            return
        stats.last_mysterybox = None
        stats.last_weekly = None
        stats.last_coinchest = None
        stats.coinchest_used = 0
    except Exception:
        logger.exception("Failed resetting subscriber cooldowns")


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


def instant_delta(from_tier: str, to_tier: str) -> dict:
    """Raw instant DIFFERENCE between two tiers: the higher tier's instant
    bundle minus what the lower tier already granted.

    Currencies clamp at zero (an upgrade never claws back). Packs already
    granted by the lower tier are dropped, so only the NEW packs remain. Used
    only as the fallback for :func:`upgrade_rewards` when a tier has no explicit
    ``upgrade_from`` bundle configured.
    """
    a = (SUBSCRIPTION_TIERS.get((from_tier or "").lower()) or {}).get("instant") or {}
    b = (SUBSCRIPTION_TIERS.get((to_tier or "").lower()) or {}).get("instant") or {}
    delta = {key: max(0, int(b.get(key, 0)) - int(a.get(key, 0)))
             for key in ("coins", "gems", "quest_points")}
    already = {str(p).strip().lower() for p in (a.get("packs") or [])}
    delta["packs"] = [p for p in (b.get("packs") or [])
                      if str(p).strip().lower() not in already]
    return delta


def upgrade_rewards(from_tier: str, to_tier: str) -> dict:
    """Rewards granted for a ``from_tier`` → ``to_tier`` upgrade.

    Prefers the target tier's explicit, admin-tuned ``upgrade_from[from_tier]``
    top-up bundle (deliberately smaller than a full activation). Falls back to
    the raw instant difference (see :func:`instant_delta`) only when no explicit
    bundle is configured.
    """
    to_cfg = SUBSCRIPTION_TIERS.get((to_tier or "").lower()) or {}
    explicit = (to_cfg.get("upgrade_from") or {}).get((from_tier or "").lower())
    if explicit is None:
        return instant_delta(from_tier, to_tier)
    return {"coins": int(explicit.get("coins", 0)),
            "gems": int(explicit.get("gems", 0)),
            "quest_points": int(explicit.get("quest_points", 0)),
            "packs": list(explicit.get("packs") or [])}


def grant_upgrade_rewards(session, user, from_tier: str, to_tier: str) -> dict:
    """Credit the top-up rewards for a ``from_tier`` → ``to_tier`` upgrade
    (see :func:`upgrade_rewards`). Caller commits."""
    delta = upgrade_rewards(from_tier, to_tier)
    coins, gems, qp = delta["coins"], delta["gems"], delta["quest_points"]

    if coins:
        user.total_coins = (user.total_coins or 0) + coins
    if gems:
        user.total_gems = (user.total_gems or 0) + gems
    if qp:
        user.quest_points = (user.quest_points or 0) + qp

    granted_packs = _grant_named_packs(session, user, delta["packs"])

    try:
        from services.activity_service import log_activity
        log_activity(session, user.id, "subscription",
                     f"Upgraded {from_tier} → {to_tier}: +{coins} coins, "
                     f"+{gems} gems, +{qp} QP, packs: "
                     f"{', '.join(granted_packs) or 'none'}",
                     coins_change=coins, gems_change=gems)
    except Exception:
        logger.exception("log_activity failed during subscription upgrade")

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

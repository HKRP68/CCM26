"""Gem rebate paid out when a user signs an elite card.

Buying a player rated above 95 pays back ``config.GEM_BONUS_BPS`` (0.1%) of the
coins spent, as gems — a 97 OVR at 4,450,000 🪙 hands back 4,450 💎.

Every buy path routes through :func:`award_buy_gem_bonus` (bot ``/buypl``, the
bot player market, and both Mini App buy endpoints) so the rebate, its audit
row and its wording can never drift between them. The rate itself lives in
``config.get_buy_gem_bonus``; this module only applies it.
"""

import logging

from config import get_buy_gem_bonus

logger = logging.getLogger(__name__)


def award_buy_gem_bonus(session, user, player, price_paid, *, source="buy"):
    """Credit the buyer's gem rebate for this purchase; return the gems paid.

    Returns 0 (and touches nothing) when the card isn't elite enough. Call it
    before ``session.commit()`` — the credit and its ActivityLog row ride on
    the caller's transaction, so a rolled-back buy never leaves gems behind.
    """
    gems = get_buy_gem_bonus(getattr(player, "rating", 0), price_paid)
    if gems <= 0:
        return 0

    user.total_gems = (user.total_gems or 0) + gems
    try:
        from services.activity_service import log_activity
        log_activity(session, user.id, "buy_gem_bonus",
                     f"Elite signing bonus for {player.name} "
                     f"({player.rating} OVR) — +{gems:,} gems [{source}]",
                     gems_change=gems,
                     player_name=player.name, player_rating=player.rating)
    except Exception:
        logger.exception("buy gem bonus activity log failed (non-fatal)")
    return gems


def bonus_line(gems, *, prefix="\n") -> str:
    """One HTML line for a purchase message, or "" when no bonus was paid."""
    if gems <= 0:
        return ""
    return f"{prefix}💎 Elite Signing Bonus: <b>+{gems:,}</b> gems"


def teaser_line(rating, price=None, *, prefix="\n") -> str:
    """The bonus a buyer *would* earn, for a card that hasn't been bought yet."""
    gems = get_buy_gem_bonus(rating, price)
    if gems <= 0:
        return ""
    return f"{prefix}💎 Elite Signing Bonus: <b>+{gems:,}</b> gems on purchase"

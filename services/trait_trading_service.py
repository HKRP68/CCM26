"""Trait resale (/selltrait) and trait-for-trait swaps (/tradetrait).

Both operate on the **inventory** only — a trait a player is wearing is not for
sale and not for trade. ``/removetrait`` already unequips for free and returns
the trait at its current level, so nothing is lost by that rule, and it keeps a
squad that is out on the field from changing under a market transaction.

Pricing lives in ``config`` (``trait_sell_value`` / ``trait_trade_fee``) and is
derived from what a trait actually cost to build — the Lv.1 shop price plus every
upgrade paid on the way up:

    Level        1      2      3      4      5
    buy        150    350    750  1,550  3,050   gems invested
    floor      120    320    720  1,520  3,020   cheapest it can be acquired for
    sell       119    319    719  1,519  3,019   always just under the floor
    trade fee   15     16     18     22     29   each side

Resale is deliberately close to cost — it returns 50% more than the table this
economy shipped with — but it is pinned below the *floor*: the cheapest gems a
level can possibly have cost, which is the Lv.1 price at the shop's deepest
discount plus the (never-discounted) upgrades. Selling is therefore a small loss
however cheaply the trait was bought, and the shop can never be farmed by buying
a discounted trait and selling it straight back.

Those are the Common-tier numbers. A trait's rarity multiplies its Lv.1 price
(config.TRAIT_RARITIES), and resale and the swap fee scale with it, so an Elite
trait is never sold or swapped at a Common's valuation.

The swap mirrors ``services.trading_service``: both traits must be the same
level AND the same rarity (the trait analogue of the same-OVR player rule), so
one fee figure covers both captains, and the gems leave the economy rather than
moving between them. A captain who cannot cover the fee gets no half-done trade
— nothing changes hands unless both sides can pay.

No Telegram and no globals here, so every rule below is unit-testable directly.
"""

import logging

from config import (
    trait_buy_value, trait_sell_value, trait_trade_fee, trait_rarity_label,
)
from models import Trait, TraitInventory
from services.activity_service import log_activity

logger = logging.getLogger(__name__)


def _rarity(trait):
    """Rarity of a trait row, tolerant of rows that predate the column."""
    from services.trait_service import trait_rarity_of
    return trait_rarity_of(trait)


def _label(trait, level):
    if not trait:
        return f"Trait Lv.{level}"
    return f"{trait.emoji or '✨'} {trait.name} Lv.{level}"


def _user_label(user):
    if not user:
        return "A player"
    return f"@{user.username}" if getattr(user, "username", None) else (
        getattr(user, "first_name", None) or "A player")


def _owned_inventory_trait(session, user_id, inventory_id):
    """``(inventory_row, trait_row)`` for a trait this user actually holds."""
    inv = (session.query(TraitInventory)
           .filter(TraitInventory.id == inventory_id,
                   TraitInventory.user_id == user_id).first())
    if not inv:
        return None, None
    return inv, session.query(Trait).get(inv.trait_id)


def list_inventory(session, user_id, level=None):
    """This user's unequipped traits, newest last. ``level`` filters to one tier."""
    q = (session.query(TraitInventory, Trait)
         .join(Trait, TraitInventory.trait_id == Trait.id)
         .filter(TraitInventory.user_id == user_id))
    if level is not None:
        q = q.filter(TraitInventory.level == int(level))
    return q.order_by(TraitInventory.level.desc(), TraitInventory.id).all()


# ════════════════════════════════════════════════════════════════════
# /selltrait — cash an inventory trait in for gems
# ════════════════════════════════════════════════════════════════════

def sell_inventory_trait(session, user, inventory_id):
    """Sell one inventory trait for gems.

    Returns ``(ok, message, detail)`` where ``detail`` carries the numbers the
    handler shows: ``{"gems", "buy_value", "level", "label", "balance"}``.
    """
    inv, trait = _owned_inventory_trait(session, user.id, inventory_id)
    if not inv:
        return False, "That trait isn't in your inventory any more.", None
    if not trait:
        return False, "Trait data is missing — please contact support.", None

    level = int(inv.level or 1)
    rarity = _rarity(trait)
    label = _label(trait, level)
    gems = trait_sell_value(level, rarity)

    session.delete(inv)
    user.total_gems = (user.total_gems or 0) + gems
    log_activity(session, user.id, "trait_sell",
                 f"Sold {trait.name} Lv.{level} for {gems} gems",
                 gems_change=gems)
    session.flush()

    return True, f"Sold {label} for {gems:,} 💎.", {
        "gems": gems,
        "buy_value": trait_buy_value(level, rarity),
        "level": level,
        "rarity": rarity,
        "label": label,
        "balance": user.total_gems,
    }


# ════════════════════════════════════════════════════════════════════
# /tradetrait — swap an inventory trait with another captain
# ════════════════════════════════════════════════════════════════════

def validate_trait_swap(session, user1, user2, inv1_id, inv2_id):
    """Check a proposed swap. Returns ``(inv1, t1, inv2, t2, error)``."""
    inv1, t1 = _owned_inventory_trait(session, user1.id, inv1_id)
    inv2, t2 = _owned_inventory_trait(session, user2.id, inv2_id)
    if not inv1 or not inv2:
        return None, None, None, None, (
            "Trade cancelled because one of the traits is no longer in "
            "inventory (it may have been applied, sold or traded).")
    if not t1 or not t2:
        return None, None, None, None, "Trait data is missing — trade cancelled."
    if int(inv1.level or 1) != int(inv2.level or 1):
        return None, None, None, None, (
            "Trade cancelled because both traits no longer have the same level.")
    if _rarity(t1) != _rarity(t2):
        # Same level is not the same value once rarity exists: a Lv.2 Elite cost
        # 1,400 gems to build and a Lv.2 Common 350. Swapping across tiers would
        # be the trait economy's free-money trade.
        return None, None, None, None, (
            f"Trade cancelled — {trait_rarity_label(_rarity(t1))} and "
            f"{trait_rarity_label(_rarity(t2))} traits can't be swapped for each "
            f"other. Both traits must be the same rarity as well as the same "
            f"level.")
    if inv1.trait_id == inv2.trait_id:
        return None, None, None, None, (
            "Those are the same trait at the same level — swapping them would "
            "change nothing. Pick a different one.")
    return inv1, t1, inv2, t2, None


def cannot_afford_fee(user1, user2, fee):
    """Message naming whoever can't cover the swap fee, or ``None``."""
    if fee <= 0:
        return None
    broke = [u for u in (user1, user2) if (u.total_gems or 0) < fee]
    if not broke:
        return None
    who = " and ".join(_user_label(u) for u in broke)
    return (f"Trade cancelled — {who} can't cover the {fee:,} 💎 trade fee for "
            f"a trait at this level.")


def quote_trait_swap(session, user1, user2, inv1_id, inv2_id):
    """Price a proposed swap without performing it.

    Returns ``{"ok": True, "fee", "level", "label1", "label2"}`` or
    ``{"ok": False, "message"}`` — used to build the confirmation card.
    """
    inv1, t1, inv2, t2, error = validate_trait_swap(
        session, user1, user2, inv1_id, inv2_id)
    if error:
        return {"ok": False, "message": error}
    level = int(inv1.level or 1)
    rarity = _rarity(t1)
    fee = trait_trade_fee(level, rarity)
    short = cannot_afford_fee(user1, user2, fee)
    if short:
        return {"ok": False, "message": short, "fee": fee}
    return {"ok": True, "fee": fee, "level": level, "rarity": rarity,
            "label1": _label(t1, level), "label2": _label(t2, level)}


def complete_trait_swap(session, user1, user2, inv1_id, inv2_id):
    """Swap the two inventory traits and charge both sides the fee.

    Re-validates and re-prices at the moment of the swap — a level can change
    (an upgrade) and gems can be spent while the confirmation sits on screen.
    Returns ``{"success", "message", ...}``; on failure nothing has moved.
    """
    inv1, t1, inv2, t2, error = validate_trait_swap(
        session, user1, user2, inv1_id, inv2_id)
    if error:
        return {"success": False, "message": error}

    level = int(inv1.level or 1)
    rarity = _rarity(t1)
    fee = trait_trade_fee(level, rarity)
    short = cannot_afford_fee(user1, user2, fee)
    if short:
        return {"success": False, "message": short, "fee": fee}

    label1, label2 = _label(t1, level), _label(t2, level)
    if fee > 0:
        for side in (user1, user2):
            side.total_gems = (side.total_gems or 0) - fee

    # The rows change hands exactly as a traded player's roster row does.
    inv1.user_id, inv2.user_id = user2.id, user1.id

    fee_note = f" (fee {fee} gems)" if fee else ""
    log_activity(session, user1.id, "trait_trade",
                 f"Received {t2.name} Lv.{level}; gave {t1.name} Lv.{level}{fee_note}",
                 gems_change=-fee if fee else 0)
    log_activity(session, user2.id, "trait_trade",
                 f"Received {t1.name} Lv.{level}; gave {t2.name} Lv.{level}{fee_note}",
                 gems_change=-fee if fee else 0)
    session.flush()
    logger.info("Trait trade completed: %s gave %s, %s gave %s (level %s, fee %s)",
                user1.id, t1.name, user2.id, t2.name, level, fee)

    return {
        "success": True,
        "fee": fee,
        "level": level,
        "rarity": rarity,
        "label1": label1,
        "label2": label2,
        "message": "Trade completed",
    }

"""Shared (global) markets — one set of slots visible to ALL users.

This replaces the per-user PlayerMarket/TraitMarket. Admin controls what's
listed via the website. When a user buys, an atomic UPDATE decrements the
remaining quantity to prevent race conditions.

Models: GlobalPlayerMarket, GlobalTraitMarket, MarketPurchase.

Key principles:
  - Admin reroll wipes all slots and regenerates from a random seed
  - Each slot has `quantity` and `purchased_count` — when the latter reaches
    the former, the slot is sold out (still visible but unbuyable)
  - Players: typically quantity=1 (unique copy)
  - Traits: typically quantity=10 (multiple players can buy the same trait)
  - All purchases logged in MarketPurchase for audit
  - Per-player ownership rule still applies for player buys (can't own 2
    versions of same player)
"""

import random
import logging
from datetime import datetime
from sqlalchemy import select, update

from models import (
    GlobalPlayerMarket, GlobalTraitMarket, MarketPurchase,
    Player, Trait, User, UserRoster, TraitInventory,
)

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════════
# Player market
# ════════════════════════════════════════════════════════════════════

DEFAULT_PLAYER_SLOTS = 8           # how many players in the market by default
PLAYER_RATING_BUCKETS = [           # (weight, low, high)
    (40, 70, 79),                   # Common
    (35, 80, 84),                   # Rare
    (15, 85, 89),                   # Epic
    (8,  90, 94),                   # Legendary
    (2,  95, 100),                  # Ultimate
]


def _pick_player_for_slot(session):
    """Pick a random player based on rating buckets. Excludes inactive + variants."""
    weights = [w for w, _, _ in PLAYER_RATING_BUCKETS]
    bucket = random.choices(PLAYER_RATING_BUCKETS, weights=weights, k=1)[0]
    _, low, high = bucket
    # Pick from base players only (parent_player_id IS NULL)
    pool = (session.query(Player)
            .filter(Player.is_active == True,
                    Player.parent_player_id.is_(None),
                    Player.rating >= low,
                    Player.rating <= high)
            .all())
    if not pool:
        # Widen if empty
        pool = (session.query(Player)
                .filter(Player.is_active == True,
                        Player.parent_player_id.is_(None))
                .all())
    return random.choice(pool) if pool else None


def _calc_player_price(player):
    """Pricing curve based on rating."""
    r = player.rating
    if r >= 95: return 250000
    if r >= 90: return 120000
    if r >= 85: return 60000
    if r >= 80: return 30000
    if r >= 75: return 15000
    return 7500


def reroll_player_market(session, num_slots=DEFAULT_PLAYER_SLOTS):
    """Wipe and regenerate the player market. Returns count generated."""
    # Wipe
    session.query(GlobalPlayerMarket).delete()
    session.flush()
    # Generate
    generated = 0
    used_player_ids = set()
    for slot in range(num_slots):
        # Avoid duplicate players in the same market (across slots)
        for _ in range(20):  # retry to find unique
            p = _pick_player_for_slot(session)
            if not p:
                break
            if p.id not in used_player_ids:
                used_player_ids.add(p.id)
                break
        else:
            continue  # gave up after 20 retries
        if not p:
            continue
        base_price = _calc_player_price(p)
        # Random discount 0-15%
        discount_pct = random.choice([0, 0, 0, 5, 10, 15])
        final_price = int(base_price * (1 - discount_pct / 100))
        row = GlobalPlayerMarket(
            slot_index=slot,
            player_id=p.id,
            base_price=base_price,
            final_price=final_price,
            quantity=1,
            purchased_count=0,
            listed_at=datetime.utcnow(),
            is_active=True,
        )
        session.add(row)
        generated += 1
    session.flush()
    return generated


def list_player_market(session):
    """Return all active player market rows ordered by slot_index."""
    return (session.query(GlobalPlayerMarket)
            .filter(GlobalPlayerMarket.is_active == True)
            .order_by(GlobalPlayerMarket.slot_index).all())


def buy_player(session, user, slot_index):
    """Atomic buy of a player from the global market.

    Returns (success, message_or_player_name).

    Race-safe: uses an UPDATE with WHERE clause checking purchased_count <
    quantity. If two users buy the same slot at the same time, only one
    succeeds.
    """
    from config import MAX_ROSTER

    slot = (session.query(GlobalPlayerMarket)
            .filter(GlobalPlayerMarket.slot_index == slot_index,
                    GlobalPlayerMarket.is_active == True).first())
    if not slot:
        return False, "Slot not found or inactive."

    if slot.purchased_count >= slot.quantity:
        return False, "Sold out — try another slot."

    player = session.query(Player).get(slot.player_id)
    if not player:
        return False, "Player no longer available."

    # Block if user owns ANY version of this player
    try:
        from services.version_service import user_owns_any_version
        if user_owns_any_version(session, user.id, player.id):
            return False, f"You already own a version of <b>{player.name}</b>."
    except Exception:
        pass

    if user.roster_count >= MAX_ROSTER:
        return False, f"Roster full ({MAX_ROSTER}). Release players first."

    if user.total_coins < slot.final_price:
        return False, f"Not enough coins. Need {slot.final_price:,}, have {user.total_coins:,}."

    # Atomic decrement using UPDATE...WHERE — prevents race condition
    result = session.execute(
        update(GlobalPlayerMarket)
        .where(
            GlobalPlayerMarket.id == slot.id,
            GlobalPlayerMarket.purchased_count < GlobalPlayerMarket.quantity,
        )
        .values(purchased_count=GlobalPlayerMarket.purchased_count + 1)
    )
    if result.rowcount == 0:
        return False, "Sold out — someone else just got it."

    # Charge user, add to roster
    user.total_coins -= slot.final_price
    user.roster_count += 1
    next_pos = user.roster_count
    entry = UserRoster(
        user_id=user.id, player_id=player.id, order_position=next_pos,
        acquired_date=datetime.utcnow(),
    )
    session.add(entry)

    # Audit
    session.add(MarketPurchase(
        user_id=user.id, market_type="player",
        slot_index=slot.slot_index, item_id=player.id, item_name=player.name,
        price_paid=slot.final_price,
    ))

    return True, player.name


# ════════════════════════════════════════════════════════════════════
# Trait market
# ════════════════════════════════════════════════════════════════════

DEFAULT_TRAIT_SLOTS = 5


def _calc_trait_price(trait):
    """Pricing per trait — uses base_price if defined, else default 200."""
    return getattr(trait, "base_price", None) or 200


def reroll_trait_market(session, num_slots=DEFAULT_TRAIT_SLOTS):
    """Wipe and regenerate the trait market."""
    session.query(GlobalTraitMarket).delete()
    session.flush()
    traits = (session.query(Trait)
              .filter(Trait.is_active == True).all())
    if not traits:
        return 0
    picked = random.sample(traits, k=min(num_slots, len(traits)))
    generated = 0
    for slot, t in enumerate(picked):
        base_price = _calc_trait_price(t)
        discount_pct = random.choice([0, 0, 10, 15, 25])
        final_price = int(base_price * (1 - discount_pct / 100))
        row = GlobalTraitMarket(
            slot_index=slot,
            trait_id=t.id,
            base_price=base_price,
            discount_pct=discount_pct,
            final_price=final_price,
            quantity=10,
            purchased_count=0,
            listed_at=datetime.utcnow(),
            is_active=True,
        )
        session.add(row)
        generated += 1
    session.flush()
    return generated


def list_trait_market(session):
    return (session.query(GlobalTraitMarket)
            .filter(GlobalTraitMarket.is_active == True)
            .order_by(GlobalTraitMarket.slot_index).all())


def buy_trait(session, user, slot_index):
    """Atomic buy of a trait. Adds to user's TraitInventory at level 1."""
    slot = (session.query(GlobalTraitMarket)
            .filter(GlobalTraitMarket.slot_index == slot_index,
                    GlobalTraitMarket.is_active == True).first())
    if not slot:
        return False, "Slot not found or inactive."
    if slot.purchased_count >= slot.quantity:
        return False, "Sold out."
    trait = session.query(Trait).get(slot.trait_id)
    if not trait:
        return False, "Trait unavailable."
    if (user.total_gems or 0) < slot.final_price:
        return False, f"Not enough gems. Need {slot.final_price}, have {user.total_gems or 0}."

    # Atomic decrement
    result = session.execute(
        update(GlobalTraitMarket)
        .where(
            GlobalTraitMarket.id == slot.id,
            GlobalTraitMarket.purchased_count < GlobalTraitMarket.quantity,
        )
        .values(purchased_count=GlobalTraitMarket.purchased_count + 1)
    )
    if result.rowcount == 0:
        return False, "Sold out."

    user.total_gems = (user.total_gems or 0) - slot.final_price
    inv = TraitInventory(user_id=user.id, trait_id=trait.id, level=1)
    session.add(inv)

    session.add(MarketPurchase(
        user_id=user.id, market_type="trait",
        slot_index=slot.slot_index, item_id=trait.id, item_name=trait.name,
        price_paid=slot.final_price,
    ))
    return True, trait.name


# ════════════════════════════════════════════════════════════════════
# Admin helpers
# ════════════════════════════════════════════════════════════════════

def update_player_slot(session, slot_id, **fields):
    """Update one slot's editable fields (final_price, quantity, is_active, etc)."""
    slot = session.query(GlobalPlayerMarket).get(slot_id)
    if not slot:
        return False, "Not found."
    for k, v in fields.items():
        if k in ("base_price", "final_price", "quantity", "purchased_count"):
            try: setattr(slot, k, int(v))
            except Exception: pass
        elif k == "is_active":
            slot.is_active = bool(v)
        elif k == "player_id":
            try: setattr(slot, k, int(v))
            except Exception: pass
    session.flush()
    return True, "Updated."


def update_trait_slot(session, slot_id, **fields):
    slot = session.query(GlobalTraitMarket).get(slot_id)
    if not slot:
        return False, "Not found."
    for k, v in fields.items():
        if k in ("base_price", "final_price", "quantity", "purchased_count", "discount_pct"):
            try: setattr(slot, k, int(v))
            except Exception: pass
        elif k == "is_active":
            slot.is_active = bool(v)
        elif k == "trait_id":
            try: setattr(slot, k, int(v))
            except Exception: pass
    session.flush()
    return True, "Updated."

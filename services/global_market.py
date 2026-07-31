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

from services.player_service import not_career

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


def _pick_player_for_slot(session, min_rating=None):
    """Pick a random player at or above min_rating. Excludes inactive + variants."""
    q = (not_career(session.query(Player))
         .filter(Player.is_active == True,
                 Player.parent_player_id.is_(None)))
    if min_rating is not None:
        q = q.filter(Player.rating >= min_rating)
    pool = q.all()
    if not pool:
        # Drop the rating filter as a fallback
        pool = (not_career(session.query(Player))
                .filter(Player.is_active == True,
                        Player.parent_player_id.is_(None))
                .all())
    return random.choice(pool) if pool else None


def _calc_player_price(player):
    """Use the same pricing curve as /buy — keeps market consistent with all
    other parts of the game.
    """
    from config import get_buy_value
    return get_buy_value(player.rating)


def reroll_player_market(session, num_slots=None, min_rating=None):
    """Wipe and regenerate the player market.

    Reads market_min_rating + market_default_slots from GameConfig if not
    explicitly provided. Returns count generated.
    """
    try:
        from services.config_service import get_config
        cfg = get_config(session)
        if num_slots is None:
            num_slots = cfg.get("market_default_slots", 6)
        if min_rating is None:
            min_rating = cfg.get("market_min_rating", 87)
    except Exception:
        if num_slots is None:
            num_slots = 6
        if min_rating is None:
            min_rating = 87

    session.query(GlobalPlayerMarket).delete()
    session.flush()

    generated = 0
    used_player_ids = set()
    for slot in range(num_slots):
        for _ in range(20):
            p = _pick_player_for_slot(session, min_rating=min_rating)
            if not p:
                break
            if p.id not in used_player_ids:
                used_player_ids.add(p.id)
                break
        else:
            continue
        if not p:
            continue
        base_price = _calc_player_price(p)
        # Base price IS the sell price — the market lists at the normal /buy
        # value and gives nobody a blanket discount. The only thing that lowers
        # it is the membership perk applied per-buyer in
        # subscription_service.market_price (Platinum 5%, Diamond 10%).
        final_price = base_price
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

    # Mark the refresh time so /playermarket knows when to auto-reroll next
    try:
        from models import GameConfig
        cfg_row = session.query(GameConfig).first()
        if not cfg_row:
            cfg_row = GameConfig()
            session.add(cfg_row)
            session.flush()
        cfg_row.market_last_refresh_at = datetime.utcnow()
        session.flush()
    except Exception:
        logger.exception("failed to update market_last_refresh_at")

    return generated


IST_OFFSET_HOURS = 5.5  # India Standard Time = UTC+5:30


def _now_utc():
    return datetime.utcnow()


def _utc_to_ist(dt):
    """Convert a naive UTC datetime to a naive IST datetime."""
    from datetime import timedelta
    return dt + timedelta(hours=5, minutes=30)


def _ist_to_utc(dt):
    """Convert a naive IST datetime to a naive UTC datetime."""
    from datetime import timedelta
    return dt - timedelta(hours=5, minutes=30)


def _next_refresh_utc(refresh_hour_ist, last_refresh_utc=None):
    """Return the next UTC datetime when the market should refresh.

    The market refreshes once a day at refresh_hour_ist (in IST). Given we
    last refreshed at last_refresh_utc, return the next time refresh_hour_ist
    will occur AFTER that (or after now if no last_refresh).
    """
    from datetime import timedelta
    now_utc = _now_utc()
    # The reference point is "today's refresh hour in IST"
    now_ist = _utc_to_ist(now_utc)
    today_ist_refresh = now_ist.replace(
        hour=int(refresh_hour_ist) % 24, minute=0, second=0, microsecond=0,
    )
    # Convert today's refresh time to UTC
    today_refresh_utc = _ist_to_utc(today_ist_refresh)

    if last_refresh_utc is None:
        # Never refreshed — next is today's refresh time if still ahead, else tomorrow
        if today_refresh_utc > now_utc:
            return today_refresh_utc
        return today_refresh_utc + timedelta(days=1)

    # Find next refresh time that's after last_refresh_utc
    candidate = today_refresh_utc
    # If today's refresh time was already past at last_refresh, jump forward
    while candidate <= last_refresh_utc:
        candidate += timedelta(days=1)
    return candidate


def _is_due(last_refresh_utc, refresh_hour_ist):
    """Has the next scheduled refresh time already passed?"""
    if last_refresh_utc is None:
        return True  # never refreshed
    next_at = _next_refresh_utc(refresh_hour_ist, last_refresh_utc)
    return _now_utc() >= next_at


def ensure_player_market_fresh(session):
    """Reroll the player market if today's refresh time has passed since last reroll.
    Called from the bot before showing the market.
    """
    from models import GameConfig
    cfg_row = session.query(GameConfig).first()
    last = cfg_row.market_last_refresh_at if cfg_row else None
    refresh_hour = (cfg_row.market_refresh_hour_ist if cfg_row else 0) or 0

    if _is_due(last, refresh_hour):
        try:
            n = reroll_player_market(session)
            session.commit()
            logger.info(f"Auto-rerolled player market: {n} slots")
            return True
        except Exception:
            session.rollback()
            logger.exception("Auto-reroll player market failed")
    return False


def ensure_trait_market_fresh(session):
    """Reroll the trait market if today's refresh time has passed since last reroll."""
    from models import GameConfig
    cfg_row = session.query(GameConfig).first()
    last = cfg_row.trait_market_last_refresh_at if cfg_row else None
    refresh_hour = (cfg_row.market_refresh_hour_ist if cfg_row else 0) or 0

    if _is_due(last, refresh_hour):
        try:
            n = reroll_trait_market(session)
            session.commit()
            logger.info(f"Auto-rerolled trait market: {n} slots")
            return True
        except Exception:
            session.rollback()
            logger.exception("Auto-reroll trait market failed")
    return False


def get_next_refresh_at(session):
    """UTC datetime when the next auto-refresh will fire (or None)."""
    from models import GameConfig
    cfg_row = session.query(GameConfig).first()
    if not cfg_row:
        return None
    last = cfg_row.market_last_refresh_at
    refresh_hour = cfg_row.market_refresh_hour_ist or 0
    return _next_refresh_utc(refresh_hour, last)


def get_next_trait_refresh_at(session):
    from models import GameConfig
    cfg_row = session.query(GameConfig).first()
    if not cfg_row:
        return None
    last = cfg_row.trait_market_last_refresh_at
    refresh_hour = cfg_row.market_refresh_hour_ist or 0
    return _next_refresh_utc(refresh_hour, last)


def list_player_market(session):
    """Return all active player market rows ordered by slot_index."""
    return (session.query(GlobalPlayerMarket)
            .filter(GlobalPlayerMarket.is_active == True)
            .order_by(GlobalPlayerMarket.slot_index).all())


def add_player_to_market(session, player_id, custom_price=None):
    """Add a single player (base or variant) to the market in the next free slot.
    Returns (success, message_or_slot_index).
    """
    player = session.query(Player).get(player_id)
    if not player:
        return False, "Player not found."
    if not player.is_active:
        return False, "Player is inactive."

    # Don't allow duplicates of the same player_id in the market
    existing = (session.query(GlobalPlayerMarket)
                .filter(GlobalPlayerMarket.player_id == player_id).first())
    if existing:
        return False, f"{player.name} is already at slot #{existing.slot_index}."

    # Find next available slot_index
    max_slot = (session.query(GlobalPlayerMarket.slot_index)
                .order_by(GlobalPlayerMarket.slot_index.desc()).first())
    next_slot = (max_slot[0] + 1) if max_slot else 0

    base_price = custom_price if custom_price else _calc_player_price(player)
    # Sell price == base price; the membership discount is applied per-buyer.
    final_price = base_price
    row = GlobalPlayerMarket(
        slot_index=next_slot,
        player_id=player.id,
        base_price=base_price,
        final_price=final_price,
        quantity=1,
        purchased_count=0,
        listed_at=datetime.utcnow(),
        is_active=True,
    )
    session.add(row)
    session.flush()
    return True, next_slot


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

    # Free, Bronze and Silver pay the slot's sell price (== base price); the
    # discount is a membership perk (Platinum 5%, Diamond 10%).
    from services import subscription_service
    price = subscription_service.market_price(user, slot.base_price, slot.final_price)

    if user.total_coins < price:
        return False, f"Not enough coins. Need {price:,}, have {user.total_coins:,}."

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
    user.total_coins -= price
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
        price_paid=price,
    ))

    return True, player.name


# ════════════════════════════════════════════════════════════════════
# Trait market
# ════════════════════════════════════════════════════════════════════

DEFAULT_TRAIT_SLOTS = 5


def _calc_trait_price(trait):
    """Pricing per trait — uses base_price if defined, else default 200."""
    return getattr(trait, "base_price", None) or 200


def reroll_trait_market(session, num_slots=None):
    """Wipe and regenerate the trait market. Reads slot count from config."""
    if num_slots is None:
        try:
            from services.config_service import get_config
            cfg = get_config(session)
            num_slots = cfg.get("trait_market_default_slots", 5)
        except Exception:
            num_slots = 5

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

    # Update last refresh marker
    try:
        from models import GameConfig
        cfg_row = session.query(GameConfig).first()
        if not cfg_row:
            cfg_row = GameConfig()
            session.add(cfg_row)
            session.flush()
        cfg_row.trait_market_last_refresh_at = datetime.utcnow()
        session.flush()
    except Exception:
        logger.exception("failed to update trait_market_last_refresh_at")

    return generated


def list_trait_market(session):
    return (session.query(GlobalTraitMarket)
            .filter(GlobalTraitMarket.is_active == True)
            .order_by(GlobalTraitMarket.slot_index).all())


def add_trait_to_market(session, trait_id, custom_price=None, quantity=10):
    """Add a single trait to the trait market in the next free slot.
    Returns (success, message_or_slot_index).
    """
    trait = session.query(Trait).get(trait_id)
    if not trait:
        return False, "Trait not found."
    if not trait.is_active:
        return False, "Trait is inactive."

    # Don't allow duplicates
    existing = (session.query(GlobalTraitMarket)
                .filter(GlobalTraitMarket.trait_id == trait_id).first())
    if existing:
        return False, f"{trait.name} is already at slot #{existing.slot_index}."

    max_slot = (session.query(GlobalTraitMarket.slot_index)
                .order_by(GlobalTraitMarket.slot_index.desc()).first())
    next_slot = (max_slot[0] + 1) if max_slot else 0

    base_price = custom_price if custom_price else _calc_trait_price(trait)
    final_price = custom_price if custom_price else base_price
    row = GlobalTraitMarket(
        slot_index=next_slot,
        trait_id=trait.id,
        base_price=base_price,
        discount_pct=0,
        final_price=final_price,
        quantity=int(quantity) if quantity else 10,
        purchased_count=0,
        listed_at=datetime.utcnow(),
        is_active=True,
    )
    session.add(row)
    session.flush()
    return True, next_slot


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

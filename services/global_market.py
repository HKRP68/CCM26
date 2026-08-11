"""Shared (global) markets — one set of slots visible to ALL users.

This replaces the per-user PlayerMarket/TraitMarket. Admin controls what's
listed via the website. When a user buys, an atomic UPDATE decrements the
remaining quantity to prevent race conditions.

Models: GlobalPlayerMarket, GlobalTraitMarket, MarketPurchase.

Key principles:
  - Admin reroll wipes all slots and regenerates from a random seed
  - Stock is UNLIMITED by default (`quantity = 0`). Everything listed stays
    buyable by everyone until the next refresh or until the admin pulls it —
    the market is a shop, not a race. A positive `quantity` turns a slot back
    into a limited run, and then `purchased_count` reaching it sells the slot
    out (still visible but unbuyable).
  - `purchased_count` is always maintained, unlimited or not, because it is
    what tells the admin which listings people actually want.
  - All purchases logged in MarketPurchase for audit
  - Per-player ownership rule still applies for player buys — unlimited stock
    lets a hundred captains own the same card, but never the same captain twice
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
# Stock
# ════════════════════════════════════════════════════════════════════

UNLIMITED = 0  # the value stored in `quantity` to mean "never runs out"
INFINITY = "∞"


def is_unlimited(slot) -> bool:
    """True when this slot can be bought any number of times."""
    try:
        return int(getattr(slot, "quantity", 0) or 0) <= 0
    except (TypeError, ValueError):
        return True


def is_sold_out(slot) -> bool:
    """The one place 'can I still buy this?' is decided.

    Every caller used to compare `purchased_count >= quantity` inline, which is
    exactly the comparison that reads an unlimited slot (quantity 0) as sold out
    from the moment it is listed. Ask this instead.
    """
    if is_unlimited(slot):
        return False
    return int(getattr(slot, "purchased_count", 0) or 0) >= int(slot.quantity)


def stock_left(slot):
    """Copies still available, or None when the slot is unlimited."""
    if is_unlimited(slot):
        return None
    return max(0, int(slot.quantity) - int(getattr(slot, "purchased_count", 0) or 0))


def stock_label(slot) -> str:
    """Short human label for a slot's stock: "∞" or "3 left"."""
    left = stock_left(slot)
    return INFINITY if left is None else f"{left} left"

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
            quantity=UNLIMITED,
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


def _next_refresh_utc(refresh_hour_ist, last_refresh_utc=None, interval_hours=24):
    """Return the next UTC datetime when the market should refresh.

    The market refreshes every ``interval_hours``, starting from
    ``refresh_hour_ist`` (an IST clock hour). With the default 24 that is one
    daily reroll at that hour — the behaviour both markets shipped with. With
    ``interval_hours=12`` and hour 0 it is midnight and noon IST. Both markets
    drive this from their own website setting.

    Given ``last_refresh_utc``, this returns the first anchor strictly after
    it; with no last refresh, the first anchor after now.
    """
    from datetime import timedelta
    from config import refresh_anchor_hours

    # The search is anchored on the LAST refresh, not on now. A market that has
    # not rerolled for a week must report the anchor it first missed — that is
    # what makes ``_is_due`` mean "has an anchor passed since we last ran?".
    after = last_refresh_utc if last_refresh_utc is not None else _now_utc()
    after_ist = _utc_to_ist(after)
    midnight_ist = after_ist.replace(hour=0, minute=0, second=0, microsecond=0)

    # Span the day either side so the search never falls off an end: the answer
    # for "refreshed late yesterday" can be an anchor earlier the same day.
    anchors = sorted(
        _ist_to_utc(midnight_ist + timedelta(days=day, hours=hour))
        for day in (-1, 0, 1)
        for hour in refresh_anchor_hours(refresh_hour_ist, interval_hours)
    )

    for candidate in anchors:
        if candidate > after:
            return candidate
    # Unreachable while the anchors span a full day either side of `after`,
    # but stay defensive rather than returning None to a caller doing >=.
    return anchors[-1] + timedelta(days=1)


def _is_due(last_refresh_utc, refresh_hour_ist, interval_hours=24):
    """Has the next scheduled refresh time already passed?"""
    if last_refresh_utc is None:
        return True  # never refreshed
    next_at = _next_refresh_utc(refresh_hour_ist, last_refresh_utc,
                                interval_hours)
    return _now_utc() >= next_at


def _schedule(start, stored_interval, fallback_start=0):
    """``(start_hour_ist, interval_hours)`` from two raw config values.

    Shared by both markets, which store the same pair of settings in their own
    columns. NULL start falls back to ``fallback_start`` — a DB migrated but
    not yet re-saved keeps the time it already had rather than silently moving
    to midnight.
    """
    from config import clamp_refresh_interval
    if start is None:
        start = fallback_start or 0
    # Only NULL means "unset" — a stored 0 is a real value and must reach
    # clamp_refresh_interval, which snaps it to the 1-hour minimum rather than
    # silently reading as "once a day".
    interval = clamp_refresh_interval(
        24 if stored_interval is None else stored_interval)
    return int(start) % 24, interval


def _player_schedule(cfg_row):
    """``(start_hour_ist, interval_hours)`` for the player market.

    Interval 24 — the column default — is the single daily reroll this market
    ran on before the interval was configurable, so nothing moves until an
    admin picks a shorter one.
    """
    if not cfg_row:
        return 0, 24
    return _schedule(cfg_row.market_refresh_hour_ist,
                     cfg_row.market_refresh_interval_hours)


def _trait_schedule(cfg_row):
    """``(start_hour_ist, interval_hours)`` for the trait market."""
    if not cfg_row:
        return 0, 24
    return _schedule(cfg_row.trait_market_refresh_start_hour_ist,
                     cfg_row.trait_market_refresh_interval_hours,
                     fallback_start=cfg_row.market_refresh_hour_ist)


def ensure_player_market_fresh(session):
    """Reroll the player market if a scheduled refresh has passed since the last
    reroll. Called from the bot before showing the market.
    """
    from models import GameConfig
    cfg_row = session.query(GameConfig).first()
    last = cfg_row.market_last_refresh_at if cfg_row else None
    refresh_hour, interval = _player_schedule(cfg_row)

    if _is_due(last, refresh_hour, interval):
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
    refresh_hour, interval = _trait_schedule(cfg_row)

    if _is_due(last, refresh_hour, interval):
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
    refresh_hour, interval = _player_schedule(cfg_row)
    return _next_refresh_utc(refresh_hour, last, interval)


def get_next_trait_refresh_at(session):
    from models import GameConfig
    cfg_row = session.query(GameConfig).first()
    if not cfg_row:
        return None
    last = cfg_row.trait_market_last_refresh_at
    refresh_hour, interval = _trait_schedule(cfg_row)
    return _next_refresh_utc(refresh_hour, last, interval)


def _describe_schedule(start, interval):
    """``{start_hour, interval_hours, hours, labels, summary}`` for a schedule.

    The summary is a ready-to-render string like "12:00 AM, 12:00 PM IST".
    """
    from config import refresh_anchor_hours, format_hour_ist
    hours = refresh_anchor_hours(start, interval)
    labels = [format_hour_ist(h) for h in hours]
    return {
        "start_hour": start,
        "interval_hours": interval,
        "hours": hours,
        "labels": labels,
        "summary": ", ".join(labels) + " IST",
    }


def get_player_refresh_schedule(session):
    """Describe the player market's schedule, for the admin page and the bot."""
    from models import GameConfig
    return _describe_schedule(*_player_schedule(session.query(GameConfig).first()))


def get_trait_refresh_schedule(session):
    """Describe the trait market's schedule for the admin page."""
    from models import GameConfig
    return _describe_schedule(*_trait_schedule(session.query(GameConfig).first()))


def _interval_hours(schedule_fn, session):
    """One market's configured interval, opening a session if given none.

    Falls back to 24 rather than raising: a caller asking "how often does this
    refresh?" is labelling a screen, and a wrong label beats a crashed command.
    """
    from models import GameConfig
    own = session is None
    if own:
        from database import get_session
        session = get_session()
    try:
        return schedule_fn(session.query(GameConfig).first())[1]
    except Exception:
        logger.exception("refresh interval lookup failed; using 24h")
        return 24
    finally:
        if own:
            session.close()


def get_player_refresh_interval_hours(session=None):
    """The configured player-refresh interval, for callers outside this module."""
    return _interval_hours(_player_schedule, session)


def get_trait_refresh_interval_hours(session=None):
    """The configured trait-refresh interval, for callers outside this module."""
    return _interval_hours(_trait_schedule, session)


def is_trait_refresh_due(last_refresh_utc, session=None):
    """Is a trait shop due, against the configured IST anchor schedule?

    Same question ``ensure_trait_market_fresh`` asks of the shared market, for
    callers holding their own timestamp — notably the legacy per-user shop in
    ``services.trait_service.refresh_shop``. Anchors matter here rather than
    bare elapsed time: with "every 12 hours from 12 AM" configured, a shop that
    last rolled at 11 PM is due at midnight, one hour later, not at 11 AM.
    Without it the shared market and the per-user shops would drift apart on
    the same setting.

    Fails open (True) if the config cannot be read, matching how the rest of
    this module treats an unreadable schedule: an extra reroll is harmless, a
    permanently stale shop is not.
    """
    from models import GameConfig
    own = session is None
    if own:
        from database import get_session
        session = get_session()
    try:
        start, interval = _trait_schedule(session.query(GameConfig).first())
        return _is_due(last_refresh_utc, start, interval)
    except Exception:
        logger.exception("is_trait_refresh_due failed; forcing a refresh")
        return True
    finally:
        if own:
            session.close()


def list_player_market(session):
    """Return all active player market rows ordered by slot_index."""
    return (session.query(GlobalPlayerMarket)
            .filter(GlobalPlayerMarket.is_active == True)
            .order_by(GlobalPlayerMarket.slot_index).all())


def add_player_to_market(session, player_id, custom_price=None,
                         quantity=UNLIMITED):
    """Add a single player (base or variant) to the market in the next free slot.

    ``quantity`` defaults to unlimited; pass a positive number for a limited run.
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
        quantity=max(UNLIMITED, int(quantity or UNLIMITED)),
        purchased_count=0,
        listed_at=datetime.utcnow(),
        is_active=True,
    )
    session.add(row)
    session.flush()
    return True, next_slot


def player_slot_prices(user, slot, player):
    """(list price, this buyer's price) for a player slot — never below resale.

    The market's asking price is admin-editable, per slot and per listing, and
    releasing a card always pays ``get_sell_value`` regardless of what its owner
    paid. A slot priced under that is therefore a coin printer — buy, release,
    buy again, with unlimited stock — so both figures are floored at
    ``config.market_price_floor``. The floor lands after the membership
    discount, because 10% off a price that only just clears resale would dip
    back under it.

    Every surface that shows or charges a market price goes through here (bot
    ``/playermarket``, the Mini App market list and both buy endpoints), so the
    price a buyer is quoted is always the price they are charged.
    """
    from config import market_price_floor
    from services import subscription_service

    floor = market_price_floor(player.rating)
    listed = max(subscription_service.market_sell_price(
        slot.base_price, slot.final_price), floor)
    price = max(subscription_service.market_price(
        user, slot.base_price, slot.final_price), floor)
    return listed, price


def buy_player(session, user, slot_index):
    """Atomic buy of a player from the global market.

    Returns (success, message_or_player_name, gem_bonus) — ``gem_bonus`` is the
    elite-signing rebate credited on this buy, 0 on every failure and on any
    card not rated above 95.

    Race-safe for a limited run: the UPDATE's WHERE clause checks
    purchased_count < quantity, so if two users buy the last copy at the same
    time only one succeeds. An unlimited slot has no such contention — the
    count is still incremented, purely as a record of demand.
    """
    from config import MAX_ROSTER

    slot = (session.query(GlobalPlayerMarket)
            .filter(GlobalPlayerMarket.slot_index == slot_index,
                    GlobalPlayerMarket.is_active == True).first())
    if not slot:
        return False, "Slot not found or inactive.", 0

    if is_sold_out(slot):
        return False, "Sold out — try another slot.", 0

    player = session.query(Player).get(slot.player_id)
    if not player:
        return False, "Player no longer available.", 0

    # Block if user owns ANY version of this player
    try:
        from services.version_service import user_owns_any_version
        if user_owns_any_version(session, user.id, player.id):
            return False, f"You already own a version of <b>{player.name}</b>.", 0
    except Exception:
        pass

    if user.roster_count >= MAX_ROSTER:
        return False, f"Roster full ({MAX_ROSTER}). Release players first.", 0

    # Free, Bronze and Silver pay the slot's sell price (== base price); the
    # discount is a membership perk (Platinum 5%, Diamond 10%). Floored at
    # resale value so the slot can't be farmed for coins.
    from services import subscription_service
    asked = subscription_service.market_price(
        user, slot.base_price, slot.final_price)
    _listed, price = player_slot_prices(user, slot, player)
    if price > asked:
        # An admin's sale price (or a discount on top of one) dipped under what
        # releasing the card pays back. Say so — the markets page still shows
        # the number they typed.
        logger.warning(
            "market slot %s (%s, %s OVR) priced at %s, under its %s resale "
            "floor — charging the floor instead.",
            slot.slot_index, player.name, player.rating, asked, price)

    if user.total_coins < price:
        return False, f"Not enough coins. Need {price:,}, have {user.total_coins:,}.", 0

    # Atomic decrement using UPDATE...WHERE — prevents race condition
    where = [GlobalPlayerMarket.id == slot.id]
    if not is_unlimited(slot):
        where.append(
            GlobalPlayerMarket.purchased_count < GlobalPlayerMarket.quantity)
    result = session.execute(
        update(GlobalPlayerMarket)
        .where(*where)
        .values(purchased_count=GlobalPlayerMarket.purchased_count + 1)
    )
    if result.rowcount == 0:
        return False, "Sold out — someone else just got it.", 0

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

    # Elite signing rebate — gems back on anything rated above 95, sized on the
    # coins actually paid so a membership discount can't out-earn its own cost.
    #
    # No /cmuundo record is written here, deliberately: reversing a market buy
    # would have to hand the slot's stock back (purchased_count, and the
    # MarketPurchase row above), which the undo handler knows nothing about.
    # That is also what makes the rebate safe to credit — nothing can reverse
    # the purchase while leaving the gems behind.
    from services.buy_bonus import award_buy_gem_bonus
    gem_bonus = award_buy_gem_bonus(session, user, player, price,
                                    source="playermarket")

    return True, player.name, gem_bonus


# ════════════════════════════════════════════════════════════════════
# Trait market
# ════════════════════════════════════════════════════════════════════

DEFAULT_TRAIT_SLOTS = 5


def _calc_trait_price(trait):
    """Gems for a Lv.1 copy — the admin's pinned price, else the rarity's."""
    from services.trait_service import trait_price_of
    return trait_price_of(trait)


def roll_trait_discount():
    """A sale percentage the resale economy can survive.

    ``config.trait_sell_value`` prices resale just under ``trait_floor_cost`` —
    the cheapest gems a trait could possibly have cost — and that floor is
    computed from ``TRAIT_DISCOUNT_RANGE``. Listing a slot at a *deeper*
    discount than that range therefore sells a trait for less than resale pays
    back, which is a gem printer: buy the slot, /selltrait it, repeat. This
    market used to roll a flat 25% against a 20% range and got away with it
    only because stock ran out at ten copies. Unlimited stock removes that
    accident, so the roll is now clamped to the range resale was priced for.
    """
    from config import TRAIT_DISCOUNT_RANGE
    low, high = min(TRAIT_DISCOUNT_RANGE), max(TRAIT_DISCOUNT_RANGE)
    mid = (low + high) // 2
    return random.choice([0, 0, low, mid, high])


def pick_traits_by_rarity(traits, num_slots):
    """Choose the traits to list, weighted so Elite stays a sighting.

    A flat ``random.sample`` over the catalogue would put an Elite trait in the
    shop roughly a quarter of the time now that there are ten of them — which
    is not what "very rare" means, and matters more than it used to because
    stock is unlimited: whatever is listed is buyable by everyone all day.
    Weights come from ``config.TRAIT_RARITIES``.
    """
    from config import trait_rarity_key, TRAIT_RARITIES
    from services.trait_service import trait_rarity_of

    pool = list(traits)
    picked = []
    n = min(int(num_slots or 0), len(pool))
    for _ in range(n):
        weights = [max(1, TRAIT_RARITIES[trait_rarity_key(trait_rarity_of(t))]["weight"])
                   for t in pool]
        chosen = random.choices(pool, weights=weights, k=1)[0]
        picked.append(chosen)
        pool.remove(chosen)
    return picked


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
    picked = pick_traits_by_rarity(traits, num_slots)
    generated = 0
    for slot, t in enumerate(picked):
        base_price = _calc_trait_price(t)
        discount_pct = roll_trait_discount()
        final_price = base_price * (100 - discount_pct) // 100
        row = GlobalTraitMarket(
            slot_index=slot,
            trait_id=t.id,
            base_price=base_price,
            discount_pct=discount_pct,
            final_price=final_price,
            quantity=UNLIMITED,
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


def add_trait_to_market(session, trait_id, custom_price=None,
                        quantity=UNLIMITED):
    """Add a single trait to the trait market in the next free slot.

    ``quantity`` defaults to unlimited; pass a positive number for a limited run.
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
        quantity=max(UNLIMITED, int(quantity or UNLIMITED)),
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
    if is_sold_out(slot):
        return False, "Sold out."
    trait = session.query(Trait).get(slot.trait_id)
    if not trait:
        return False, "Trait unavailable."
    if (user.total_gems or 0) < slot.final_price:
        return False, f"Not enough gems. Need {slot.final_price}, have {user.total_gems or 0}."

    # Atomic decrement (no stock race to lose on an unlimited slot)
    where = [GlobalTraitMarket.id == slot.id]
    if not is_unlimited(slot):
        where.append(
            GlobalTraitMarket.purchased_count < GlobalTraitMarket.quantity)
    result = session.execute(
        update(GlobalTraitMarket)
        .where(*where)
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
    """Update one slot's editable fields (final_price, quantity, is_active, etc).

    ``quantity`` 0 (or below) is stored as UNLIMITED rather than rejected — that
    is how the admin page puts a slot back into unlimited stock.
    """
    slot = session.query(GlobalPlayerMarket).get(slot_id)
    if not slot:
        return False, "Not found."
    for k, v in fields.items():
        if k == "quantity":
            try: slot.quantity = max(UNLIMITED, int(v))
            except Exception: pass
        elif k in ("base_price", "final_price", "purchased_count"):
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
        if k == "quantity":
            try: slot.quantity = max(UNLIMITED, int(v))
            except Exception: pass
        elif k in ("base_price", "final_price", "purchased_count", "discount_pct"):
            try: setattr(slot, k, int(v))
            except Exception: pass
        elif k == "is_active":
            slot.is_active = bool(v)
        elif k == "trait_id":
            try: setattr(slot, k, int(v))
            except Exception: pass
    session.flush()
    return True, "Updated."

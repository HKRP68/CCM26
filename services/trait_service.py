"""Trait system — business logic for shop, inventory, application, upgrades."""

import html
import random
import logging
from datetime import datetime, timedelta

from models import (
    User, UserRoster, Player, Trait, PlayerTrait, TraitInventory,
    TraitMarket, TraitDaily,
)
from config import (
    TRAIT_SHOP_SLOTS, TRAIT_SHOP_DAILY_PURCHASE_LIMIT, TRAIT_SHOP_BASE_PRICE,
    TRAIT_REROLL_COST, TRAIT_DAILY_DISCOUNT_MIN, TRAIT_DAILY_DISCOUNT_MAX,
    TRAIT_UPGRADE_COSTS, TRAIT_REPLACE_COST,
    TRAIT_MAX_PER_PLAYER, TRAIT_MAX_SAME_CATEGORY, TRAIT_MAX_ELITE_PER_PLAYER,
    TRAIT_MAX_PER_SQUAD,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# TRAIT DEFINITIONS — seeded into the `traits` table
# ═══════════════════════════════════════════════════════════════════════
#
# This list is the single source of truth for the catalogue. ``database.
# _seed_traits`` upserts it on every boot (keyed on ``effect_key``), so editing
# a description, emoji, category or rarity here updates the live rows — it does
# NOT touch ``is_active``, which stays the admin's switch.
#
# Every ``effect_key`` must have a handler in ``services.trait_engine.
# TRAIT_HANDLERS``; a key with no handler is a trait that quietly does nothing,
# which tests/test_trait_catalogue.py exists to prevent.
#
# ``rarity`` (config.TRAIT_RARITIES) sets both how often the shared market rolls
# a trait and what it costs:
#
#   ⚪ common  ×1 →   150 💎    a straightforward, always-on edge
#   🔵 rare    ×2 →   300 💎    conditional, but the condition comes up often
#   🟣 epic    ×4 →   600 💎    situational and strong when it lands
#   ⭐ elite   ×8 → 1,200 💎    very rare; one per player (TRAIT_MAX_ELITE_PER_PLAYER)

TRAIT_DEFINITIONS = [
    # ── BATTING ──────────────────────────────────────────────────────
    {"name": "Finisher", "category": "Batting", "emoji": "🔥", "rarity": "rare",
     "description": "Boosts 6s and 4s in the last 3 overs of the innings.",
     "effect_key": "bat_finisher"},
    {"name": "Power Hitter", "category": "Batting", "emoji": "💥", "rarity": "common",
     "description": "More 6s at the cost of higher wicket risk.",
     "effect_key": "bat_power_hitter"},
    {"name": "Anchor", "category": "Batting", "emoji": "⚓", "rarity": "common",
     "description": "Lower wicket chance but slightly fewer 6s.",
     "effect_key": "bat_anchor"},
    {"name": "Fast Starter", "category": "Batting", "emoji": "⚡", "rarity": "common",
     "description": "Boosts boundaries in the first 10 balls faced.",
     "effect_key": "bat_fast_starter"},
    {"name": "Clutch Player", "category": "Batting", "emoji": "🎯", "rarity": "rare",
     "description": "Boosts boundaries when required run rate exceeds 8.",
     "effect_key": "bat_clutch"},
    {"name": "Spin Basher", "category": "Batting", "emoji": "🌀", "rarity": "rare",
     "description": "Stronger against spin bowlers — more boundaries, less risk.",
     "effect_key": "bat_spin_basher"},
    {"name": "Pace Destroyer", "category": "Batting", "emoji": "🚀", "rarity": "rare",
     "description": "Better against fast bowlers — more boundaries, less risk.",
     "effect_key": "bat_pace_destroyer"},
    {"name": "Late Bloomer", "category": "Batting", "emoji": "🌱", "rarity": "rare",
     "description": "Gets stronger after facing 20 balls.",
     "effect_key": "bat_late_bloomer"},
    {"name": "Power Surge", "category": "Batting", "emoji": "⚡", "rarity": "epic",
     "description": "Temporary boost after consecutive boundaries.",
     "effect_key": "bat_power_surge"},
    {"name": "Pinch Hitter", "category": "Batting", "emoji": "🏏", "rarity": "common",
     "description": "Extra boundaries while the powerplay field is up.",
     "effect_key": "bat_pinch_hitter"},

    # ── BOWLING ──────────────────────────────────────────────────────
    {"name": "Death Specialist", "category": "Bowling", "emoji": "☠️", "rarity": "rare",
     "description": "Better economy & wickets in the last 3 overs.",
     "effect_key": "bowl_death"},
    {"name": "Wicket Hunter", "category": "Bowling", "emoji": "🏹", "rarity": "common",
     "description": "Higher wicket chance on every delivery.",
     "effect_key": "bowl_wicket_hunter"},
    {"name": "Dot Ball Specialist", "category": "Bowling", "emoji": "⚪", "rarity": "common",
     "description": "Higher dot ball chance on every delivery.",
     "effect_key": "bowl_dot_specialist"},
    {"name": "Powerplay King", "category": "Bowling", "emoji": "👑", "rarity": "rare",
     "description": "Strong in the first 3 overs — more dots, more wickets.",
     "effect_key": "bowl_powerplay"},
    {"name": "Yorker Specialist", "category": "Bowling", "emoji": "🎯", "rarity": "rare",
     "description": "Death-over accuracy — more wickets & dots in final 3 overs.",
     "effect_key": "bowl_yorker"},
    {"name": "Spell Builder", "category": "Bowling", "emoji": "📶", "rarity": "epic",
     "description": "Improves with each over bowled in the spell.",
     "effect_key": "bowl_spell_builder"},
    {"name": "Partnership Breaker", "category": "Bowling", "emoji": "💔", "rarity": "epic",
     "description": "Bonus against 50+ partnerships.",
     "effect_key": "bowl_partner_breaker"},
    {"name": "Tail-End Hunter", "category": "Bowling", "emoji": "🐍", "rarity": "epic",
     "description": "Much stronger against lower-order batters.",
     "effect_key": "bowl_tail_hunter"},
    {"name": "Middle-Over Squeeze", "category": "Bowling", "emoji": "🗜️", "rarity": "rare",
     "description": "Chokes the middle overs — more dots, more wickets.",
     "effect_key": "bowl_middle_squeeze"},
    {"name": "Economy Machine", "category": "Bowling", "emoji": "🔒", "rarity": "common",
     "description": "Concedes fewer boundaries in every phase.",
     "effect_key": "bowl_economy"},

    # ── FIELDING ─────────────────────────────────────────────────────
    {"name": "Safe Hands", "category": "Fielding", "emoji": "🧤", "rarity": "common",
     "description": "Lowers the chance of a dropped catch.",
     "effect_key": "field_safe_hands"},
    {"name": "Sniper Arm", "category": "Fielding", "emoji": "🎯", "rarity": "common",
     "description": "Raises run-out chance on quick singles.",
     "effect_key": "field_sniper"},
    {"name": "Boundary Rider", "category": "Fielding", "emoji": "🛡️", "rarity": "rare",
     "description": "Cuts off 4s in the deep — boundaries become singles.",
     "effect_key": "field_boundary_rider"},
    {"name": "Livewire", "category": "Fielding", "emoji": "⚡", "rarity": "common",
     "description": "Electric in the ring — turns pushed runs into dots.",
     "effect_key": "field_livewire"},

    # ── MENTAL ───────────────────────────────────────────────────────
    {"name": "Consistency King", "category": "Mental", "emoji": "📊", "rarity": "rare",
     "description": "Trims extremes — fewer wickets but also fewer 6s.",
     "effect_key": "mental_consistency"},
    {"name": "Momentum Player", "category": "Mental", "emoji": "📈", "rarity": "rare",
     "description": "Gains momentum — bonus scales from 0% to full over balls 1-30.",
     "effect_key": "mental_momentum"},
    {"name": "Confidence Player", "category": "Mental", "emoji": "😎", "rarity": "common",
     "description": "Gains a small bonus after every boundary.",
     "effect_key": "mental_confidence"},
    {"name": "Comeback King", "category": "Mental", "emoji": "🔄", "rarity": "rare",
     "description": "Gets stronger after being beaten earlier in the spell.",
     "effect_key": "mental_comeback"},
    {"name": "Ice Veins", "category": "Mental", "emoji": "🧊", "rarity": "rare",
     "description": "Calm under a high required rate — far less likely to throw it away.",
     "effect_key": "mental_ice"},

    # ── AWARENESS ────────────────────────────────────────────────────
    {"name": "Pitch Reader", "category": "Awareness", "emoji": "🔍", "rarity": "rare",
     "description": "Adapts faster to pitch conditions — settles in early.",
     "effect_key": "aware_pitch"},
    {"name": "Strike Rotator", "category": "Awareness", "emoji": "🔁", "rarity": "common",
     "description": "Converts dots into singles more often.",
     "effect_key": "aware_rotation"},
    {"name": "Gap Finder", "category": "Awareness", "emoji": "🎳", "rarity": "common",
     "description": "Slightly more 2s and 4s.",
     "effect_key": "aware_gap"},

    # ── SPECIAL ──────────────────────────────────────────────────────
    {"name": "Giant Killer", "category": "Special", "emoji": "🗡️", "rarity": "epic",
     "description": "Performs better against higher-rated opponents.",
     "effect_key": "special_giantkiller"},

    # ── ELITE (very rare — one per player) ───────────────────────────
    {"name": "Master Blaster", "category": "Elite", "emoji": "💫", "rarity": "elite",
     "description": "Excels in every batting phase.",
     "effect_key": "elite_master_blaster"},
    {"name": "Run Machine", "category": "Elite", "emoji": "🏃", "rarity": "elite",
     "description": "Consistently scores big innings — grows the longer they bat.",
     "effect_key": "elite_run_machine"},
    {"name": "Ice Finisher", "category": "Elite", "emoji": "❄️", "rarity": "elite",
     "description": "Elite finisher in close chases.",
     "effect_key": "elite_ice_finisher"},
    {"name": "Bowling Wizard", "category": "Elite", "emoji": "🧙", "rarity": "elite",
     "description": "Small bonus in every bowling phase.",
     "effect_key": "elite_bowling_wizard"},
    {"name": "Magic Spell", "category": "Elite", "emoji": "✨", "rarity": "elite",
     "description": "Occasionally produces an exceptional over.",
     "effect_key": "elite_magic_spell"},
    {"name": "Unplayable", "category": "Elite", "emoji": "🚧", "rarity": "elite",
     "description": "Rarely gets hit for consecutive boundaries.",
     "effect_key": "elite_unplayable"},
    {"name": "Golden Arm", "category": "Elite", "emoji": "🥇", "rarity": "elite",
     "description": "Strikes early when introduced into the attack.",
     "effect_key": "elite_golden_arm"},
    {"name": "Big Fish Hunter", "category": "Elite", "emoji": "🐋", "rarity": "elite",
     "description": "More likely to dismiss top-order batters.",
     "effect_key": "elite_big_fish"},
    {"name": "Nightmare Matchup", "category": "Elite", "emoji": "😱", "rarity": "elite",
     "description": "Performs exceptionally against a specific batter type.",
     "effect_key": "elite_matchup"},
    {"name": "GOAT Instinct", "category": "Elite", "emoji": "🐐", "rarity": "elite",
     "description": "Rare clutch boost in high-pressure moments.",
     "effect_key": "elite_goat"},
]

# Display order for anything that groups the catalogue (the /traits list, the
# admin page). Anything not named here sorts last, alphabetically.
TRAIT_CATEGORY_ORDER = [
    "Batting", "Bowling", "Fielding", "Mental", "Awareness", "Special", "Elite",
]

TRAIT_BY_EFFECT = {t["effect_key"]: t for t in TRAIT_DEFINITIONS}


def trait_meta(effect_key):
    """The catalogue entry for an effect key, or None if it isn't one of ours."""
    return TRAIT_BY_EFFECT.get(effect_key)


def trait_rarity_of(trait) -> str:
    """Rarity of a ``Trait`` row (or a catalogue dict), normalised.

    Reads the column when the database has one and falls back to the catalogue,
    so a row written before the rarity column existed still prices correctly.
    """
    from config import trait_rarity_key
    if trait is None:
        return trait_rarity_key(None)
    if isinstance(trait, dict):
        return trait_rarity_key(trait.get("rarity"))
    rarity = getattr(trait, "rarity", None)
    if not rarity:
        meta = trait_meta(getattr(trait, "effect_key", None))
        rarity = meta.get("rarity") if meta else None
    return trait_rarity_key(rarity)


def trait_price_of(trait) -> int:
    """Gems for a fresh Lv.1 copy — the admin's pinned price, else the rarity's."""
    from config import trait_list_price
    pinned = getattr(trait, "base_price", None) if not isinstance(trait, dict) else None
    if pinned and int(pinned) > 0:
        return int(pinned)
    return trait_list_price(trait_rarity_of(trait))


def sorted_categories(categories):
    """Categories in catalogue order, unknown ones alphabetically at the end."""
    known = {c: i for i, c in enumerate(TRAIT_CATEGORY_ORDER)}
    return sorted(set(categories),
                  key=lambda c: (known.get(c, len(known)), str(c)))


# ═══════════════════════════════════════════════════════════════════════
# SHOP LOGIC
# ═══════════════════════════════════════════════════════════════════════

def _today_key() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def _get_or_create_daily(session, user_id):
    today = _today_key()
    daily = (session.query(TraitDaily)
             .filter(TraitDaily.user_id == user_id, TraitDaily.day_key == today)
             .first())
    if not daily:
        daily = TraitDaily(user_id=user_id, day_key=today, purchases=0, rerolls=0)
        session.add(daily)
        session.flush()
    return daily


def _clear_old_market(session, user_id):
    session.query(TraitMarket).filter(TraitMarket.user_id == user_id).delete()
    session.flush()


def refresh_shop(session, user_id, force=False):
    """Ensure user has 5-slot shop for today. Returns list of shop rows."""
    now = datetime.utcnow()
    rows = (session.query(TraitMarket)
            .filter(TraitMarket.user_id == user_id)
            .order_by(TraitMarket.slot_index).all())

    needs_refresh = force or not rows
    if not needs_refresh and rows:
        # Honour the website's trait-refresh interval so this legacy per-user
        # shop can never disagree with the shared one.
        try:
            from services.global_market import get_trait_refresh_interval_hours
            window = get_trait_refresh_interval_hours(session)
        except Exception:
            logger.exception("trait refresh interval lookup failed; using 24h")
            window = 24
        if now - rows[0].refreshed_at >= timedelta(hours=window):
            needs_refresh = True

    if not needs_refresh:
        return rows

    _clear_old_market(session, user_id)

    all_traits = session.query(Trait).filter(Trait.is_active == True).all()
    if not all_traits:
        return []
    n_slots = min(TRAIT_SHOP_SLOTS, len(all_traits))
    # Weighted by rarity, so this shop shows Elite as rarely as the shared one.
    from services.global_market import pick_traits_by_rarity
    chosen = pick_traits_by_rarity(all_traits, n_slots)

    discount_slot = random.randint(0, n_slots - 1)
    discount_pct = random.randint(TRAIT_DAILY_DISCOUNT_MIN, TRAIT_DAILY_DISCOUNT_MAX)

    new_rows = []
    for i, t in enumerate(chosen):
        # Price per trait, not one flat number: rarity is what a trait costs.
        base = trait_price_of(t)
        disc = discount_pct if i == discount_slot else 0
        final = base * (100 - disc) // 100
        row = TraitMarket(
            user_id=user_id, slot_index=i, trait_id=t.id,
            base_price=base, discount_pct=disc, final_price=final,
            purchased=False, refreshed_at=now,
        )
        session.add(row)
        new_rows.append(row)

    session.flush()
    return new_rows


def reroll_shop(session, user):
    if user.total_gems < TRAIT_REROLL_COST:
        return False, f"Not enough gems. Reroll costs {TRAIT_REROLL_COST} 💎."
    user.total_gems -= TRAIT_REROLL_COST
    refresh_shop(session, user.id, force=True)
    return True, f"Shop rerolled! -{TRAIT_REROLL_COST} 💎"


def buy_trait_from_shop(session, user, slot_index):
    daily = _get_or_create_daily(session, user.id)
    if daily.purchases >= TRAIT_SHOP_DAILY_PURCHASE_LIMIT:
        return False, f"Daily limit reached ({TRAIT_SHOP_DAILY_PURCHASE_LIMIT} purchases/day).", None

    row = (session.query(TraitMarket)
           .filter(TraitMarket.user_id == user.id,
                   TraitMarket.slot_index == slot_index).first())
    if not row:
        return False, "Shop slot not found. Run /traitshop first.", None
    if row.purchased:
        return False, "Already purchased this slot.", None
    if user.total_gems < row.final_price:
        return False, f"Need {row.final_price} 💎, you have {user.total_gems} 💎.", None

    user.total_gems -= row.final_price
    row.purchased = True
    daily.purchases += 1

    inv = TraitInventory(user_id=user.id, trait_id=row.trait_id, level=1)
    session.add(inv)
    session.flush()

    trait = session.query(Trait).get(row.trait_id)
    if not trait:
        session.rollback()
        return False, "Trait data is missing — please contact support.", None
    return True, f"Bought {trait.emoji} {trait.name} Lv.1 for {row.final_price} 💎!", inv


# ═══════════════════════════════════════════════════════════════════════
# APPLY / REPLACE / UPGRADE
# ═══════════════════════════════════════════════════════════════════════

def _count_same_category(session, roster_id, category):
    return (session.query(PlayerTrait)
            .join(Trait, PlayerTrait.trait_id == Trait.id)
            .filter(PlayerTrait.roster_id == roster_id,
                    Trait.category == category)
            .count())


def _squad_traits_query(session, user_id):
    """Equipped traits on a user's NON-career roster cards.

    The Career Player is excluded on purpose: it carries its own
    TRAIT_MAX_PER_PLAYER allowance outside the squad budget. ``is_career`` is
    NULL on rows predating the career feature, so the filter is written
    NULL-safe exactly like ``services.player_service.not_career``.
    """
    return (session.query(PlayerTrait)
            .join(UserRoster, PlayerTrait.roster_id == UserRoster.id)
            .join(Player, UserRoster.player_id == Player.id)
            .filter(PlayerTrait.user_id == user_id,
                    (Player.is_career == False) | (Player.is_career.is_(None))))  # noqa: E712


def count_squad_traits(session, user_id):
    """How many traits the user has equipped across their non-career squad."""
    return _squad_traits_query(session, user_id).count()


def squad_trait_budget(session, user_id):
    """``{used, max, full}`` for the squad-wide trait cap — for UI payloads."""
    used = count_squad_traits(session, user_id)
    return {"used": used, "max": TRAIT_MAX_PER_SQUAD,
            "full": used >= TRAIT_MAX_PER_SQUAD}


def _is_career_roster(session, roster):
    """True if a roster entry holds the user's Career Player."""
    player = session.query(Player).get(roster.player_id)
    return bool(player is not None and getattr(player, "is_career", False))


def _squad_cap_block(session, user, roster):
    """Message refusing an apply that would exceed the squad budget, or None.

    Career cards are exempt, so they never hit this. Anything else is refused
    once the non-career squad already holds TRAIT_MAX_PER_SQUAD traits.
    """
    if _is_career_roster(session, roster):
        return None
    if count_squad_traits(session, user.id) < TRAIT_MAX_PER_SQUAD:
        return None
    return (f"Squad trait limit reached ({TRAIT_MAX_PER_SQUAD}). Free a slot "
            f"with /removetrait — it's free and the trait keeps its level. "
            f"Your Career Player has its own {TRAIT_MAX_PER_PLAYER} slots on "
            f"top of this.")


def _elite_block(new_trait, other_traits):
    """Message refusing a second Elite trait on one player, or None.

    ``other_traits`` is every trait that will still be on the player after this
    change — the equipped list for an apply, that list minus the slot being
    overwritten for a replace.
    """
    if trait_rarity_of(new_trait) != "elite":
        return None
    already = sum(1 for t in other_traits if trait_rarity_of(t) == "elite")
    if already < TRAIT_MAX_ELITE_PER_PLAYER:
        return None
    return (f"Only {TRAIT_MAX_ELITE_PER_PLAYER} ⭐ Elite trait per player. "
            f"Remove the one they're wearing first (/removetrait — it's free "
            f"and keeps its level).")


def get_player_traits(session, roster_id):
    return (session.query(PlayerTrait, Trait)
            .join(Trait, PlayerTrait.trait_id == Trait.id)
            .filter(PlayerTrait.roster_id == roster_id).all())


def apply_trait_to_player(session, user, inventory_id, roster_id):
    inv = session.query(TraitInventory).filter(
        TraitInventory.id == inventory_id,
        TraitInventory.user_id == user.id).first()
    if not inv:
        return False, "Trait not in your inventory."

    roster = session.query(UserRoster).filter(
        UserRoster.id == roster_id, UserRoster.user_id == user.id).first()
    if not roster:
        return False, "Player not in your roster."

    current = get_player_traits(session, roster_id)
    if len(current) >= TRAIT_MAX_PER_PLAYER:
        return False, (f"Player already has {TRAIT_MAX_PER_PLAYER} traits. "
                       f"Use /traitreplace to swap one.")

    trait = session.query(Trait).get(inv.trait_id)
    if not trait:
        return False, "Trait definition missing."

    for pt, t in current:
        if t.id == trait.id:
            return False, f"Player already has {trait.name}. Upgrade it instead."

    same_cat = _count_same_category(session, roster_id, trait.category)
    if same_cat >= TRAIT_MAX_SAME_CATEGORY:
        return False, (f"Max {TRAIT_MAX_SAME_CATEGORY} traits of same category "
                       f"({trait.category}) per player.")

    elite_block = _elite_block(trait, [t for _pt, t in current])
    if elite_block:
        return False, elite_block

    squad_block = _squad_cap_block(session, user, roster)
    if squad_block:
        return False, squad_block

    pt = PlayerTrait(
        user_id=user.id, roster_id=roster_id,
        trait_id=trait.id, level=inv.level,
    )
    session.add(pt)
    session.delete(inv)
    session.flush()

    player = session.query(Player).get(roster.player_id)
    player_name = player.name if player else f"Player #{roster.player_id}"
    return True, f"✨ Applied {trait.emoji} {trait.name} Lv.{pt.level} to {player_name}!"


def replace_trait_on_player(session, user, player_trait_id, inventory_id):
    if user.total_gems < TRAIT_REPLACE_COST:
        return False, f"Replace costs {TRAIT_REPLACE_COST} 💎."

    pt = session.query(PlayerTrait).filter(
        PlayerTrait.id == player_trait_id,
        PlayerTrait.user_id == user.id).first()
    if not pt:
        return False, "That trait isn't on any of your players."

    inv = session.query(TraitInventory).filter(
        TraitInventory.id == inventory_id,
        TraitInventory.user_id == user.id).first()
    if not inv:
        return False, "Replacement trait not in inventory."

    new_trait = session.query(Trait).get(inv.trait_id)
    other_traits = (session.query(PlayerTrait)
                    .filter(PlayerTrait.roster_id == pt.roster_id,
                            PlayerTrait.id != pt.id).all())
    for o in other_traits:
        if o.trait_id == new_trait.id:
            return False, f"Player already has {new_trait.name} in another slot."

    current_cat_count = 0
    remaining = []
    for o in other_traits:
        ot = session.query(Trait).get(o.trait_id)
        if not ot:
            continue
        remaining.append(ot)
        if ot.category == new_trait.category:
            current_cat_count += 1
    if current_cat_count >= TRAIT_MAX_SAME_CATEGORY:
        return False, (f"Would exceed {TRAIT_MAX_SAME_CATEGORY} {new_trait.category} "
                       f"traits on this player.")

    # The slot being overwritten is already excluded from ``other_traits``, so
    # swapping one Elite straight out for another is allowed.
    elite_block = _elite_block(new_trait, remaining)
    if elite_block:
        return False, elite_block

    # No TRAIT_MAX_PER_SQUAD check here on purpose: a replace overwrites an
    # existing PlayerTrait row in place, so the squad count is unchanged. A
    # captain sitting exactly on the cap can still reshape their squad.

    user.total_gems -= TRAIT_REPLACE_COST
    old_trait = session.query(Trait).get(pt.trait_id)
    pt.trait_id = new_trait.id
    pt.level = inv.level
    pt.acquired_at = datetime.utcnow()
    session.delete(inv)
    session.flush()

    return True, (f"🔄 Replaced {old_trait.emoji} {old_trait.name} with "
                  f"{new_trait.emoji} {new_trait.name} Lv.{pt.level}. "
                  f"-{TRAIT_REPLACE_COST} 💎")


# ═══════════════════════════════════════════════════════════════════════
# UNEQUIP — traits always come back to the owner's inventory
# ═══════════════════════════════════════════════════════════════════════

def return_traits_to_inventory(session, roster_ids):
    """Send every trait equipped on ``roster_ids`` back to its owner's inventory.

    This is the single helper for every path where a roster slot stops being the
    player it was — a sell/release, an undone buy, a trade, a claim/overflow
    replace, or the admin player purge. Each trait returns at the level it was
    equipped at (a Lv.4 trait comes back Lv.4) and goes to the user who owned it
    (``PlayerTrait.user_id``), which is the pre-trade owner on a swap.

    Returns the number of traits returned. The caller commits.
    """
    if isinstance(roster_ids, int):
        roster_ids = [roster_ids]
    ids = [int(r) for r in (roster_ids or []) if r]
    if not ids:
        return 0

    equipped = (session.query(PlayerTrait)
                .filter(PlayerTrait.roster_id.in_(ids)).all())
    for pt in equipped:
        session.add(TraitInventory(
            user_id=pt.user_id, trait_id=pt.trait_id, level=pt.level))
        session.delete(pt)
    if equipped:
        session.flush()
    return len(equipped)


def _player_name_for_roster(session, roster_id):
    row = (session.query(Player)
           .join(UserRoster, UserRoster.player_id == Player.id)
           .filter(UserRoster.id == roster_id).first())
    return row.name if row else "your player"


def remove_trait_from_player(session, user, player_trait_id):
    """Unequip one trait (/removetrait) and return it to inventory.

    Free — the trait keeps its level, so removing and re-applying never costs
    the user progress. Returns ``(ok, message)``; the caller commits.
    """
    pt = session.query(PlayerTrait).filter(
        PlayerTrait.id == player_trait_id,
        PlayerTrait.user_id == user.id).first()
    if not pt:
        return False, "That trait isn't equipped on any of your players."

    trait = session.query(Trait).get(pt.trait_id)
    if not trait:
        return False, "Trait definition missing."

    player_name = _player_name_for_roster(session, pt.roster_id)
    level = pt.level

    session.add(TraitInventory(
        user_id=user.id, trait_id=pt.trait_id, level=level))
    session.delete(pt)
    session.flush()

    return True, (f"📦 Removed {trait.emoji} {trait.name} Lv.{level} from "
                  f"<b>{html.escape(player_name)}</b> — it's back in your "
                  f"inventory.\n"
                  f"<i>Use /traitapply to attach it to another player.</i>")


def trim_squad_traits_to_cap(session, user, cap=TRAIT_MAX_PER_SQUAD):
    """Refund the least valuable equipped squad traits until the user is at ``cap``.

    Used by ``migrate_squad_and_trait_limits.py`` when TRAIT_MAX_PER_SQUAD is
    introduced and existing squads are over it. The trait is destroyed and the
    user is credited the full gems it cost to build (``config.trait_buy_value``)
    — a forced removal is not a sale, so no resale discount applies.

    Career Player traits are never candidates: they sit outside the squad
    budget (see :func:`_squad_traits_query`).

    Removal order is lowest level first, then lowest rarity, then lowest id —
    a stable key, because level alone ties constantly (see the note in
    ``services.trait_engine`` on why ordering by level is not deterministic).

    Returns ``{"removed": [...], "gems": int, "count": int}``. Caller commits.
    """
    from config import trait_buy_value, trait_rarity_key, TRAIT_RARITY_ORDER

    rows = (_squad_traits_query(session, user.id)
            .add_entity(Trait)
            .join(Trait, PlayerTrait.trait_id == Trait.id)
            .all())
    over = len(rows) - cap
    if over <= 0:
        return {"removed": [], "gems": 0, "count": 0}

    rows.sort(key=lambda rt: (
        rt[0].level or 1,
        TRAIT_RARITY_ORDER.index(trait_rarity_key(rt[1].rarity)),
        rt[0].id,
    ))

    removed, gems = [], 0
    for pt, trait in rows[:over]:
        refund = trait_buy_value(pt.level, trait.rarity)
        user.total_gems = (user.total_gems or 0) + refund
        gems += refund
        removed.append({"name": trait.name, "level": pt.level,
                        "rarity": trait_rarity_key(trait.rarity), "gems": refund})
        session.delete(pt)

    session.flush()
    try:
        from services.activity_service import log_activity
        log_activity(session, user.id, "trait_refund",
                     f"Squad trait limit ({cap}): refunded {len(removed)} "
                     f"trait(s) for {gems:,} gems",
                     gems_change=gems)
    except Exception:
        logger.exception("log_activity failed during trait trim (non-fatal)")

    return {"removed": removed, "gems": gems, "count": len(removed)}


def upgrade_player_trait(session, user, player_trait_id):
    pt = session.query(PlayerTrait).filter(
        PlayerTrait.id == player_trait_id,
        PlayerTrait.user_id == user.id).first()
    if not pt:
        return False, "Trait not found."
    if pt.level >= 5:
        return False, "Trait is already at Max Level (5)."
    cost = TRAIT_UPGRADE_COSTS.get(pt.level)
    if cost is None:
        return False, "Invalid upgrade path."
    if user.total_gems < cost:
        return False, f"Upgrade to Lv.{pt.level + 1} costs {cost} 💎, you have {user.total_gems} 💎."
    user.total_gems -= cost
    pt.level += 1
    session.flush()
    trait = session.query(Trait).get(pt.trait_id)
    extra = ""
    if pt.level == 5:
        extra = "\n🌟 <b>MAX LEVEL REACHED!</b> Badge + hidden bonus unlocked."
    return True, f"⬆️ {trait.emoji} {trait.name} → Lv.{pt.level}. -{cost} 💎{extra}"


def upgrade_inventory_trait(session, user, inventory_id):
    inv = session.query(TraitInventory).filter(
        TraitInventory.id == inventory_id,
        TraitInventory.user_id == user.id).first()
    if not inv:
        return False, "Inventory trait not found."
    if inv.level >= 5:
        return False, "Already at Max Level."
    cost = TRAIT_UPGRADE_COSTS.get(inv.level)
    if cost is None:
        return False, "Invalid."
    if user.total_gems < cost:
        return False, f"Need {cost} 💎, have {user.total_gems} 💎."
    user.total_gems -= cost
    inv.level += 1
    session.flush()
    trait = session.query(Trait).get(inv.trait_id)
    return True, f"⬆️ {trait.emoji} {trait.name} → Lv.{inv.level} (in inventory). -{cost} 💎"

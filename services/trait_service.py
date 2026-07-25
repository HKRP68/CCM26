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
    TRAIT_MAX_PER_PLAYER, TRAIT_MAX_SAME_CATEGORY,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# TRAIT DEFINITIONS — seeded into the `traits` table
# ═══════════════════════════════════════════════════════════════════════

TRAIT_DEFINITIONS = [
    # ── BATTING ──────────────────────────────────────────────────────
    {"name": "Finisher", "category": "Batting", "emoji": "🔥",
     "description": "Boosts 6s and 4s in the last 3 overs of the innings.",
     "effect_key": "bat_finisher"},
    {"name": "Power Hitter", "category": "Batting", "emoji": "💥",
     "description": "More 6s at the cost of higher wicket risk.",
     "effect_key": "bat_power_hitter"},
    {"name": "Anchor", "category": "Batting", "emoji": "⚓",
     "description": "Lower wicket chance but slightly fewer 6s.",
     "effect_key": "bat_anchor"},
    {"name": "Fast Starter", "category": "Batting", "emoji": "⚡",
     "description": "Boosts boundaries in the first 10 balls faced.",
     "effect_key": "bat_fast_starter"},
    {"name": "Clutch Player", "category": "Batting", "emoji": "🎯",
     "description": "Boosts boundaries when required run rate exceeds 8.",
     "effect_key": "bat_clutch"},

    # ── BOWLING ──────────────────────────────────────────────────────
    {"name": "Death Specialist", "category": "Bowling", "emoji": "☠️",
     "description": "Better economy & wickets in the last 3 overs.",
     "effect_key": "bowl_death"},
    {"name": "Wicket Hunter", "category": "Bowling", "emoji": "🏹",
     "description": "Higher wicket chance on every delivery.",
     "effect_key": "bowl_wicket_hunter"},
    {"name": "Dot Ball Specialist", "category": "Bowling", "emoji": "⚪",
     "description": "Higher dot ball chance on every delivery.",
     "effect_key": "bowl_dot_specialist"},
    {"name": "Powerplay King", "category": "Bowling", "emoji": "👑",
     "description": "Strong in the first 3 overs — more dots, more wickets.",
     "effect_key": "bowl_powerplay"},
    {"name": "Yorker Specialist", "category": "Bowling", "emoji": "🎯",
     "description": "Death-over accuracy — more wickets & dots in final 3 overs.",
     "effect_key": "bowl_yorker"},

    # ── FIELDING ─────────────────────────────────────────────────────
    {"name": "Safe Hands", "category": "Fielding", "emoji": "🧤",
     "description": "Lowers the chance of a dropped catch.",
     "effect_key": "field_safe_hands"},
    {"name": "Sniper Arm", "category": "Fielding", "emoji": "🎯",
     "description": "Raises run-out chance on quick singles.",
     "effect_key": "field_sniper"},

    # ── MENTAL ───────────────────────────────────────────────────────
    {"name": "Consistency King", "category": "Mental", "emoji": "📊",
     "description": "Trims extremes — fewer wickets but also fewer 6s.",
     "effect_key": "mental_consistency"},
    {"name": "Momentum Player", "category": "Mental", "emoji": "📈",
     "description": "Gains momentum — bonus scales from 0% to full over balls 1-30.",
     "effect_key": "mental_momentum"},
]


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
        if now - rows[0].refreshed_at >= timedelta(hours=24):
            needs_refresh = True

    if not needs_refresh:
        return rows

    _clear_old_market(session, user_id)

    all_traits = session.query(Trait).filter(Trait.is_active == True).all()
    if not all_traits:
        return []
    n_slots = min(TRAIT_SHOP_SLOTS, len(all_traits))
    chosen = random.sample(all_traits, n_slots)

    discount_slot = random.randint(0, n_slots - 1)
    discount_pct = random.randint(TRAIT_DAILY_DISCOUNT_MIN, TRAIT_DAILY_DISCOUNT_MAX)

    new_rows = []
    for i, t in enumerate(chosen):
        base = TRAIT_SHOP_BASE_PRICE
        disc = discount_pct if i == discount_slot else 0
        final = int(base * (100 - disc) / 100)
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
    for o in other_traits:
        ot = session.query(Trait).get(o.trait_id)
        if ot.category == new_trait.category:
            current_cat_count += 1
    if current_cat_count >= TRAIT_MAX_SAME_CATEGORY:
        return False, (f"Would exceed {TRAIT_MAX_SAME_CATEGORY} {new_trait.category} "
                       f"traits on this player.")

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

"""Pack service — opens packs, manages defaults, enforces costs and limits.

A pack defines:
  - A cost (coins/quest_points/gems)
  - One or more "main" players from a rating band, with optional weighting
  - N "bonus" players from a wider band, uniform random by default
  - Optional daily purchase limit

The default packs roughly correspond to:
  Pack 1 Bronze   - 700 QP        - 85-87 + 2x 74-80
  Pack 2 Silver   - 1000 QP       - 88-90 + 2x 75-82
  Pack 3 Star     - 790,000 c     - 89-92 + 2x 76-83
  Pack 4 Legend   - 1,850,000 c   - 91-95 + 2x 77-84
  Pack 5 Ultimate - 3,500,000 c   - 93-99 + 2x 78-85

Admin can create/edit/delete packs from the website.
"""

import json
import logging
import random
from datetime import datetime, timedelta
from sqlalchemy import func

from models import Pack, PackPurchase, UnopenedPack, Player, User, UserRoster

from services.player_service import not_career

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════
# Default pack seed data
# ════════════════════════════════════════════════════════════════════

DEFAULT_PACKS = [
    {
        "slot_number": 1,
        "name": "Bronze Pack",
        "description": "Entry-level pack. Random 85-87 rated player.",
        "emoji": "🥉",
        "cost_quest_points": 700,
        "cost_coins": 0,
        "cost_gems": 0,
        "main_filter_mode": "rating",
        "main_min_rating": 85, "main_max_rating": 87, "main_count": 1,
        "main_weights_json": json.dumps([60, 30, 10]),  # 85, 86, 87
        "main_versions_json": None,
        "bonus_min_rating": 74, "bonus_max_rating": 80, "bonus_count": 2,
    },
    {
        "slot_number": 2,
        "name": "Silver Pack",
        "description": "Mid-tier 88-90 rated player.",
        "emoji": "🥈",
        "cost_quest_points": 1000,
        "cost_coins": 0,
        "cost_gems": 0,
        "main_filter_mode": "rating",
        "main_min_rating": 88, "main_max_rating": 90, "main_count": 1,
        "main_weights_json": json.dumps([55, 30, 15]),
        "main_versions_json": None,
        "bonus_min_rating": 75, "bonus_max_rating": 82, "bonus_count": 2,
    },
    {
        "slot_number": 3,
        "name": "Star Pack",
        "description": "A guaranteed Star-version card (any rating).",
        "emoji": "⭐",
        "cost_quest_points": 0,
        "cost_coins": 790_000,
        "cost_gems": 0,
        "main_filter_mode": "version",
        "main_min_rating": 70, "main_max_rating": 99, "main_count": 1,
        "main_weights_json": None,
        "main_versions_json": json.dumps(["Star", "Star Card", "Star Player"]),
        "bonus_min_rating": 76, "bonus_max_rating": 83, "bonus_count": 2,
    },
    {
        "slot_number": 4,
        "name": "Legend Pack",
        "description": "A Legend-version card rated 91-95.",
        "emoji": "🌟",
        "cost_quest_points": 0,
        "cost_coins": 1_850_000,
        "cost_gems": 0,
        "main_filter_mode": "both",
        "main_min_rating": 91, "main_max_rating": 95, "main_count": 1,
        "main_weights_json": json.dumps([35, 25, 20, 12, 8]),
        "main_versions_json": json.dumps(["Legend", "Legend Card", "Legend Player"]),
        "bonus_min_rating": 77, "bonus_max_rating": 84, "bonus_count": 2,
    },
    {
        "slot_number": 5,
        "name": "Ultimate Legend Pack",
        "description": "A Legend-version card rated 96-100. The pinnacle.",
        "emoji": "💎",
        "cost_quest_points": 0,
        "cost_coins": 3_500_000,
        "cost_gems": 0,
        "main_filter_mode": "both",
        "main_min_rating": 96, "main_max_rating": 100, "main_count": 1,
        "main_weights_json": json.dumps([35, 25, 20, 12, 8]),
        "main_versions_json": json.dumps(["Legend", "Legend Card", "Legend Player"]),
        "bonus_min_rating": 78, "bonus_max_rating": 85, "bonus_count": 2,
    },
]


def seed_default_packs(session):
    """Insert default packs if they don't exist. Idempotent."""
    # One query for every slot rather than one per pack — this runs on every
    # boot, and each round trip is bot downtime on a restart.
    slots = [p["slot_number"] for p in DEFAULT_PACKS]
    taken = {s for (s,) in session.query(Pack.slot_number)
             .filter(Pack.slot_number.in_(slots))}
    inserted = 0
    for pdata in DEFAULT_PACKS:
        if pdata["slot_number"] in taken:
            continue
        p = Pack(**pdata)
        session.add(p)
        inserted += 1
    session.flush()
    return inserted


# ════════════════════════════════════════════════════════════════════
# Pack opening
# ════════════════════════════════════════════════════════════════════

# Packs never consult the website's blocked rating ranges. A pack is bought,
# earned or granted, and its band is a promise: a 92-99 pack owes a 92-99 card
# even while 92-99 is blocked from /claim, /daily and /gspin. See
# :mod:`services.rating_block_service` — its source list has no entry a pack
# could match, so there is nothing here to filter with.


def _weighted_pick_rating(min_r, max_r, weights_json):
    """Pick a rating in [min_r, max_r] using the JSON weights, or uniform."""
    ratings = list(range(min_r, max_r + 1))
    if not ratings:
        return min_r

    weights = None
    if weights_json:
        try:
            parsed = json.loads(weights_json)
            if isinstance(parsed, list) and len(parsed) == len(ratings):
                weights = parsed
            else:
                logger.warning(f"Bad weights_json: {weights_json!r}, using uniform")
        except (ValueError, TypeError):
            logger.warning(f"Bad weights_json: {weights_json!r}, using uniform")

    if weights is not None and sum(weights) > 0:
        return random.choices(ratings, weights=weights, k=1)[0]
    return random.choice(ratings)


def _pick_main_player(session, pack, *, exclude_player_ids=None,
                       user_owned_base_ids=None, force_rating=None):
    """Pick a player for the MAIN slot honoring pack.main_filter_mode.

    For 'rating': pick a rating from the band, then a base-card at that rating.
    For 'version': pick any active player whose version matches one of the
                   configured names.
    For 'both': filter by version AND rating range; pick from the matches.

    `force_rating`: if set, overrides the random rating pick. Used by the pack
    pity timer to guarantee a max-rating pull. Has no effect on version-only
    packs (no rating selection happens there anyway).
    """
    exclude = set(exclude_player_ids or [])
    owned = set(user_owned_base_ids or [])

    mode = (pack.main_filter_mode or "rating").lower()

    # Parse versions list
    versions = []
    if pack.main_versions_json:
        try:
            v = json.loads(pack.main_versions_json)
            if isinstance(v, list):
                versions = [str(s).strip() for s in v if str(s).strip()]
        except (ValueError, TypeError):
            pass

    if mode == "rating":
        rating = force_rating if force_rating is not None else _weighted_pick_rating(
            pack.main_min_rating, pack.main_max_rating, pack.main_weights_json)
        return _pick_base_at_rating(session, rating,
                                     exclude_player_ids=exclude,
                                     user_owned_base_ids=owned)

    if mode == "version":
        # Version-only packs ignore force_rating — there's no rating selection here.
        if not versions:
            logger.warning(f"Pack {pack.id} mode=version but no versions configured")
            return None
        # Case-insensitive match
        from sqlalchemy import func as _func
        lowered = [v.lower() for v in versions]
        pool = (not_career(session.query(Player))
                .filter(Player.is_active == True,
                        _func.lower(Player.version).in_(lowered))
                .all())
        pool = [p for p in pool if p.id not in exclude]
        if not pool:
            return None
        # Prefer not-owned families
        fresh = [p for p in pool
                 if (p.parent_player_id or p.id) not in owned]
        return random.choice(fresh) if fresh else random.choice(pool)

    if mode == "both":
        if not versions:
            # Fall back to rating mode if version list is empty
            logger.warning(f"Pack {pack.id} mode=both but versions empty; using rating only")
            return _pick_main_player(session, _ProxyPack(pack, mode="rating"),
                                     exclude_player_ids=exclude,
                                     user_owned_base_ids=owned)
        from sqlalchemy import func as _func
        lowered = [v.lower() for v in versions]
        rating = force_rating if force_rating is not None else _weighted_pick_rating(
            pack.main_min_rating, pack.main_max_rating, pack.main_weights_json)
        # Try exact rating first
        pool = (not_career(session.query(Player))
                .filter(Player.is_active == True,
                        Player.rating == rating,
                        _func.lower(Player.version).in_(lowered))
                .all())
        if not pool:
            # Widen to full band
            pool = (not_career(session.query(Player))
                    .filter(Player.is_active == True,
                            Player.rating.between(pack.main_min_rating, pack.main_max_rating),
                            _func.lower(Player.version).in_(lowered))
                    .all())
        pool = [p for p in pool if p.id not in exclude]
        if not pool:
            return None
        fresh = [p for p in pool
                 if (p.parent_player_id or p.id) not in owned]
        return random.choice(fresh) if fresh else random.choice(pool)

    # Unknown mode — fall back to rating
    return _pick_main_player(session, _ProxyPack(pack, mode="rating"),
                             exclude_player_ids=exclude,
                             user_owned_base_ids=owned)


class _ProxyPack:
    """Tiny proxy used to recursively call _pick_main_player with a mode override."""
    def __init__(self, src, mode):
        self.id = src.id
        self.main_filter_mode = mode
        self.main_min_rating = src.main_min_rating
        self.main_max_rating = src.main_max_rating
        self.main_weights_json = src.main_weights_json
        self.main_versions_json = src.main_versions_json


def _pick_base_at_rating(session, rating, *, exclude_player_ids=None,
                          user_owned_base_ids=None):
    """Pick a random BASE-CARD player at the given rating (legacy rating-mode helper)."""
    exclude = set(exclude_player_ids or [])
    owned = set(user_owned_base_ids or [])

    base_q = (not_career(session.query(Player))
              .filter(Player.is_active == True,
                      Player.parent_player_id.is_(None),
                      Player.rating == rating))
    pool = [p for p in base_q.all() if p.id not in exclude]
    if not pool:
        widened = (not_career(session.query(Player))
                   .filter(Player.is_active == True,
                           Player.parent_player_id.is_(None),
                           Player.rating.between(rating - 1, rating + 1))
                   .all())
        pool = [p for p in widened if p.id not in exclude]
    if not pool:
        return None
    fresh = [p for p in pool if p.id not in owned]
    return random.choice(fresh) if fresh else random.choice(pool)


def _pick_bonus_player(session, pack, *, exclude_player_ids=None,
                        user_owned_base_ids=None):
    """Bonus pick is always rating-based, base cards only."""
    rating = _weighted_pick_rating(
        pack.bonus_min_rating, pack.bonus_max_rating, pack.bonus_weights_json)
    return _pick_base_at_rating(session, rating,
                                 exclude_player_ids=exclude_player_ids,
                                 user_owned_base_ids=user_owned_base_ids)


def _pick_player_at_rating(session, rating, *, exclude_player_ids=None,
                           user_owned_base_ids=None):
    """Backwards-compat wrapper — kept for any external callers."""
    return _pick_base_at_rating(session, rating,
                                 exclude_player_ids=exclude_player_ids,
                                 user_owned_base_ids=user_owned_base_ids)


MAX_INVENTORY = 50  # max unopened packs a user can hold

# Pack pity timer — guarantees a max-rating pull after PITY_THRESHOLD
# consecutive "non-max" pulls. Resets to 0 on every max-rating roll
# (or when the guaranteed pull fires). Only applies to rating/both mode
# packs with min < max — single-rating packs and version-only packs skip pity.
PITY_THRESHOLD = 10


def _lock_buyer_row(session, user):
    """Row-lock the buyer for the rest of this transaction.

    ``buy_pack`` does check-then-charge: it validates the balance, the
    inventory cap and the daily limit, and only then deducts. Two buys that
    overlap — the classic double-tap on the Mini App "Buy" button, which fires
    a second POST before the first has committed — each read the same
    pre-deduction snapshot from their own session, both pass validation, and
    both charge. The user ends up with two packs for one price, or past the
    daily limit.

    ``SELECT … FOR UPDATE`` makes the second buy wait until the first commits,
    then re-read the charged balance. Real lock on Postgres, a harmless no-op
    on SQLite (dev/tests), where writes serialize anyway. Best-effort: a
    session that cannot lock (a test double, an exotic backend) must never
    stop a legitimate purchase.
    """
    try:
        # autoflush is off, so flush first — populate_existing() would
        # otherwise overwrite pending in-session writes to this User.
        session.flush()
        (session.query(User)
         .filter(User.id == user.id)
         .populate_existing()
         .with_for_update()
         .first())
    except Exception:
        logger.debug("buy_pack: buyer row lock unavailable", exc_info=True)


def buy_pack(session, user, pack):
    """Validate cost + limits, charge user, add an UnopenedPack to inventory.

    Does NOT roll players — that happens in open_unopened_pack().

    Concurrency: the buyer row is locked first (see ``_lock_buyer_row``) so
    duplicate/rapid purchases can't both spend the same balance.

    Returns dict:
      success: bool
      message: str (only on failure)
      cost_paid: int
      currency: str
      inventory_id: int (the UnopenedPack.id created)
    """
    _lock_buyer_row(session, user)

    if pack.cost_coins and user.total_coins < pack.cost_coins:
        return {"success": False,
                "message": f"❌ Need {pack.cost_coins:,} coins (you have {user.total_coins:,})."}
    if pack.cost_quest_points and (user.quest_points or 0) < pack.cost_quest_points:
        return {"success": False,
                "message": f"❌ Need {pack.cost_quest_points:,} quest points (you have {user.quest_points or 0:,})."}
    if pack.cost_gems and user.total_gems < pack.cost_gems:
        return {"success": False,
                "message": f"❌ Need {pack.cost_gems} gems (you have {user.total_gems})."}

    inv_count = (session.query(func.count(UnopenedPack.id))
                 .filter(UnopenedPack.user_id == user.id).scalar()) or 0
    if inv_count >= MAX_INVENTORY:
        return {"success": False,
                "message": f"❌ Your pack inventory is full ({MAX_INVENTORY}). Open some via /openpack first!"}

    if pack.daily_limit and pack.daily_limit > 0:
        since = datetime.utcnow() - timedelta(hours=24)
        bought_today = (session.query(func.count(PackPurchase.id))
                        .filter(PackPurchase.user_id == user.id,
                                PackPurchase.pack_id == pack.id,
                                PackPurchase.purchased_at >= since)
                        .scalar()) or 0
        if bought_today >= pack.daily_limit:
            return {"success": False,
                    "message": f"❌ Daily limit reached ({pack.daily_limit}/24h). Try again later."}

    cost_paid = 0
    currency = "free"
    if pack.cost_coins:
        user.total_coins -= pack.cost_coins
        cost_paid = pack.cost_coins; currency = "coins"
    elif pack.cost_quest_points:
        user.quest_points = (user.quest_points or 0) - pack.cost_quest_points
        cost_paid = pack.cost_quest_points; currency = "quest_points"
    elif pack.cost_gems:
        user.total_gems -= pack.cost_gems
        cost_paid = pack.cost_gems; currency = "gems"

    inv = UnopenedPack(user_id=user.id, pack_id=pack.id,
                       acquired_at=datetime.utcnow(), source="buypack")
    session.add(inv)

    # Audit row created at buy time. players_json filled later when opened.
    audit = PackPurchase(
        user_id=user.id, pack_id=pack.id,
        cost_paid=cost_paid, currency=currency,
        players_json=None, purchased_at=datetime.utcnow(),
    )
    session.add(audit)
    session.flush()

    return {"success": True, "cost_paid": cost_paid, "currency": currency,
            "inventory_id": inv.id}


def list_user_inventory(session, user_id):
    """Return [(UnopenedPack, Pack)] sorted oldest-first (FIFO)."""
    return (session.query(UnopenedPack, Pack)
            .join(Pack, Pack.id == UnopenedPack.pack_id)
            .filter(UnopenedPack.user_id == user_id)
            .order_by(UnopenedPack.acquired_at.asc(), UnopenedPack.id.asc())
            .all())


def open_unopened_pack(session, user, inventory_id):
    """Roll players, add to roster, delete inventory row."""
    inv = (session.query(UnopenedPack)
           .filter(UnopenedPack.id == inventory_id,
                   UnopenedPack.user_id == user.id).first())
    if not inv:
        return {"success": False, "message": "❌ Pack not found in your inventory."}
    pack = session.query(Pack).get(inv.pack_id)
    if not pack:
        session.delete(inv); session.flush()
        return {"success": False, "message": "❌ Pack definition no longer exists."}

    # Owned base_ids for dedupe
    owned_rosters = (session.query(UserRoster.player_id)
                     .filter(UserRoster.user_id == user.id).all())
    owned_player_ids = {r[0] for r in owned_rosters}
    owned_base_ids = set()
    if owned_player_ids:
        for p in (session.query(Player.id, Player.parent_player_id)
                  .filter(Player.id.in_(owned_player_ids)).all()):
            owned_base_ids.add(p.parent_player_id or p.id)

    rolled = []
    used_player_ids = set()

    # Pity check — applies only if this pack has a rating range with >1 step
    # and user has hit the threshold. We force a one-time override.
    pity_force_max = False
    mode = (pack.main_filter_mode or "rating").lower()
    rating_band = (pack.main_max_rating or 0) - (pack.main_min_rating or 0)
    pity_eligible = (mode in ("rating", "both")) and rating_band > 0
    if pity_eligible and (user.pack_pity_counter or 0) >= PITY_THRESHOLD:
        pity_force_max = True

    for i in range(pack.main_count or 1):
        # Force max rating on this pull when pity triggers, but only for the
        # first main pull (so packs with multi-main only get one bumped pick).
        force_rating = pack.main_max_rating if (pity_force_max and i == 0) else None
        p = _pick_main_player(session, pack,
                              exclude_player_ids=used_player_ids,
                              user_owned_base_ids=owned_base_ids,
                              force_rating=force_rating)
        if p:
            used_player_ids.add(p.id)
            rolled.append((p, "main"))
            owned_base_ids.add(p.parent_player_id or p.id)

    # Update pity counter based on the first main pull
    pity_was_triggered = False
    if pity_eligible and rolled:
        first_main = rolled[0][0]
        if pity_force_max:
            pity_was_triggered = True
            user.pack_pity_counter = 0
        elif first_main.rating >= (pack.main_max_rating or 0):
            user.pack_pity_counter = 0  # max-rating natural roll resets pity
        else:
            user.pack_pity_counter = (user.pack_pity_counter or 0) + 1

    for _ in range(pack.bonus_count or 0):
        p = _pick_bonus_player(session, pack,
                                exclude_player_ids=used_player_ids,
                                user_owned_base_ids=owned_base_ids)
        if p:
            used_player_ids.add(p.id)
            rolled.append((p, "bonus"))
            owned_base_ids.add(p.parent_player_id or p.id)

    if not rolled:
        return {"success": False,
                "message": "❌ Couldn't roll any players. Contact admin — your pack is still in inventory."}

    from config import MAX_ROSTER
    added = []; to_claim = []
    for player, slot_type in rolled:
        if user.roster_count < MAX_ROSTER:
            entry = UserRoster(
                user_id=user.id, player_id=player.id,
                order_position=user.roster_count + 1,
                acquired_date=datetime.utcnow(),
            )
            session.add(entry)
            user.roster_count += 1
            added.append((player, slot_type))
        else:
            to_claim.append(player)

    # Backfill the audit row created at buy time
    received = [{"player_id": p.id, "name": p.name, "rating": p.rating, "slot": s}
                for p, s in rolled]
    last_audit = (session.query(PackPurchase)
                  .filter(PackPurchase.user_id == user.id,
                          PackPurchase.pack_id == pack.id,
                          PackPurchase.players_json.is_(None))
                  .order_by(PackPurchase.purchased_at.asc()).first())
    if last_audit:
        last_audit.players_json = json.dumps(received)
    else:
        audit = PackPurchase(
            user_id=user.id, pack_id=pack.id,
            cost_paid=0, currency="opened",
            players_json=json.dumps(received),
            purchased_at=datetime.utcnow(),
        )
        session.add(audit)

    session.delete(inv)
    session.flush()

    return {"success": True, "pack": pack, "players": rolled,
            "added": added, "players_to_claim": to_claim,
            "pity_triggered": pity_was_triggered,
            "pity_counter": user.pack_pity_counter or 0}


def grant_pack(session, user_id, pack_id, *, source="admin"):
    """Add an UnopenedPack to a user's inventory (admin tool, no payment)."""
    inv = UnopenedPack(user_id=user_id, pack_id=pack_id,
                       acquired_at=datetime.utcnow(), source=source)
    session.add(inv); session.flush()
    return inv


def open_pack(session, user, pack):
    """Legacy one-shot: buy and immediately open. Used by older callers/tests."""
    buy_result = buy_pack(session, user, pack)
    if not buy_result["success"]:
        return buy_result
    open_result = open_unopened_pack(session, user, buy_result["inventory_id"])
    if open_result.get("success"):
        # Merge currency info from buy
        open_result["cost_paid"] = buy_result["cost_paid"]
        open_result["currency"] = buy_result["currency"]
    return open_result


def list_packs(session, *, only_active=True):
    q = session.query(Pack)
    if only_active:
        q = q.filter(Pack.is_active == True)
    return q.order_by(Pack.slot_number).all()


def get_user_pack_purchases_today(session, user_id, pack_id):
    """How many of this pack the user has bought in the last 24h."""
    since = datetime.utcnow() - timedelta(hours=24)
    return (session.query(func.count(PackPurchase.id))
            .filter(PackPurchase.user_id == user_id,
                    PackPurchase.pack_id == pack_id,
                    PackPurchase.purchased_at >= since)
            .scalar()) or 0


def count_main_pool(session, pack):
    """Return count of players that match the pack's main filter.
    Used in admin to warn if a pack has 0 matching players."""
    mode = (pack.main_filter_mode or "rating").lower()
    versions = []
    if pack.main_versions_json:
        try:
            v = json.loads(pack.main_versions_json)
            if isinstance(v, list):
                versions = [str(s).strip().lower() for s in v if str(s).strip()]
        except (ValueError, TypeError):
            pass

    if mode == "rating":
        return (not_career(session.query(Player))
                .filter(Player.is_active == True,
                        Player.parent_player_id.is_(None),
                        Player.rating.between(pack.main_min_rating, pack.main_max_rating))
                .count())
    if mode == "version":
        if not versions: return 0
        from sqlalchemy import func as _func
        return (not_career(session.query(Player))
                .filter(Player.is_active == True,
                        _func.lower(Player.version).in_(versions))
                .count())
    if mode == "both":
        if not versions: return 0
        from sqlalchemy import func as _func
        return (not_career(session.query(Player))
                .filter(Player.is_active == True,
                        Player.rating.between(pack.main_min_rating, pack.main_max_rating),
                        _func.lower(Player.version).in_(versions))
                .count())
    return 0


def count_bonus_pool(session, pack):
    """Players matching the bonus rating range (base cards only)."""
    return (not_career(session.query(Player))
            .filter(Player.is_active == True,
                    Player.parent_player_id.is_(None),
                    Player.rating.between(pack.bonus_min_rating, pack.bonus_max_rating))
            .count())

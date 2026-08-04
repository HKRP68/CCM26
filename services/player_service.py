"""Player selection and value services.

Uses player_cache for random picks (zero egress for selection logic), then
fetches the single chosen ORM row by ID. Result: massive egress reduction.

Also home to :func:`purge_player_references`, the one place that knows every
table pointing at a ``players`` row — shared by the website's player delete and
by the Career Player delete so a new referencing table only has to be handled
once.
"""

import logging
import random
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from models import Player
from config import CLAIM_RARITY, get_buy_value, get_sell_value

logger = logging.getLogger(__name__)


def not_career(query):
    """Exclude Career Player cards from a shared-pool or browse query.

    A career card (/cmucareer) is a normal ``players`` row owned by exactly one
    user, so without this filter it could be claimed, packed, marketed or picked
    for a bot XI and end up in somebody else's squad. Apply it anywhere players
    are drawn at random or listed for everyone.

    ``is_career`` is NULL on rows written before the column existed, so match
    those too rather than relying on the backfill alone.
    """
    return query.filter(or_(Player.is_career.is_(False),
                            Player.is_career.is_(None)))


def get_random_player_by_rating_range(session: Session, low: int, high: int,
                                      source: str | None = "claim") -> Player | None:
    """Return a random active player within [low, high] rating.

    Implementation: uses in-memory cache to pick the ID, then fetches just that
    one row. Was previously fetching every player in range — wasteful.

    ``source`` names the reward path for the website's blocked rating ranges
    (see :mod:`services.rating_block_service`); pass None to skip the check for
    a draw that isn't a random reward. When blocks are in play the widening this
    normally delegates to the cache is done here instead, over allowed ratings
    only, so a widened search can never reach into a blocked band.
    """
    from services import player_cache

    if source:
        from services import rating_block_service
        if rating_block_service.blocked_ratings(session, source):
            return _pick_widening(session, low, high, source)

    pick = player_cache.get_random_in_rating_range(low, high)
    if not pick:
        return None
    # Fetch single ORM row (cheap — single row, indexed lookup)
    return session.get(Player, pick["id"])


def _pick_widening(session: Session, low: int, high: int, source: str) -> Player | None:
    """Cache-backed pick that widens outward but never past a blocked rating.

    Mirrors ``player_cache.get_random_in_rating_range``'s widen-then-give-up
    behaviour, minus the "any active player" last resort, which would hand back
    exactly the ratings the block rules exist to withhold.
    """
    for expand in range(0, 11):
        band_low = max(50, low - expand)
        band_high = min(100, high + expand)
        player = _pick_in_range_strict(session, band_low, band_high, source)
        if player:
            return player
    return None


def _get_rarity_distribution(session: Session):
    """Return list of (cumulative_threshold, low, high) tuples.

    Tries the admin-configurable ClaimRarityTier table first.
    Falls back to CLAIM_RARITY from config.py if no rows exist.
    """
    try:
        from models import ClaimRarityTier
        rows = (session.query(ClaimRarityTier)
                .filter(ClaimRarityTier.is_active == True)
                .order_by(ClaimRarityTier.sort_order, ClaimRarityTier.id).all())
        if rows:
            # Build cumulative thresholds
            total = sum(r.probability for r in rows)
            if total <= 0:
                return CLAIM_RARITY
            # Normalize probabilities so they sum to 1.0 (allows admin to use percentages)
            cumulative = 0.0
            out = []
            for r in rows:
                cumulative += (r.probability / total)
                out.append((cumulative, r.rating_min, r.rating_max))
            return out
    except Exception:
        pass
    return CLAIM_RARITY


def _pick_in_range_strict(session: Session, low: int, high: int,
                          source: str | None = "claim") -> Player | None:
    """Pick a random active player strictly inside [low, high] — no widening.

    Unlike get_random_player_by_rating_range (which progressively widens the
    band and ultimately returns ANY active player when the band is empty), this
    helper never leaks outside the requested band. That keeps the website-set
    rarity weights authoritative for /claim and /daily.

    Ratings the website has blocked for ``source`` are skipped, so a band that
    is half blocked still pays out from the half that isn't.
    """
    from services import player_cache
    ratings = range(low, high + 1)
    if source:
        from services import rating_block_service
        ratings = rating_block_service.allowed_ratings(session, low, high, source)
    pool = []
    for r in ratings:
        pool.extend(player_cache.get_by_rating(r))
    if not pool:
        return None
    pick = random.choice(pool)
    return session.get(Player, pick["id"])


def _rarity_bands(session: Session):
    """The configured distribution as ``[weight, low, high]`` rows.

    Shared by the live draw and the website's simulator so both read the rarity
    table the same way.
    """
    dist = _get_rarity_distribution(session)
    bands = []
    prev = 0.0
    for threshold, low, high in dist or []:
        bands.append([max(0.0, threshold - prev), low, high])
        prev = threshold
    return bands


def simulate_rarity_draws(session: Session, draws: int = 10000,
                          source: str = "claim") -> dict:
    """Model ``draws`` pulls and report where they land — no player rows fetched.

    The website uses this to preview a rarity change. It reproduces the live
    draw exactly (same weights, same "empty band re-rolls into another band"
    rule, same blocked ratings) but reads pool sizes from the player cache
    instead of loading a card per pull, so a run large enough to show a
    0.00001% tier is still one page load.

    Returns ``{"bands": [{low, high, weight, pool, draws}], "misses": int}``,
    where ``misses`` counts pulls that found no eligible card anywhere.
    """
    from services import player_cache, rating_block_service

    bands = _rarity_bands(session)
    if not bands:
        return {"bands": [], "misses": draws}

    blocked = rating_block_service.blocked_ratings(session, source) if source else frozenset()

    # Pool sizes per band, blocked ratings excluded. A band's pool never changes
    # mid-run, so "is this band empty?" is answered once rather than per draw.
    live = []
    for weight, low, high in bands:
        ratings = [r for r in range(low, high + 1) if r not in blocked]
        pool = sum(len(player_cache.get_by_rating(r)) for r in ratings)
        live.append({"low": low, "high": high, "weight": weight,
                     "pool": pool, "draws": 0})

    eligible = [b for b in live if b["pool"] > 0 and b["weight"] > 0]
    if not eligible:
        # Mirror the live draw's fallback: zero-weight bands still get used
        # rather than failing the pull outright.
        eligible = [b for b in live if b["pool"] > 0]
    if not eligible:
        return {"bands": live, "misses": draws}

    # random.choices draws the whole run in one C-level call, which is what
    # makes a million-pull preview cheap enough to render on a page load.
    weights = [b["weight"] for b in eligible]
    if sum(weights) <= 0:
        weights = [1.0] * len(eligible)
    for band in random.choices(eligible, weights=weights, k=draws):
        band["draws"] += 1

    return {"bands": live, "misses": 0}


def get_random_player_by_rarity(session: Session) -> Player | None:
    """Pick a random player using the website Claim Rarity distribution.

    Uses the admin-configured ClaimRarityTier rows when present (otherwise the
    CLAIM_RARITY fallback). The chosen band is honoured strictly: if it has no
    eligible players we re-roll among the OTHER configured bands (weighted by
    their probabilities) rather than widening into arbitrary ratings, so the
    rarity set on the website is the rarity players actually receive.

    Ratings blocked for the ``claim`` source are removed from every band before
    the draw. A band with nothing left behaves exactly like an empty one — the
    draw re-rolls into another configured band instead of awarding a blocked
    card.
    """
    dist = _get_rarity_distribution(session)
    if not dist:
        return get_random_player_by_rating_range(session, 50, 58)

    # Convert cumulative thresholds back to per-band weights.
    bands = []
    prev = 0.0
    for threshold, low, high in dist:
        bands.append([max(0.0, threshold - prev), low, high])
        prev = threshold

    # Weighted draw without replacement: respect the configured rarity, but if a
    # band is empty fall through to the next-most-likely configured band.
    remaining = [b for b in bands if b[0] > 0] or [list(b) for b in bands]
    while remaining:
        total = sum(b[0] for b in remaining)
        if total <= 0:
            break
        roll = random.random() * total
        acc = 0.0
        chosen = remaining[-1]
        for band in remaining:
            acc += band[0]
            if roll <= acc:
                chosen = band
                break
        remaining.remove(chosen)
        player = _pick_in_range_strict(session, chosen[1], chosen[2])
        if player:
            return player

    # Nothing in any configured band (shouldn't happen) — last-resort widening
    # so /claim and /daily never fail outright.
    _, low, high = dist[-1]
    return get_random_player_by_rating_range(session, low, high)


def get_player_values(rating: int) -> tuple[int, int]:
    """Return (buy_value, sell_value) for a rating."""
    return get_buy_value(rating), get_sell_value(rating)


def get_players_for_debut(session: Session) -> list[Player]:
    """Return a balanced 11-player starter squad.

    The squad always targets 5 batsmen, 3 bowlers, 2 all-rounders and one
    wicket keeper. One randomly selected role receives an 83-85 OVR card;
    the other ten cards are 72-80 OVR. If a role-specific pool is short, no
    partial squad is returned: an admin should fix the player seed instead.
    """
    role_slots = [
        "Batsman", "Batsman", "Batsman", "Batsman", "Batsman",
        "Bowler", "Bowler", "Bowler",
        "All-rounder", "All-rounder",
        "Wicket Keeper",
    ]
    star_slot = random.randrange(len(role_slots))
    result: list[Player] = []
    seen_ids: set[int] = set()

    def pick_one(low: int, high: int, category: str | None = None) -> Player | None:
        query = not_career(session.query(Player).filter(
            and_(Player.rating >= low, Player.rating <= high,
                 Player.is_active == True, ~Player.id.in_(seen_ids))
        ))
        if category:
            query = query.filter(Player.category == category)
        pool = query.all()
        if not pool:
            return None
        player = random.choice(pool)
        seen_ids.add(player.id)
        return player

    for index, category in enumerate(role_slots):
        low, high = ((83, 85) if index == star_slot else (72, 80))
        player = pick_one(low, high, category)
        if not player:
            # Do not grant an unbalanced squad. A partial seed should be fixed
            # by an admin instead of silently giving a new user the wrong XI.
            return []
        result.append(player)

    return result


# ── Deletion ────────────────────────────────────────────────────────────────

def renumber_roster(session, user_id):
    """Close any gaps in a user's ``order_position`` after rows are removed."""
    from sqlalchemy import asc
    from models import UserRoster

    remaining = (session.query(UserRoster)
                 .filter(UserRoster.user_id == user_id)
                 .order_by(asc(UserRoster.order_position).nullslast(),
                           UserRoster.acquired_date, UserRoster.id).all())
    for position, entry in enumerate(remaining, 1):
        if entry.order_position != position:
            entry.order_position = position


def purge_player_references(session, player, *, refund=True):
    """Remove every row that references ``player`` so the player row can be
    deleted without tripping a ForeignKeyViolation. Caller commits.

    Roster rows go either way — equipped traits return to their owner's
    inventory, and trade/captain pointers are cleared — because those are what
    actually block the delete. ``refund`` decides whether the owner is also paid
    the player's sell value and given a release activity row, which is right for
    a catalogue card pulled from under its owners but wrong for a Career Player
    (bought with gems, never with coins — see
    :func:`services.career_service.delete_career_player`, which refunds the gems
    itself). CASCADE tables (challenge_players, tournament_player_stats,
    fantasy_league_players, roster_overflow_claims) clean themselves up when the
    player row is finally deleted. Returns the number of owners refunded.
    """
    from models import (User, UserRoster, Trade,
                        PlayerGameStats, PlayerMarket, GlobalPlayerMarket,
                        BotTeamPlayer, PlayerFormHistory, PlayerImage,
                        PlayerMatchStats, FantasyPlayerScore, FantasyPick)
    from services.activity_service import log_activity

    pid = player.id
    name = player.name
    rating = player.rating

    # 1) Roster rows — refund owners, return traits, clear trade/captain refs.
    roster_rows = session.query(UserRoster).filter(UserRoster.player_id == pid).all()
    roster_ids = [r.id for r in roster_rows]
    refunded_users = 0
    touched_users = set()
    if roster_ids:
        # Equipped traits go back to the owner's inventory rather than vanishing.
        from services.trait_service import return_traits_to_inventory
        return_traits_to_inventory(session, roster_ids)
        # Null roster pointers held by trades (FK is NO ACTION → would block).
        for t in (session.query(Trade)
                    .filter((Trade.initiator_roster_id.in_(roster_ids)) |
                            (Trade.receiver_roster_id.in_(roster_ids))).all()):
            if t.initiator_roster_id in roster_ids:
                t.initiator_roster_id = None
            if t.receiver_roster_id in roster_ids:
                t.receiver_roster_id = None
        session.flush()
        for row in roster_rows:
            user = session.query(User).get(row.user_id)
            if user:
                touched_users.add(user.id)
                user.roster_count = max(0, (user.roster_count or 0) - 1)
                if user.captain_roster_id == row.id:
                    user.captain_roster_id = None
                if refund:
                    sv = get_sell_value(rating)
                    user.total_coins = (user.total_coins or 0) + sv
                    try:
                        log_activity(session, user.id, "release",
                                     f"Player '{name}' ({rating}) removed by admin "
                                     f"· refunded {sv:,}",
                                     coins_change=sv, player_name=name,
                                     player_rating=rating)
                    except Exception:
                        logger.exception("player purge activity log failed")
                    refunded_users += 1
            session.delete(row)
        session.flush()

    # 2) Trades that reference the player directly (player_id is NOT NULL, so it
    #    can't be nulled — drop the historical trade rows instead).
    (session.query(Trade)
       .filter((Trade.initiator_player_id == pid) | (Trade.receiver_player_id == pid))
       .delete(synchronize_session=False))

    # 3) Remaining blocking references (no CASCADE in the model).
    for Model in (PlayerGameStats, PlayerMarket, GlobalPlayerMarket, BotTeamPlayer,
                  PlayerFormHistory, PlayerImage, PlayerMatchStats,
                  FantasyPlayerScore, FantasyPick):
        session.query(Model).filter(Model.player_id == pid).delete(synchronize_session=False)

    # 4) Detach child variants (self-FK parent_player_id, no CASCADE).
    (session.query(Player)
       .filter(Player.parent_player_id == pid)
       .update({Player.parent_player_id: None}, synchronize_session=False))

    session.flush()
    for user_id in touched_users:
        renumber_roster(session, user_id)
    session.flush()
    return refunded_users

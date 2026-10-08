"""Automatic giveaways — keeps one rotating giveaway live at all times.

The giveaway sweeper (services/giveaway_scheduler.py) calls
:func:`ensure_auto_giveaway` on every tick. When the rotation is enabled and no
automatic giveaway is scheduled or running, the next one is created starting
now; the sweeper's normal start/end passes then announce it and draw it like
any other giveaway.

Rotation:
  * Prize type cycles through ``prize_cycle`` (default player → coins → gems).
  * A player prize takes the next rating off ``rating_ladder``. The default
    ladder climbs slowly from 83 and keeps falling back to 85-86, so a 91 comes
    round once per lap rather than four giveaways in.
  * The prize card is a random active, non-career base card at that rating
    (nearest rating if none), skipping cards given away recently.

Every automatic giveaway uses tiered winners (``auto_winners``) — the number of
winners grows with the number of entries.
"""

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

DEFAULT_LADDER = "83,84,85,86,85,86,87,85,86,88,85,86,87,89,85,86,90,86,87,91"
DEFAULT_CYCLE = "player,coins,gems"
PRIZE_TYPES = ("player", "coins", "gems")
RATING_MIN, RATING_MAX = 50, 100
# How many recent automatic player prizes a new pick avoids repeating.
RECENT_PRIZE_MEMORY = 10


# ── Parsing ──────────────────────────────────────────────────────────

def parse_ladder(text) -> list[int]:
    """'83, 84,85' → [83, 84, 85]. Raises ValueError on a bad entry."""
    out = []
    for part in str(text or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        r = int(part)
        if not RATING_MIN <= r <= RATING_MAX:
            raise ValueError(f"rating {r} is outside {RATING_MIN}-{RATING_MAX}")
        out.append(r)
    if not out:
        raise ValueError("the rating ladder is empty")
    return out


def parse_cycle(text) -> list[str]:
    """'player, coins,gems' → ['player', 'coins', 'gems']. Raises ValueError."""
    out = []
    for part in str(text or "").split(","):
        part = part.strip().lower()
        if not part:
            continue
        if part not in PRIZE_TYPES:
            raise ValueError(f"unknown prize type '{part}' (use {', '.join(PRIZE_TYPES)})")
        out.append(part)
    if not out:
        raise ValueError("the prize cycle is empty")
    return out


def ladder_of(cfg) -> list[int]:
    try:
        return parse_ladder(cfg.rating_ladder or DEFAULT_LADDER)
    except ValueError:
        return parse_ladder(DEFAULT_LADDER)


def cycle_of(cfg) -> list[str]:
    try:
        return parse_cycle(cfg.prize_cycle or DEFAULT_CYCLE)
    except ValueError:
        return parse_cycle(DEFAULT_CYCLE)


# ── Config ───────────────────────────────────────────────────────────

def get_config(session, *, for_update=False):
    """The single settings row, created with defaults on first use."""
    from models import GiveawayAutoConfig
    q = session.query(GiveawayAutoConfig).order_by(GiveawayAutoConfig.id)
    if for_update:
        q = q.with_for_update()
    cfg = q.first()
    if cfg is None:
        cfg = GiveawayAutoConfig(id=1, enabled=False, duration_hours=72,
                                 rating_ladder=DEFAULT_LADDER, ladder_index=0,
                                 prize_cycle=DEFAULT_CYCLE, cycle_index=0,
                                 coins_amount=50000, gems_amount=50,
                                 exclude_recent_winners=True)
        session.add(cfg)
        session.flush()
    return cfg


def next_prize_preview(cfg) -> dict:
    """What the next automatic giveaway will offer: {'type', 'rating'|'amount'}."""
    cycle = cycle_of(cfg)
    ptype = cycle[(cfg.cycle_index or 0) % len(cycle)]
    if ptype == "player":
        ladder = ladder_of(cfg)
        return {"type": "player", "rating": ladder[(cfg.ladder_index or 0) % len(ladder)]}
    amount = cfg.coins_amount if ptype == "coins" else cfg.gems_amount
    return {"type": ptype, "amount": int(amount or 0)}


# ── Player prize pick ────────────────────────────────────────────────

def _recent_prize_player_ids(session) -> set:
    from models import Giveaway
    rows = (session.query(Giveaway.prize_player_id)
            .filter(Giveaway.is_auto.is_(True), Giveaway.prize_player_id.isnot(None))
            .order_by(Giveaway.id.desc()).limit(RECENT_PRIZE_MEMORY).all())
    return {pid for (pid,) in rows}


def pick_prize_player(session, rating):
    """A random active, non-career card at ``rating`` — base cards first, then
    any edition, then the nearest ratings outward. Recently given cards are
    skipped while there is anything else to choose."""
    from sqlalchemy import func
    from models import Player
    from services.player_service import not_career

    recent = _recent_prize_player_ids(session)

    def _at(r, base_only, avoid_recent):
        q = (not_career(session.query(Player))
             .filter(Player.is_active.is_(True), Player.rating == r))
        if base_only:
            q = q.filter(Player.parent_player_id.is_(None))
        if avoid_recent and recent:
            q = q.filter(~Player.id.in_(recent))
        return q.order_by(func.random()).first()

    for spread in range(0, 4):
        for r in sorted({rating - spread, rating + spread}):
            if not RATING_MIN <= r <= RATING_MAX:
                continue
            for base_only, avoid_recent in ((True, True), (False, True),
                                            (True, False), (False, False)):
                p = _at(r, base_only, avoid_recent)
                if p is not None:
                    return p
    return None


# ── Creation ─────────────────────────────────────────────────────────

def _auto_live_exists(session) -> bool:
    from models import Giveaway
    return (session.query(Giveaway.id)
            .filter(Giveaway.is_auto.is_(True),
                    Giveaway.status.in_(("scheduled", "running")))
            .first()) is not None


def _next_number(session) -> int:
    from models import Giveaway
    return (session.query(Giveaway).filter(Giveaway.is_auto.is_(True)).count()) + 1


def create_next_auto_giveaway(session, cfg, now=None):
    """Create the next automatic giveaway from ``cfg`` and advance the rotation.

    Returns the new Giveaway (flushed, not committed), or None if no prize
    could be found (no cards at all). Caller commits.
    """
    from models import Giveaway
    from services.giveaway_service import MAX_TIERED_WINNERS

    now = now or datetime.utcnow()
    cycle = cycle_of(cfg)
    ptype = cycle[(cfg.cycle_index or 0) % len(cycle)]
    number = _next_number(session)

    prize_amount = 0
    prize_player_id = None
    if ptype == "player":
        ladder = ladder_of(cfg)
        rating = ladder[(cfg.ladder_index or 0) % len(ladder)]
        player = pick_prize_player(session, rating)
        if player is None:
            logger.warning("Auto giveaway: no card found near %s OVR — skipping "
                           "to the next prize type", rating)
            cfg.cycle_index = ((cfg.cycle_index or 0) + 1) % len(cycle)
            if len(cycle) > 1 and any(t != "player" for t in cycle):
                return create_next_auto_giveaway(session, cfg, now)
            return None
        prize_player_id = player.id
        title = f"🎁 Auto Giveaway #{number} — {player.rating} OVR Card"
        cfg.ladder_index = ((cfg.ladder_index or 0) + 1) % len(ladder)
    elif ptype == "coins":
        prize_amount = int(cfg.coins_amount or 0)
        title = f"🎁 Auto Giveaway #{number} — {prize_amount:,} Coins"
    else:
        prize_amount = int(cfg.gems_amount or 0)
        title = f"🎁 Auto Giveaway #{number} — {prize_amount:,} Gems"

    cfg.cycle_index = ((cfg.cycle_index or 0) + 1) % len(cycle)
    cfg.updated_at = now

    hours = max(1, int(cfg.duration_hours or 72))
    g = Giveaway(
        title=title[:120],
        prize_type=ptype,
        prize_amount=prize_amount,
        prize_player_id=prize_player_id,
        num_winners=MAX_TIERED_WINNERS,
        start_time=now,
        end_time=now + timedelta(hours=hours),
        status="scheduled",
        announce_target="groups",
        is_auto=True,
        auto_winners=True,
        created_by="auto",
    )
    session.add(g)
    session.flush()
    logger.info("Auto giveaway #%s created: %s", g.id, title)
    return g


def ensure_auto_giveaway(session, now=None):
    """Create the next automatic giveaway if the rotation is on and none is live.

    The settings row is locked first so two overlapping sweeps (a rolling
    deploy) can't both create one. Returns the created Giveaway or None.
    Commits on creation.
    """
    cfg = get_config(session)
    if not cfg.enabled:
        session.commit()
        return None
    cfg = get_config(session, for_update=True)
    if not cfg.enabled or _auto_live_exists(session):
        session.commit()
        return None
    g = create_next_auto_giveaway(session, cfg, now)
    session.commit()
    return g

"""Player Market service — daily 5-slot shop of 87+ rated players at 10% off."""

import random
import logging
from datetime import datetime, timedelta

from models import User, Player, UserRoster, PlayerMarket
from config import get_buy_value

logger = logging.getLogger(__name__)

# Configuration
PLAYER_MARKET_SLOTS = 5
PLAYER_MARKET_MIN_RATING = 87
PLAYER_MARKET_DISCOUNT = 0.10        # 10% off buy value
PLAYER_MARKET_REFRESH_HOURS = 24


def _clear_old_market(session, user_id):
    session.query(PlayerMarket).filter(PlayerMarket.user_id == user_id).delete()
    session.flush()


def get_or_refresh_market(session, user_id, force=False):
    """Ensure user has a 5-slot market for today. Returns list of (PlayerMarket, Player).

    Refreshes if:
      - User has no rows
      - Existing rows are >= 24h old
      - force=True
    """
    now = datetime.utcnow()
    rows = (session.query(PlayerMarket)
            .filter(PlayerMarket.user_id == user_id)
            .order_by(PlayerMarket.slot_index).all())

    needs_refresh = force or not rows
    if not needs_refresh and rows:
        if now - rows[0].refreshed_at >= timedelta(hours=PLAYER_MARKET_REFRESH_HOURS):
            needs_refresh = True

    if needs_refresh:
        _clear_old_market(session, user_id)
        # Pick 5 random 87+ active players
        candidates = (session.query(Player)
                      .filter(Player.rating >= PLAYER_MARKET_MIN_RATING,
                              Player.is_active == True)
                      .all())
        if not candidates:
            return []
        n_slots = min(PLAYER_MARKET_SLOTS, len(candidates))
        chosen = random.sample(candidates, n_slots)

        for i, p in enumerate(chosen):
            base = get_buy_value(p.rating)
            final = int(base * (1 - PLAYER_MARKET_DISCOUNT))
            row = PlayerMarket(
                user_id=user_id,
                slot_index=i + 1,
                player_id=p.id,
                base_price=base,
                final_price=final,
                purchased=False,
                refreshed_at=now,
            )
            session.add(row)
        session.flush()
        rows = (session.query(PlayerMarket)
                .filter(PlayerMarket.user_id == user_id)
                .order_by(PlayerMarket.slot_index).all())

    # Return (row, player) pairs
    pairs = []
    for r in rows:
        p = session.query(Player).get(r.player_id)
        if p:
            pairs.append((r, p))
    return pairs


def buy_from_player_market(session, user, slot_index):
    """Buy the player at the given slot. Returns (success, msg)."""
    row = (session.query(PlayerMarket)
           .filter(PlayerMarket.user_id == user.id,
                   PlayerMarket.slot_index == slot_index)
           .first())
    if not row:
        return False, "Slot not found. Run /playermarket again."
    if row.purchased:
        return False, "Already purchased this slot."

    player = session.query(Player).get(row.player_id)
    if not player:
        return False, "Player no longer available."

    # Roster full check
    from config import MAX_ROSTER
    if user.roster_count >= MAX_ROSTER:
        return False, f"Roster full ({MAX_ROSTER}). Release players first."

    # Coin check
    if user.total_coins < row.final_price:
        shortage = row.final_price - user.total_coins
        return False, f"Need {shortage:,} more coins."

    # Process buy
    user.total_coins -= row.final_price
    entry = UserRoster(
        user_id=user.id, player_id=player.id,
        order_position=user.roster_count + 1,
        acquired_date=datetime.utcnow(),
    )
    session.add(entry)
    user.roster_count += 1
    row.purchased = True

    return True, f"✅ Bought <b>{player.name}</b> ({player.rating} OVR) for {row.final_price:,} 🪙"

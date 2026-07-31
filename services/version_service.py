"""Player version helpers — manage Base + variant card editions.

A Player row is either:
  - A "base" card: parent_player_id is NULL
  - A "variant" of another base: parent_player_id points to the base

When a user searches or browses, we show the BASE card by default. The base
card's detail page shows a list of variants, and clicking one swaps the
current view to that variant.

Buy logic:
  - Buying a specific version (base or variant) only buys that exact version
  - Cannot buy ANY version if user already owns ANY version of that base
"""

import logging
from sqlalchemy.orm import Session

from models import Player, UserRoster

from services.player_service import not_career

logger = logging.getLogger(__name__)


def get_base_id(player_id, session=None):
    """Resolve to the base player_id. If already base, returns same id."""
    own = False
    if session is None:
        from database import get_session
        session = get_session()
        own = True
    try:
        p = session.query(Player).get(player_id)
        if not p:
            return player_id
        return p.parent_player_id or p.id
    finally:
        if own:
            session.close()


def get_all_versions(session: Session, base_id):
    """Return all rows that are editions of this player (including the base itself).

    Editions are discovered two ways so the version carousel stays reliable no
    matter how the data was created:
      1. Rows linked to the base via parent_player_id (the canonical variants).
      2. Other ACTIVE rows that share the same player name but were never linked
         (e.g. a same-named "IPL" card added as its own base, or an orphan
         variant). Without this, /buypl & /playerinfo would collapse to a single
         page and never show the ◀ Prev / Next ▶ buttons.

    Ordered: base first, then variants by id. Deduped by row id.
    """
    base = session.query(Player).get(base_id)
    if not base:
        return []
    # If the supplied id is actually a variant, walk up to its base
    if base.parent_player_id:
        parent = session.query(Player).get(base.parent_player_id)
        if parent:
            base = parent

    variants = (session.query(Player)
                .filter(Player.parent_player_id == base.id,
                        Player.is_active == True)
                .order_by(Player.id).all())

    ordered = [base] + variants
    seen = {p.id for p in ordered}

    # Merge in any other active rows with the same name that aren't linked.
    if base.name:
        for p in (session.query(Player)
                  .filter(Player.name == base.name,
                          Player.is_active == True,
                          Player.id != base.id)
                  .order_by(Player.id).all()):
            if p.id not in seen:
                ordered.append(p)
                seen.add(p.id)

    return ordered


def user_owns_any_version(session: Session, user_id, player_id):
    """True if the user has a roster entry for any edition of this player.

    Uses the same edition grouping as get_all_versions so the "can't own two
    editions" rule stays consistent across linked variants AND same-named rows.
    """
    base = session.query(Player).get(player_id)
    if not base:
        return False
    base_id = base.parent_player_id or base.id

    version_ids = [v.id for v in get_all_versions(session, base_id)]
    if not version_ids:
        version_ids = [player_id]

    return (session.query(UserRoster)
            .filter(UserRoster.user_id == user_id,
                    UserRoster.player_id.in_(version_ids)).first() is not None)


def list_base_players_only_query(session: Session):
    """Returns a query that filters to base cards only.
    Use this for /search, /myroster suggestions, etc."""
    return not_career(session.query(Player)).filter(Player.parent_player_id.is_(None))


def get_default_for_search(session: Session, name_query):
    """Search returns base players matching the name (variants hidden by default)."""
    q = (not_career(session.query(Player))
         .filter(Player.parent_player_id.is_(None),
                 Player.is_active == True,
                 Player.name.ilike(f"%{name_query}%")))
    return q.all()

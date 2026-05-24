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
    """Return all rows that are versions of this base (including the base itself).
    Ordered: base first, then variants by id.
    """
    base = session.query(Player).get(base_id)
    if not base:
        return []
    # If the supplied id is actually a variant, walk up to its base
    if base.parent_player_id:
        base = session.query(Player).get(base.parent_player_id)
        if not base:
            return []

    variants = (session.query(Player)
                .filter(Player.parent_player_id == base.id,
                        Player.is_active == True)
                .order_by(Player.id).all())
    return [base] + variants


def user_owns_any_version(session: Session, user_id, player_id):
    """True if the user has a roster entry for the base or any variant."""
    base = session.query(Player).get(player_id)
    if not base:
        return False
    base_id = base.parent_player_id or base.id

    # Get all version ids for this base
    version_ids = [base_id]
    for v in (session.query(Player.id)
              .filter(Player.parent_player_id == base_id).all()):
        version_ids.append(v[0])

    return (session.query(UserRoster)
            .filter(UserRoster.user_id == user_id,
                    UserRoster.player_id.in_(version_ids)).first() is not None)


def list_base_players_only_query(session: Session):
    """Returns a query that filters to base cards only.
    Use this for /search, /myroster suggestions, etc."""
    return session.query(Player).filter(Player.parent_player_id.is_(None))


def get_default_for_search(session: Session, name_query):
    """Search returns base players matching the name (variants hidden by default)."""
    q = (session.query(Player)
         .filter(Player.parent_player_id.is_(None),
                 Player.is_active == True,
                 Player.name.ilike(f"%{name_query}%")))
    return q.all()

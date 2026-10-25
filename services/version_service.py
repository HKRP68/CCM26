"""Player version helpers — manage Base + variant card editions.

A Player row is either:
  - A "base" card: parent_player_id is NULL
  - A "variant" of another base: parent_player_id points to the base

Main card types are Base, Legend and Star. A family uses the first available
type in that order as its root. Same-name rows are editions even if an older
import never saved their parent pointers. Career cards remain separate.

Buy logic:
  - Buying a specific version (base or variant) only buys that exact version
  - Cannot buy ANY version if user already owns ANY version of that base
"""

import logging
from collections import defaultdict
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from models import Player, UserRoster

from services.player_service import not_career

logger = logging.getLogger(__name__)

MAIN_VERSIONS = ("Base", "Legend", "Star")


def normalize_version(value):
    """Recognise legacy main-card labels without renaming special editions."""
    label = " ".join(str(value or "").split()) or "Base"
    for main in MAIN_VERSIONS:
        if label.lower() in (main.lower(), f"{main.lower()} card",
                             f"{main.lower()} player"):
            return main
    return label


def name_key(value):
    return (value or "").strip().lower()


def filter_version(query, label):
    """Filter by the card's edition label, independently of its family role."""
    label = normalize_version(label)
    value = func.lower(func.trim(Player.version))
    if label in MAIN_VERSIONS:
        main = label.lower()
        clause = value.in_((main, f"{main} card", f"{main} player"))
        if label == "Base":
            clause = or_(clause, Player.version.is_(None), value == "")
        return query.filter(clause)
    return query.filter(value == label.lower())


def main_card(cards):
    """Choose one stable family root, even when no Base edition exists."""
    def order(card):
        label = normalize_version(card.version)
        rank = MAIN_VERSIONS.index(label) if label in MAIN_VERSIONS else 3
        return rank, card.id
    return min(cards, key=order)


def version_groups(session):
    """All catalogue families, including inactive and unlinked imported cards."""
    groups = defaultdict(list)
    for card in not_career(session.query(Player)).order_by(Player.id).all():
        groups[name_key(card.name)].append(card)
    return groups


def sync_version_links(session, names=None):
    """Repair family pointers inside the caller's transaction. Never change stats.

    Run after an import has flushed, so row order and missing Base editions do
    not matter. ``names`` limits repairs to families touched by that import.
    """
    session.flush()
    query = not_career(session.query(Player))
    if names is not None:
        keys = {name_key(name) for name in names}
        if not keys:
            return 0
        query = query.filter(func.lower(func.trim(Player.name)).in_(keys))
    groups = defaultdict(list)
    for card in query.order_by(Player.id).all():
        groups[name_key(card.name)].append(card)
    changed = 0
    for cards in groups.values():
        root = main_card(cards)
        # Keep any older explicit link to a differently named family intact.
        if root.parent_player_id and root.parent_player_id not in {c.id for c in cards}:
            continue
        for card in cards:
            parent_id = None if card.id == root.id else root.id
            if card.parent_player_id != parent_id:
                card.parent_player_id = parent_id
                changed += 1
    return changed


def _family_cards(session, player):
    if player.is_career:
        return [player]
    # Follow legacy links safely; repaired families are always a single level.
    seen = set()
    while player.parent_player_id and player.id not in seen:
        seen.add(player.id)
        parent = session.get(Player, player.parent_player_id)
        if not parent or parent.is_career:
            break
        player = parent
    cards = not_career(session.query(Player)).filter(or_(
        func.lower(func.trim(Player.name)) == name_key(player.name),
        Player.id == player.id,
        Player.parent_player_id == player.id,
    )).order_by(Player.id).all()
    root = main_card(cards)
    return [root] + [c for c in cards if c.id != root.id]


def get_base_id(player_id, session=None):
    """Resolve to the base player_id. If already base, returns same id."""
    own = False
    if session is None:
        from database import get_session
        session = get_session()
        own = True
    try:
        p = session.get(Player, player_id)
        if not p:
            return player_id
        return _family_cards(session, p)[0].id
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
    base = session.get(Player, base_id)
    if not base:
        return []
    cards = _family_cards(session, base)
    return [cards[0]] + [c for c in cards[1:] if c.is_active]


def user_owns_any_version(session: Session, user_id, player_id):
    """True if the user has a roster entry for any edition of this player.

    Uses the same edition grouping as get_all_versions so the "can't own two
    editions" rule stays consistent across linked variants AND same-named rows.
    """
    base = session.get(Player, player_id)
    if not base:
        return False
    # Inactive editions still belong to their owners.
    version_ids = [v.id for v in _family_cards(session, base)]
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

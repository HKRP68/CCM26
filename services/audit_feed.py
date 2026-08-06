"""Turning audit rows into something an admin can read.

``pack_purchases`` and ``market_purchases`` are keyed by id, because that is
what an audit row should store. Rendered straight onto the admin pages that
gave you "user #84 bought pack #3, got
``[{"player_id": 91, "name": …}]``" — three ids and a blob of JSON, and no way
to tell at a glance who is pulling what out of the economy.

These helpers resolve those rows into names once, in one query per batch, so
the Packs and Markets tabs can say who opened which pack and exactly what came
out of it.
"""

from __future__ import annotations

import json
import logging
from typing import Iterable

logger = logging.getLogger(__name__)


def manager_label(user) -> str | None:
    """"lost_in_space14" / "Rohit" / "Delhi XI", or None if there is nothing.

    None is a real answer, not a failure: the admin pages pair the label with
    the numeric id, so a manager with no handle and no name still renders as
    something clickable.
    """
    if user is None:
        return None
    for value in (getattr(user, "username", None),
                  getattr(user, "first_name", None),
                  getattr(user, "team_name", None)):
        text = (value or "").strip()
        if text:
            return text
    return None


def users_by_id(session, user_ids: Iterable) -> dict:
    """{user_id: User} for a batch of audit rows — one query, not one per row."""
    from models import User
    ids = {int(i) for i in user_ids if i}
    if not ids:
        return {}
    return {u.id: u for u in session.query(User).filter(User.id.in_(ids)).all()}


def pack_pulls(players_json):
    """What a pack gave up: a list of ``{name, rating, slot}``.

    Returns **None** for a pack that has been bought but not opened. The pulls
    are only written when the manager opens it, so an empty list and "still
    sealed" are different states and the page shows them differently.

    A row whose JSON is unreadable degrades to an empty list: a corrupt audit
    row from some older format is worth a dash on the page, never a 500 on a
    tab an admin is trying to read.
    """
    if not players_json:
        return None
    try:
        parsed = json.loads(players_json)
    except (ValueError, TypeError):
        logger.debug("unreadable pack audit row", exc_info=True)
        return []
    if not isinstance(parsed, list):
        return []

    pulls = []
    for entry in parsed:
        if isinstance(entry, dict):
            pulls.append({
                "name": entry.get("name") or f"#{entry.get('player_id', '?')}",
                "rating": entry.get("rating"),
                "slot": (entry.get("slot") or "").lower(),
            })
        else:
            # An older audit format stored bare names.
            pulls.append({"name": str(entry), "rating": None, "slot": ""})
    return pulls

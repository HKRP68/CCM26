"""The batting order a player saves with /sbo — one source of truth.

A player arranges their line-up once (``/setbo``, ``/sbo`` or the Mini App XI
screen) and that order is what walks out to bat in **every** mode: /playmatch,
/vsbot, /letsplay, /lpbot, /wpm, /wsp, /sim, tour matches and Quick Match. The
order IS the roster's ``order_position`` — slots 1-11 are the Playing XI in
batting order, 12+ is the bench — so the Mini App and the chat commands edit the
same thing.

Two states, and only two:

* **Saved order** (``User.batting_order_set_at`` is set) — roster order 1-11 is
  used verbatim, and the mode does not ask again. Openers are slots 1 and 2; a
  wicket brings in the next player down.
* **No saved order** (the flag is NULL) — nobody ever chose a line-up, so the XI
  is sorted by batting rating (high → low). That is the "Auto order" the /sbo
  card promises, and it is what every mode now falls back to. ``/sbo auto``
  clears the flag and returns the mode to asking per match.

Modes never re-derive any of this themselves. They call ``ordered_xi_dicts`` (or
``effective_xi_pairs``), and the XI comes back in the right order with each
player dict carrying ``order_locked`` — a JSON-safe marker that survives the
match state, the innings swap and the engine adapters, so any code holding only
an XI can still tell whether the player chose the order or the game did.
"""

import logging

logger = logging.getLogger(__name__)

XI_SIZE = 11

#: Key stamped on every engine player dict of a saved (user-chosen) line-up.
ORDER_LOCK_KEY = "order_locked"


# ════════════════════════════════════════════════════════════════════
# The flag
# ════════════════════════════════════════════════════════════════════

def has_custom_order(user) -> bool:
    """True when this ``User`` row has a batting order the player arranged."""
    return getattr(user, "batting_order_set_at", None) is not None


def user_has_custom_order(session, user_id) -> bool:
    """Same, by id. Never raises — a failed lookup falls back to auto order."""
    from models import User
    try:
        row = (session.query(User.batting_order_set_at)
               .filter(User.id == user_id).first())
        return bool(row and row[0])
    except Exception:
        logger.exception("batting-order flag lookup failed (user %s)", user_id)
        return False


# ════════════════════════════════════════════════════════════════════
# Ordering
# ════════════════════════════════════════════════════════════════════

def _bat_key_pair(entry_player):
    _entry, player = entry_player
    return (int(getattr(player, "bat_rating", 0) or 0),
            int(getattr(player, "rating", 0) or 0),
            str(getattr(player, "name", "") or ""))


def _bat_key_dict(p):
    return (int(p.get("bat_rating") or 0),
            int(p.get("rating") or 0),
            str(p.get("name") or ""))


def sort_pairs_by_bat_rating(pairs):
    """(UserRoster, Player) pairs by batting rating, high → low."""
    return sorted(pairs, key=_bat_key_pair, reverse=True)


def sort_dicts_by_bat_rating(players):
    """Engine player dicts by batting rating, high → low."""
    return sorted(players, key=_bat_key_dict, reverse=True)


def _mark(players, locked):
    """Stamp ``order_locked`` on a list of engine player dicts (copies them)."""
    out = []
    for p in players:
        if isinstance(p, dict):
            q = dict(p)
            q[ORDER_LOCK_KEY] = bool(locked)
            out.append(q)
        else:
            out.append(p)
    return out


def order_pairs(pairs, custom: bool):
    """Put roster pairs into the effective batting order.

    ``custom`` → the list is already in the player's saved order, so it is
    returned untouched. Otherwise it is sorted by batting rating.
    """
    return list(pairs) if custom else sort_pairs_by_bat_rating(pairs)


def order_dicts(players, custom: bool):
    """Same for engine player dicts, stamping ``order_locked`` on each."""
    ordered = list(players) if custom else sort_dicts_by_bat_rating(players)
    return _mark(ordered, custom)


def is_order_locked(players) -> bool:
    """True when this XI (or batting order) is a line-up the player saved."""
    for p in players or []:
        if isinstance(p, dict):
            return bool(p.get(ORDER_LOCK_KEY))
    return False


# ════════════════════════════════════════════════════════════════════
# Roster → XI
# ════════════════════════════════════════════════════════════════════

def effective_xi_pairs(session, user_id, pairs=None):
    """Return ``(xi_pairs, bench_pairs, custom)`` for a user's roster.

    ``pairs`` is the full roster in ``order_position`` order; it is read with
    ``handlers.lineup._get_ordered_roster`` when not supplied. The XI comes back
    in the order it will actually bat in. The bench keeps roster order.
    """
    if pairs is None:
        from handlers.lineup import _get_ordered_roster
        pairs = _get_ordered_roster(session, user_id)
    custom = user_has_custom_order(session, user_id)
    xi = order_pairs(list(pairs[:XI_SIZE]), custom)
    return xi, list(pairs[XI_SIZE:]), custom


def ordered_xi_dicts(session, user_id, players):
    """Put an already-built XI of engine dicts into this user's batting order.

    ``players`` must have been read in ``order_position`` order (every caller
    does), so a saved line-up is already correct and only needs stamping.
    """
    return order_dicts(players, user_has_custom_order(session, user_id))


# ════════════════════════════════════════════════════════════════════
# Using the order during a match
# ════════════════════════════════════════════════════════════════════

def openers_from_order(batting_order):
    """The two players who open when the line-up is the player's own.

    Returns ``(striker, non_striker)``, or ``(None, None)`` if the XI is too
    short to open an innings.
    """
    order = list(batting_order or [])
    if len(order) < 2:
        return None, None
    return order[0], order[1]


def next_batsman_index(batting_order, bat_stats, striker_idx, non_striker_idx,
                       start=0):
    """Index of the next batter to walk in — the next one down the order.

    Skips anyone already out or already at the crease, so it stays correct after
    a retired/impact substitution reshuffles the list. Returns ``None`` when
    nobody is left (the innings is over).
    """
    order = batting_order or []
    stats = bat_stats or {}
    for i in range(max(0, int(start or 0)), len(order)):
        if i in (striker_idx, non_striker_idx):
            continue
        rid = (order[i] or {}).get("roster_id")
        st = stats.get(rid) or stats.get(str(rid)) or {}
        if st.get("out"):
            continue
        return i
    return None

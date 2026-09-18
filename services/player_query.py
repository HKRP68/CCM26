"""One filter over the master ``players`` catalogue, and one ``details_json``.

Two things in this repo were on their way to a third copy each, and both are
the kind of duplicate that fails silently:

**The player filter.** ``admin._filtered_master_players`` (the Challenge Data
page) matches a rating by *exact equality* and cannot filter by card version;
``admin.players_list`` (the Players page) has rating ranges and version
filtering but is wired directly to ``request.args``. The Franchise Auction's
pool builder needs the union of both — a rating *band* plus a version list is
most of what the proposal's pool builder is — so this module holds the superset
and the Challenge Data page now asks it for the same rows it always got.

**The ``details_json`` blob.** ``services.cipl_match.cp_to_player_dict`` reads
every rating and handedness a match is played with out of
``ChallengePlayer.details_json``, *not* out of the master ``players`` row. Two
places already wrote that key set by hand (``admin`` for a hand-added player,
``draft_service`` for a drafted one) and a third — the auction — would make a
drifted key mean a squad plays with the wrong numbers and nothing says so.
``challenge_details_json`` is that one key set.

Contract: session first, nothing here commits, no Flask and no Telegram
imports, so services and tests can both use it.
"""

import json

from sqlalchemy import or_

from models import Player
from services.player_service import not_career

# Distinguishes "the caller did not say" from "the caller said None".
_UNSET = object()


# ──────────────────────────────────────────────────────────────────────
# The filter
# ──────────────────────────────────────────────────────────────────────

# Every key this understands, so a caller can build an empty filter dict and a
# form can be checked against it.
FILTER_KEYS = (
    "q", "country", "role", "bat_hand", "bowl_hand", "bowl_style",
    "rating", "rating_min", "rating_max",
    "bat_rating", "bat_rating_min", "bat_rating_max",
    "bowl_rating", "bowl_rating_min", "bowl_rating_max",
    "version_mode", "versions",
)


def empty_filters():
    """A filter dict with every key present and nothing selected."""
    blank = {key: "" for key in FILTER_KEYS}
    blank["versions"] = []
    return blank


def _int_or_none(raw):
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def master_player_query(session, filters=None):
    """A query over active, non-career master cards, narrowed by ``filters``.

    Returns a Query rather than a list so a caller can count it, page it, or
    take only the ids. Career cards and inactive rows are excluded here rather
    than by each caller — a career card reaching a shared pool is how it ends
    up in somebody else's squad (``player_service.not_career``).

    Unknown or unparseable values are **ignored rather than raising**: these
    come from a query string an admin is typing into, and a half-typed rating
    should narrow nothing, not 500 the page.
    """
    filters = filters or {}
    query = not_career(session.query(Player).filter(Player.is_active == True))  # noqa: E712

    text = str(filters.get("q") or "").strip()
    if text:
        query = query.filter(Player.name.ilike(f"%{text}%"))

    for key, column in (("country", Player.country),
                        ("role", Player.category),
                        ("bat_hand", Player.bat_hand),
                        ("bowl_hand", Player.bowl_hand),
                        ("bowl_style", Player.bowl_style)):
        value = str(filters.get(key) or "").strip()
        if value:
            query = query.filter(column == value)

    # An exact value and a band are both accepted for each rating. The exact
    # form is what the Challenge Data page has always posted; the band is what
    # the auction's pool builder needs. Passing both is not an error — the
    # exact value simply wins, because it is the narrower statement.
    for key, column in (("rating", Player.rating),
                        ("bat_rating", Player.bat_rating),
                        ("bowl_rating", Player.bowl_rating)):
        exact = _int_or_none(filters.get(key))
        if exact is not None:
            query = query.filter(column == exact)
            continue
        low = _int_or_none(filters.get(f"{key}_min"))
        high = _int_or_none(filters.get(f"{key}_max"))
        # A band typed backwards is a slip, not a request for no players.
        if low is not None and high is not None and low > high:
            low, high = high, low
        if low is not None:
            query = query.filter(column >= low)
        if high is not None:
            query = query.filter(column <= high)

    query = _apply_version_filter(query, filters)
    return query


def _apply_version_filter(query, filters):
    """``version_mode``: '' (any), 'base' (base cards only), or 'list'.

    'base' means a card with no parent — the original of a cricketer, whatever
    its ``version`` string happens to say — which is the same rule
    ``admin.players_list`` applies, and it is the one that actually keeps two
    editions of one player out of a single pool.
    """
    mode = str(filters.get("version_mode") or "").strip().lower()
    if mode == "base":
        return query.filter(or_(Player.parent_player_id.is_(None),
                                Player.parent_player_id == 0))
    wanted = filters.get("versions") or []
    if isinstance(wanted, str):
        wanted = [part.strip() for part in wanted.split(",")]
    wanted = [str(v).strip() for v in wanted if str(v).strip()]
    if wanted:
        return query.filter(Player.version.in_(wanted))
    return query


def ordered(query):
    """The catalogue's house ordering: best first, then name, then edition."""
    return query.order_by(Player.rating.desc(), Player.name.asc(),
                          Player.version.asc())


def filter_options(session):
    """Distinct values for the dropdowns, plus every version in the catalogue."""
    def values(column):
        rows = (session.query(column).filter(column.isnot(None))
                .distinct().order_by(column).all())
        return [row[0] for row in rows if row[0]]

    return {
        "countries": values(Player.country),
        "roles": values(Player.category),
        "bat_hands": values(Player.bat_hand),
        "bowl_hands": values(Player.bowl_hand),
        "bowl_styles": values(Player.bowl_style),
        "versions": values(Player.version),
        "ratings": list(range(100, 29, -1)),
    }


# ──────────────────────────────────────────────────────────────────────
# The details_json contract
# ──────────────────────────────────────────────────────────────────────

def challenge_details_json(player, is_overseas=False, extra=None,
                           source_player_id=_UNSET):
    """The ``details_json`` a ``ChallengePlayer`` carries, from a master card.

    The keys through ``is_overseas`` are the contract
    ``services.cipl_match.cp_to_player_dict`` reads. Anything in ``extra`` is
    appended after them and ignored by every existing reader — which is how the
    draft carries ``tier`` / ``icon_eligible`` / ``gender`` along without
    touching the part the match engine depends on.

    ``player`` is duck-typed: a master ``Player`` row and an ``AuctionLot``
    snapshot both satisfy it, which is the point — the auction must publish
    through exactly this door, not through a second copy of these keys.

    ``source_player_id`` must be passed explicitly for anything that is not a
    master ``Player``. An ``AuctionLot``'s own ``id`` is the *lot* id, and
    defaulting to it would write a blob pointing at the wrong card — so the
    default is a sentinel and only a real ``Player`` falls back to ``.id``.
    """
    if source_player_id is _UNSET:
        source_player_id = (getattr(player, "source_player_id", None)
                            if hasattr(player, "source_player_id")
                            else getattr(player, "id", None))
    blob = {
        "source_player_id": source_player_id,
        "name": player.name,
        "version": getattr(player, "version", None) or "Base",
        "country": player.country,
        "category": player.category,
        "role": player.category,
        "rating": player.rating,
        "bat_rating": player.bat_rating or 0,
        "bowl_rating": player.bowl_rating or 0,
        "bat_hand": player.bat_hand,
        "bowl_hand": player.bowl_hand,
        "bowl_style": player.bowl_style,
        "is_overseas": bool(is_overseas),
    }
    if extra:
        blob.update(extra)
    return json.dumps(blob, separators=(",", ":"))

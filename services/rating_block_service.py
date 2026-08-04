"""Blocked rating ranges — ratings that random reward draws must never award.

The website's Claim Rarity page owns both halves of "what can a player pull":
the rarity weights decide how likely each band is, and the block rules decide
which ratings are off the table entirely. A block is a veto, not a weight — it
is applied after a band has been chosen, so an admin can pull rating 95-100 out
of circulation without touching the tier table and put it back with one
checkbox.

Three sources are recognised, matching the three ways a card arrives by chance:

  ``claim``  /claim, /daily and everything else driven by the rarity tiers
  ``drop``   packs, free packs, and other card drops
  ``gspin``  the /gspin wheel and its Mini App twin

Deliberate acquisitions — buying, trading, the market, admin grants, career
cards — are never filtered here. Blocking those would take players' own cards
away from them, which is not what the setting means.

Lookups are served from a small module-level cache (rules change roughly never,
draws happen constantly). :func:`invalidate` clears it; the website calls that
after every edit, and a short TTL covers a second web worker editing the rules.
"""

import logging
import threading
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Every source a rule can veto, and the column that stores the decision.
SOURCES = ("claim", "drop", "gspin")
_SOURCE_COLUMNS = {
    "claim": "block_claim",
    "drop": "block_drop",
    "gspin": "block_gspin",
}

SOURCE_LABELS = {
    "claim": "Claim / Daily",
    "drop": "Drops & Packs",
    "gspin": "Lucky Card",
}

REFRESH_INTERVAL = timedelta(seconds=30)
# How long to stay quiet after a failed read. Without it a database outage
# turns every draw into another failing query and another logged traceback,
# so the busier the bot gets the harder it hammers the thing that is down.
FAILURE_BACKOFF = timedelta(seconds=5)

_lock = threading.Lock()
_cache = {"by_source": {}, "loaded_at": None, "failed_at": None}


def invalidate():
    """Drop the cached rules so the next draw re-reads them. Call after edits.

    Also clears any failure backoff: an admin who just saved a rule should see
    it take effect immediately, not after the backoff expires.
    """
    with _lock:
        _cache["loaded_at"] = None
        _cache["failed_at"] = None


def _load(session):
    """Rebuild {source: frozenset(blocked ratings)} from the rule table."""
    from models import RatingBlockRule

    by_source = {s: set() for s in SOURCES}
    rows = (session.query(RatingBlockRule)
            .filter(RatingBlockRule.is_active == True).all())
    for row in rows:
        low, high = int(row.rating_min), int(row.rating_max)
        if low > high:
            low, high = high, low
        ratings = range(low, high + 1)
        for source, column in _SOURCE_COLUMNS.items():
            if getattr(row, column, False):
                by_source[source].update(ratings)
    return {s: frozenset(v) for s, v in by_source.items()}


def blocked_ratings(session, source):
    """Return the frozenset of ratings ``source`` may not award.

    Never raises: a missing table or a dead connection means "nothing is
    blocked", because failing open costs an admin one unwanted card while
    failing closed would break /claim outright.
    """
    if source not in SOURCES:
        return frozenset()
    now = datetime.utcnow()
    with _lock:
        loaded = _cache["loaded_at"]
        if loaded is not None and (now - loaded) <= REFRESH_INTERVAL:
            return _cache["by_source"].get(source, frozenset())
        failed = _cache["failed_at"]
        if failed is not None and (now - failed) <= FAILURE_BACKOFF:
            return frozenset()
    try:
        by_source = _load(session)
    except Exception:
        logger.exception("could not load rating block rules — treating as unblocked")
        with _lock:
            _cache["failed_at"] = datetime.utcnow()
        return frozenset()
    with _lock:
        _cache["by_source"] = by_source
        _cache["loaded_at"] = datetime.utcnow()
        _cache["failed_at"] = None
    return by_source.get(source, frozenset())


def is_blocked(session, rating, source):
    """True when ``rating`` may not be awarded by ``source``."""
    if rating is None:
        return False
    return int(rating) in blocked_ratings(session, source)


def allowed_ratings(session, low, high, source):
    """The ratings in [low, high] that ``source`` is still allowed to award."""
    if low > high:
        low, high = high, low
    blocked = blocked_ratings(session, source)
    return [r for r in range(int(low), int(high) + 1) if r not in blocked]


def filter_players(session, players, source):
    """Drop every blocked player from ``players`` (ORM rows or cache dicts).

    Returns a new list. Anything without a readable rating is kept — an unknown
    rating can't be matched against a rule, and silently dropping it would turn
    a data problem into a missing reward.
    """
    blocked = blocked_ratings(session, source)
    if not blocked:
        return list(players)

    def rating_of(p):
        if isinstance(p, dict):
            return p.get("rating")
        return getattr(p, "rating", None)

    return [p for p in players
            if rating_of(p) is None or int(rating_of(p)) not in blocked]


def rating_filter(source, session, model=None):
    """A SQLAlchemy filter excluding ``source``'s blocked ratings, or None.

    For query-based draws that would otherwise pull the blocked rows out of the
    database only to discard them in Python. Returns None when nothing is
    blocked so callers can skip the extra clause.
    """
    blocked = blocked_ratings(session, source)
    if not blocked:
        return None
    if model is None:
        from models import Player as model
    return ~model.rating.in_(sorted(blocked))


def describe(session, source):
    """Human-readable summary of what ``source`` currently can't award."""
    blocked = sorted(blocked_ratings(session, source))
    if not blocked:
        return "nothing blocked"
    spans = []
    start = prev = blocked[0]
    for rating in blocked[1:]:
        if rating == prev + 1:
            prev = rating
            continue
        spans.append((start, prev))
        start = prev = rating
    spans.append((start, prev))
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in spans)

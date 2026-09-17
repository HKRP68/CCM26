"""What a pack's odds are, in one place that everything reads.

A pack's main and bonus slots each carry a *weight per rating*: the odds that a
pull lands on that rating. Four separate pieces of code care about that list —
the admin form that writes it, the pull that obeys it (``services.pack_service``),
the price that is computed from it (``services.pack_pricing``), and the save
routine in ``admin.py`` that turns a screenful of inputs back into one. Before
this module they each parsed it their own way, and a list that one accepted
another quietly discarded.

So the rules live here, once:

* **The stored shape is a positional list**, one weight per integer rating in
  ``[min_rating, max_rating]`` — ``"[60, 30, 10]"`` over 85-87 means 60% of the
  time an 85. That is what ``pack_service._weighted_pick_rating`` has always
  read and what the seeded packs carry, so it stays canonical. :func:`weights_map`
  also accepts a rating-keyed object and a comma-separated string, which is what
  makes the old free-text box and any scripted post keep working.
* **``None`` means uniform**, and every way of failing means ``None``: empty,
  unparseable, the wrong length for the band, a negative weight, or a set of
  weights that sums to zero. Those are exactly the conditions under which the
  pull falls back to ``random.choice``, and pretending otherwise would price a
  pack differently from how it pays out.
* **A rating with no card cannot be pulled**, whatever weight it carries. The
  rows say so rather than showing odds nobody can win.

:func:`odds_rows` is the one builder behind both halves of the admin form — the
server-rendered first paint and the JSON the page re-fetches when the filters
change — so the table can never disagree with itself.
"""

import json
import logging
import random

from sqlalchemy import func

from config import get_buy_value
from models import Player
from services.player_service import not_career

logger = logging.getLogger(__name__)

# The smallest weight the form will keep, mirroring ``admin.MIN_TIER_PROBABILITY``
# on the /claim rarity page. A true chase rating should be possible without
# deleting the row, and 1 in 10,000,000 is chase enough.
MIN_WEIGHT = 0.00001
WEIGHT_DECIMALS = 5

# Ratings run 50-100 everywhere in the game, so a band is at most 51 rows —
# which is also the widest odds table the form will ever render.
RATING_FLOOR = 50
RATING_CEILING = 100


def band(min_rating, max_rating):
    """The ratings a slot covers, low to high, clamped to 50-100.

    An inverted band is read as the admin having typed the two numbers the wrong
    way round rather than as an empty pack — the same courtesy
    ``_save_pack_from_form`` and ``main_slot_value`` already extend.
    """
    try:
        low, high = int(min_rating), int(max_rating)
    except (TypeError, ValueError):
        return []
    if high < low:
        low, high = high, low
    low = max(RATING_FLOOR, min(RATING_CEILING, low))
    high = max(RATING_FLOOR, min(RATING_CEILING, high))
    return list(range(low, high + 1))


# ──────────────────────────────────────────────────────────────────────
# Reading what the form and the columns carry
# ──────────────────────────────────────────────────────────────────────

def parse_versions(raw):
    """A version list from JSON, a list, or a comma-separated string."""
    if not raw:
        return []
    values = raw
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("["):
            try:
                values = json.loads(text)
            except (ValueError, TypeError):
                values = text.split(",")
        else:
            values = text.split(",")
    if not isinstance(values, (list, tuple)):
        return []
    out = []
    for item in values:
        name = str(item).strip()
        if name and name.casefold() not in {v.casefold() for v in out}:
            out.append(name)
    return out


def parse_versions_field(session, posted):
    """The version list from a submitted form field.

    The picker posts one value per choice, so the common case is already a
    list. A single value is ambiguous: it is one option from the picker, or it
    is the comma-joined string the field used to be a free-text box for. It is
    only split when it does not name a real version — otherwise a version
    actually called "World Cup, Final" would be torn in half by its own name.
    """
    values = list(posted or [])
    if len(values) != 1:
        return parse_versions(values)
    only = str(values[0]).strip()
    if "," not in only:
        return parse_versions(only)
    exists = (not_career(session.query(Player.id))
              .filter(func.lower(Player.version) == only.lower())
              .first() is not None)
    return [only] if exists else parse_versions(only)


def parse_weights(raw, expected):
    """``expected``-long weight list, or ``None`` for uniform.

    Mirrors ``pack_service._weighted_pick_rating``: a list of the wrong length,
    unreadable JSON, or weights summing to zero all mean uniform, because that
    is what the pack itself will do with them.
    """
    if not raw or expected <= 0:
        return None
    values = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        if text.startswith("["):
            try:
                values = json.loads(text)
            except (ValueError, TypeError):
                return None
        else:
            values = [part for part in text.split(",") if part.strip()]
    if not isinstance(values, (list, tuple)) or len(values) != expected:
        return None
    try:
        weights = [float(str(w).strip()) for w in values]
    except (TypeError, ValueError):
        return None
    if any(w < 0 for w in weights) or sum(weights) <= 0:
        return None
    return weights


def base_counts_by_rating(session, min_rating, max_rating):
    """``{rating: base cards available}`` over a band — rating-mode's real pool."""
    rows = (not_career(session.query(Player.rating, func.count(Player.id)))
            .filter(Player.is_active == True,
                    Player.parent_player_id.is_(None),
                    Player.rating.between(int(min_rating), int(max_rating)))
            .group_by(Player.rating).all())
    return {int(r): int(c or 0) for r, c in rows if r is not None}


def version_counts_by_rating(session, versions, min_rating, max_rating):
    """``{rating: cards of these versions}`` over a band — 'both' mode's pool."""
    lowered = [v.lower() for v in versions]
    if not lowered:
        return {}
    rows = (not_career(session.query(Player.rating, func.count(Player.id)))
            .filter(Player.is_active == True,
                    Player.rating.between(int(min_rating), int(max_rating)),
                    func.lower(Player.version).in_(lowered))
            .group_by(Player.rating).all())
    return {int(r): int(c or 0) for r, c in rows if r is not None}

# ──────────────────────────────────────────────────────────────────────
# The codec
# ──────────────────────────────────────────────────────────────────────

def weights_map(min_rating, max_rating, raw):
    """``{rating: weight}`` for a band, or ``None`` when the odds are uniform.

    Accepts every shape a weight list arrives in:

    * a positional JSON list — the stored form, whose length must match the band
      exactly, because that is the only thing that tells us which rating each
      number belongs to;
    * a rating-keyed JSON object, ``{"88": 35, "89": 20}`` — ratings outside the
      band are dropped, missing ones are zero. Nothing writes this yet; reading
      it now is what makes swapping the stored shape a one-line change later;
    * a comma-separated string — the free-text box the form used to be;
    * a ``list`` or ``dict`` already parsed.

    Everything that cannot produce a usable set of odds returns ``None``, which
    is what ``pack_service._weighted_pick_rating`` treats as "pick evenly".
    """
    ratings = band(min_rating, max_rating)
    if not ratings or raw is None:
        return None

    values = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        if text.startswith("[") or text.startswith("{"):
            try:
                values = json.loads(text)
            except (ValueError, TypeError):
                logger.debug("pack odds: unreadable JSON %r", text)
                return None
        else:
            values = [part for part in text.split(",") if part.strip()]

    if isinstance(values, dict):
        mapping = {}
        for key, value in values.items():
            try:
                rating, weight = int(key), float(value)
            except (TypeError, ValueError):
                return None
            if rating in ratings:
                mapping[rating] = weight
        out = {r: mapping.get(r, 0.0) for r in ratings}
    elif isinstance(values, (list, tuple)):
        # Length is the only thing that says which rating each weight is for.
        if len(values) != len(ratings):
            logger.debug("pack odds: %d weights for %d ratings — uniform",
                         len(values), len(ratings))
            return None
        try:
            out = {r: float(str(w).strip()) for r, w in zip(ratings, values)}
        except (TypeError, ValueError):
            return None
    else:
        return None

    if any(w < 0 for w in out.values()) or sum(out.values()) <= 0:
        return None
    return out


def weights_json(min_rating, max_rating, mapping):
    """A ``{rating: weight}`` map back to the stored positional list.

    ``None`` when the odds carry no information — every weight zero, an empty
    map, or a band of one rating, where a weight list is noise. ``None`` is the
    sentinel the whole codebase already keys on for "uniform", so a cleared
    table and a pack that never had odds are stored identically.
    """
    ratings = band(min_rating, max_rating)
    if not ratings or len(ratings) < 2 or not mapping:
        return None
    weights = []
    for rating in ratings:
        try:
            weight = float(mapping.get(rating, 0) or 0)
        except (TypeError, ValueError):
            weight = 0.0
        weights.append(round(max(0.0, weight), WEIGHT_DECIMALS))
    if sum(weights) <= 0:
        return None
    # Whole numbers store as whole numbers: "[60, 30, 10]" is what the seeded
    # packs carry and what anybody reading the column expects to see.
    tidy = [int(w) if float(w).is_integer() else w for w in weights]
    return json.dumps(tidy)


# ──────────────────────────────────────────────────────────────────────
# What the catalogue can fill
# ──────────────────────────────────────────────────────────────────────

def version_span(session, versions):
    """``(min, max)`` rating across the cards carrying ``versions``, or ``None``.

    This is what makes "pick a version, then set the odds" possible at all: the
    admin never types a rating band for a version pack, so the band is whatever
    the version actually holds.
    """
    names = parse_versions(versions)
    if not names:
        return None
    counts = _counts_for(session, mode="version", versions=names,
                         ratings=list(range(RATING_FLOOR, RATING_CEILING + 1)))
    live = [r for r, c in counts.items() if c > 0]
    if not live:
        return None
    return min(live), max(live)


def _counts_for(session, *, mode, versions, ratings, slot="main"):
    """``{rating: cards available}`` over ``ratings`` for one slot's filter."""
    if not ratings:
        return {}
    low, high = ratings[0], ratings[-1]
    if slot == "bonus" or mode == "rating":
        # Bonus slots and rating mode both draw from base cards only.
        return base_counts_by_rating(session, low, high)
    if not versions:
        # 'both' with no version falls back to rating mode, and so does the
        # pool — see ``pack_pricing.main_slot_value``.
        return base_counts_by_rating(session, low, high)
    return version_counts_by_rating(session, versions, low, high)


def odds_rows(session, *, mode="rating", versions=None, min_rating=70,
              max_rating=99, weights_raw=None, slot="main"):
    """Everything the odds table needs for one slot.

    One builder for both the Jinja first paint and the JSON the page re-fetches,
    so the two can never drift. Rows come back **highest rating first** — the
    card an admin cares about is the one at the top of the pack.
    """
    mode = (mode or "rating").lower()
    names = parse_versions(versions)

    # Odds that already fit the band they were given describe that band — a
    # positional list only parses against the right number of ratings, which is
    # what makes it self-describing. So a version pack whose band was narrowed
    # by hand keeps its narrowed band here rather than being widened back to
    # the version's full span, which would re-map every weight by one rating.
    mapping = weights_map(min_rating, max_rating, weights_raw)

    span_source, span_note = "inputs", ""
    if slot == "main" and mode == "version" and mapping is None:
        span = version_span(session, names)
        if span:
            min_rating, max_rating = span
            span_source = "versions"
            span_note = (f"Rating span taken from {', '.join(names)} — "
                         f"{min_rating}-{max_rating}.")
        elif names:
            span_note = f"No active cards carry {', '.join(names)}."
        else:
            span_note = "Pick a version to see what it can pull."
    elif slot == "main" and mode == "version" and names:
        span_note = (f"Rated {min_rating}-{max_rating} within "
                     f"{', '.join(names)}.")

    ratings = band(min_rating, max_rating)
    counts = _counts_for(session, mode=mode, versions=names, ratings=ratings,
                         slot=slot)

    rows = []
    for rating in sorted(ratings, reverse=True):
        pool = int(counts.get(rating, 0) or 0)
        rows.append({
            "rating": rating,
            "pool": pool,
            "buy_value": get_buy_value(rating),
            "weight": (mapping or {}).get(rating, 0.0),
        })

    return {
        "slot": slot,
        "mode": mode,
        "min_rating": ratings[0] if ratings else 0,
        "max_rating": ratings[-1] if ratings else 0,
        "span_source": span_source,
        "span_note": span_note,
        "rows": rows,
        "uniform": mapping is None,
        "empty_ratings": [r["rating"] for r in rows if not r["pool"]],
        "pool_total": sum(r["pool"] for r in rows),
        "versions": names,
    }


# ──────────────────────────────────────────────────────────────────────
# Seeing the odds before the pack ships
# ──────────────────────────────────────────────────────────────────────

def simulate(counts, min_rating, max_rating, weights_raw, draws=1000):
    """``{rating: hits}`` over ``draws`` pulls, on the configured odds.

    The rating half of a pull only — which rating comes up, not which card — so
    it can answer for a pack that has not been saved yet. A rating with an empty
    pool is dropped and the rest renormalised, exactly as the pull does, which
    is the whole point: a table that looks balanced but leans on a rating nobody
    has a card at simulates very differently from how it reads.
    """
    ratings = band(min_rating, max_rating)
    live = [r for r in ratings if (counts or {}).get(r, 0) > 0]
    if not live or draws <= 0:
        return {}

    mapping = weights_map(min_rating, max_rating, weights_raw)
    weights = [float((mapping or {}).get(r, 1.0)) for r in live]
    if sum(weights) <= 0:
        weights = [1.0] * len(live)

    tally = {r: 0 for r in ratings}
    for rating in random.choices(live, weights=weights, k=int(draws)):
        tally[rating] += 1
    return tally

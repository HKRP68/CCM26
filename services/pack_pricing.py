"""What a pack is worth, and what it should therefore cost.

Pricing a pack by hand means opening the rating table, working out which cards
the filters can actually produce, averaging their buy values, weighting that by
the rating weights, adding the bonus slots, and then picking a round number.
Admins did that in their head, and a pack whose price was guessed is either free
money or dead stock.

This does the arithmetic instead. ``suggest`` reads the same filters the pack
itself draws from — the same modes, the same rating weights, the same version
list, ``not_career`` and ``is_active`` and all — works out what one pull of that
pack is worth in coins, and rounds it up to the next round number. The other two
currencies come off that one figure at the game's own rates.

Three things worth knowing about the numbers it produces:

* **It is the value of the cards, not a verdict on the price.** The shipped
  packs deliberately sell the big ones a little under what they contain; that is
  a design choice, and this hands the admin the honest figure to make it from
  rather than making it for them.
* **A rating nobody has a card at is worth nothing.** A pack weighted towards a
  rating with an empty pool cannot pull it, so that weight is dropped and the
  rest renormalised — and the empty ratings are reported, because an unfillable
  band is usually a mistake rather than a price.
* **The three currencies are alternatives.** ``services.pack_service`` requires
  every non-zero cost but charges only the first of coins → quest points → gems,
  so a pack with two prices set asks for money it never takes. The suggestion
  offers all three; the form applies one.
"""

import json
import logging
import math

from sqlalchemy import func

from config import COINS_PER_GEM, get_buy_value
from models import Player
from services.player_service import not_career

logger = logging.getLogger(__name__)

# Coins one quest point is worth. Not an exchange rate anybody can trade at —
# quest points are earned, never bought — but the shipped packs price against
# it: Bronze's 700 QP buys ~950k of cards and Silver's 1000 QP ~1.46M, which is
# this rate to within a few per cent. Retune it here and every suggestion moves
# with it.
COINS_PER_QUEST_POINT = 1_400

# The canonical rarities always offered in the version picker, even before a
# card carrying one exists — a new pack for a version being prepared should not
# have to wait for the first card to be uploaded.
CANONICAL_VERSIONS = ("Base", "Star", "Legend")


# ──────────────────────────────────────────────────────────────────────
# Rounding
# ──────────────────────────────────────────────────────────────────────

def round_up_nice(value):
    """Round ``value`` up to the next round number — two significant figures.

    ``925_300`` → ``930_000``; ``2_211_700`` → ``2_300_000``; ``678`` → ``680``.
    Always rounds up, never down: a suggested price that undercuts the cards in
    the pack by a rounding error is the one direction that costs the game coins.
    """
    value = int(math.ceil(float(value or 0)))
    if value <= 0:
        return 0
    if value < 100:
        return value
    step = 10 ** (len(str(value)) - 2)
    return int(math.ceil(value / step) * step)


# ──────────────────────────────────────────────────────────────────────
# What is in the catalogue
# ──────────────────────────────────────────────────────────────────────

def version_catalogue(session):
    """Every version a pack can be pointed at, with what picking it means.

    ``[{name, cards, avg_value, min_rating, max_rating}, …]`` — one row per
    distinct ``Player.version`` among the cards a pack may actually draw
    (active, not a career card), richest first, with the canonical rarities
    always present even when no card carries them yet.

    The counts and averages are what make the picker worth having: "Legend —
    42 cards · avg 2,350,000 🪙" is a choice an admin can price from, where a
    bare list of names is a spelling test.

    One grouped query for the whole catalogue. Averaging is over the CARDS, not
    over the ratings — a version with thirty 85s and one 99 is worth about an
    85 — which is why the rating breakdown is fetched rather than an AVG().
    """
    rows = (not_career(session.query(Player.version, Player.rating,
                                     func.count(Player.id)))
            .filter(Player.is_active == True,
                    Player.version.isnot(None))
            .group_by(Player.version, Player.rating).all())

    tally = {}
    for name, rating, count in rows:
        label = (name or "").strip()
        if not label:
            continue
        tally.setdefault(label, {})[int(rating or 0)] = int(count or 0)

    out = []
    for label, by_rating in tally.items():
        cards = sum(by_rating.values())
        value = (sum(get_buy_value(r) * c for r, c in by_rating.items()) / cards
                 if cards else 0)
        out.append({
            "name": label,
            "cards": cards,
            "avg_value": int(round(value)),
            "min_rating": min(by_rating) if by_rating else 0,
            "max_rating": max(by_rating) if by_rating else 0,
        })

    seen = {row["name"].casefold() for row in out}
    for name in CANONICAL_VERSIONS:
        if name.casefold() not in seen:
            out.append({"name": name, "cards": 0, "avg_value": 0,
                        "min_rating": 0, "max_rating": 0})
    out.sort(key=lambda r: (-r["avg_value"], r["name"].casefold()))
    return out


# ──────────────────────────────────────────────────────────────────────
# What one pull is worth
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


def _base_counts_by_rating(session, min_rating, max_rating):
    """``{rating: base cards available}`` over a band — rating-mode's real pool."""
    rows = (not_career(session.query(Player.rating, func.count(Player.id)))
            .filter(Player.is_active == True,
                    Player.parent_player_id.is_(None),
                    Player.rating.between(int(min_rating), int(max_rating)))
            .group_by(Player.rating).all())
    return {int(r): int(c or 0) for r, c in rows if r is not None}


def _version_counts_by_rating(session, versions, min_rating, max_rating):
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


def _weighted_band_value(counts, min_rating, max_rating, weights):
    """``(value, pool, empty_ratings)`` for a weighted pull over a rating band.

    Every card at a given rating costs the same, so a rating's value is simply
    its buy price and the only question is how often it comes up. A rating with
    no card in ``counts`` cannot be pulled at all: its weight is dropped and the
    rest renormalised, which is what the pack does in practice (it tries that
    rating, finds nothing, and the pull fails or widens).
    """
    ratings = list(range(int(min_rating), int(max_rating) + 1))
    if not ratings:
        return 0, 0, []
    if weights is None or len(weights) != len(ratings):
        weights = [1.0] * len(ratings)

    live = [(r, w) for r, w in zip(ratings, weights) if counts.get(r, 0) > 0]
    empty = [r for r in ratings if counts.get(r, 0) <= 0]
    total_weight = sum(w for _r, w in live)
    if not live or total_weight <= 0:
        return 0, sum(counts.values()), empty
    value = sum(get_buy_value(r) * w for r, w in live) / total_weight
    return int(round(value)), sum(counts.values()), empty


def main_slot_value(session, *, mode, versions, min_rating, max_rating, weights):
    """``{value, pool, empty_ratings, basis}`` for one main-slot pull."""
    mode = (mode or "rating").lower()
    versions = parse_versions(versions)
    min_rating, max_rating = int(min_rating), int(max_rating)
    if max_rating < min_rating:
        min_rating, max_rating = max_rating, min_rating
    weights = parse_weights(weights, max_rating - min_rating + 1)

    if mode == "version":
        # No rating selection happens at all — the pull is uniform over every
        # card carrying one of the named versions, so the value is their mean.
        if not versions:
            return {"value": 0, "pool": 0, "empty_ratings": [],
                    "basis": "version — no version selected"}
        lowered = [v.lower() for v in versions]
        rows = (not_career(session.query(Player.rating, func.count(Player.id)))
                .filter(Player.is_active == True,
                        func.lower(Player.version).in_(lowered))
                .group_by(Player.rating).all())
        counts = {int(r or 0): int(c or 0) for r, c in rows}
        pool = sum(counts.values())
        if not pool:
            return {"value": 0, "pool": 0, "empty_ratings": [],
                    "basis": "version — no cards carry " + ", ".join(versions)}
        value = sum(get_buy_value(r) * c for r, c in counts.items()) / pool
        return {"value": int(round(value)), "pool": pool,
                "empty_ratings": [],
                "basis": f"average of {pool} card(s) in " + ", ".join(versions)}

    if mode == "both":
        if not versions:
            # The pack falls back to rating-only here, so the price does too.
            return main_slot_value(session, mode="rating", versions=None,
                                   min_rating=min_rating, max_rating=max_rating,
                                   weights=weights)
        counts = _version_counts_by_rating(session, versions,
                                           min_rating, max_rating)
        value, pool, empty = _weighted_band_value(counts, min_rating,
                                                  max_rating, weights)
        return {"value": value, "pool": pool, "empty_ratings": empty,
                "basis": f"{', '.join(versions)} rated {min_rating}-{max_rating}, "
                         + ("weighted" if weights else "even odds")}

    counts = _base_counts_by_rating(session, min_rating, max_rating)
    value, pool, empty = _weighted_band_value(counts, min_rating, max_rating,
                                              weights)
    return {"value": value, "pool": pool, "empty_ratings": empty,
            "basis": f"base cards rated {min_rating}-{max_rating}, "
                     + ("weighted" if weights else "even odds")}


def bonus_slot_value(session, min_rating, max_rating):
    """``{value, pool, empty_ratings, basis}`` for one bonus-slot pull.

    Bonus slots are rating-only and always uniform — no version filter, no
    weights — so this is the mean buy value of the base cards in the band,
    weighted by how many there are at each rating.
    """
    min_rating, max_rating = int(min_rating), int(max_rating)
    if max_rating < min_rating:
        min_rating, max_rating = max_rating, min_rating
    counts = _base_counts_by_rating(session, min_rating, max_rating)
    pool = sum(counts.values())
    if not pool:
        return {"value": 0, "pool": 0,
                "empty_ratings": list(range(min_rating, max_rating + 1)),
                "basis": f"no base cards rated {min_rating}-{max_rating}"}
    # Uniform over the CARDS, not over the ratings: a band with forty 74s and
    # one 80 is worth about a 74.
    value = sum(get_buy_value(r) * c for r, c in counts.items()) / pool
    empty = [r for r in range(min_rating, max_rating + 1) if counts.get(r, 0) <= 0]
    return {"value": int(round(value)), "pool": pool, "empty_ratings": empty,
            "basis": f"{pool} base card(s) rated {min_rating}-{max_rating}"}


# ──────────────────────────────────────────────────────────────────────
# The suggestion
# ──────────────────────────────────────────────────────────────────────

def suggest(session, *, main_filter_mode="rating", main_versions=None,
            main_min_rating=70, main_max_rating=99, main_count=1,
            main_weights=None, bonus_min_rating=70, bonus_max_rating=80,
            bonus_count=0):
    """Price one pack from what it can pull.

    Returns the two halves of the value (main slots and bonus slots), their
    total, and that total rounded up to a round number in each of the three
    currencies. Nothing here writes: the caller decides what to do with it.
    """
    main = main_slot_value(session, mode=main_filter_mode,
                           versions=main_versions,
                           min_rating=main_min_rating,
                           max_rating=main_max_rating,
                           weights=main_weights)
    bonus = bonus_slot_value(session, bonus_min_rating, bonus_max_rating)

    main_count = max(0, int(main_count or 0))
    bonus_count = max(0, int(bonus_count or 0))
    main_total = main["value"] * main_count
    bonus_total = bonus["value"] * bonus_count
    raw = main_total + bonus_total

    coins = round_up_nice(raw)
    quest_points = round_up_nice(raw / COINS_PER_QUEST_POINT) if raw else 0
    gems = round_up_nice(raw / COINS_PER_GEM) if raw else 0

    warnings = []
    if main_count and not main["pool"]:
        warnings.append("The main slot can't pull anything — nothing matches "
                        "its filter, so this pack would open empty.")
    if bonus_count and not bonus["pool"]:
        warnings.append(f"No base cards are rated {bonus_min_rating}-"
                        f"{bonus_max_rating}, so the bonus slots would come up "
                        "empty.")
    if main["empty_ratings"]:
        warnings.append("Nothing to pull at rating "
                        + ", ".join(str(r) for r in main["empty_ratings"])
                        + " — those odds were dropped from the estimate.")

    return {
        "main": main, "main_count": main_count, "main_total": main_total,
        "bonus": bonus, "bonus_count": bonus_count, "bonus_total": bonus_total,
        "raw_value": int(raw),
        "coins": coins,
        "quest_points": quest_points,
        "gems": gems,
        "coins_per_quest_point": COINS_PER_QUEST_POINT,
        "coins_per_gem": COINS_PER_GEM,
        "warnings": warnings,
    }

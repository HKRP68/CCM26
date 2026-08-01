"""Career Player name generation from a website-managed, per-country name pool.

The /cmucareer wizard asks for a country, then a first-name initial, then a
surname initial, and builds a name from those three answers. The candidate first
names and surnames live in ``career_name_pool``, seeded from the real players in
``data/players.json`` by ``seed_career_names.py`` and editable on the website.

Two rules shape everything here:

* **The name must be genuinely new.** A career name is never allowed to collide
  with any existing player — real or another user's career card. Uniqueness is
  checked before the name is offered and again inside the creation transaction,
  and the database's ``uq_players_name_version`` index is the final backstop.
* **The wizard must never dead-end.** The letter buttons are built from what the
  pool actually holds, so every offered letter can produce a name.
"""

import logging
import random
import string

from sqlalchemy import func

from models import CareerNamePool, Player

logger = logging.getLogger(__name__)

FIRST = "first"
SURNAME = "surname"
LETTERS = tuple(string.ascii_uppercase)


def normalise_value(value):
    """Trim a pool entry and capitalise its first letter.

    The source capitalisation is otherwise kept intact. ``str.title()`` would
    lower-case every letter after the first of each word, flattening the real
    surnames this pool is mined from — "McCullum" to "Mccullum", "MacKenzie" to
    "Mackenzie", "de Villiers" to "De Villiers" — and the result is stamped
    permanently on somebody's career card.
    """
    cleaned = " ".join((value or "").split())[:60]
    return cleaned[:1].upper() + cleaned[1:] if cleaned else ""


def letter_of(value):
    """The uppercase first letter a pool entry is filed under, or ``''``."""
    cleaned = normalise_value(value)
    first = cleaned[:1].upper()
    return first if first in LETTERS else ""


def name_is_taken(session, name):
    """True when any player already carries this name (case-insensitive)."""
    name = (name or "").strip()
    if not name:
        return True
    return session.query(Player.id).filter(
        func.lower(Player.name) == name.lower()).first() is not None


# ── Pool queries ────────────────────────────────────────────────────────────

def _pool_query(session, country, kind):
    return (session.query(CareerNamePool)
            .filter(CareerNamePool.is_active.is_(True),
                    CareerNamePool.country == country,
                    CareerNamePool.name_kind == kind))


def available_countries(session, require_flag=True):
    """Countries that can actually produce a name, sorted alphabetically.

    A country qualifies when it has at least one first name and one surname.
    When ``require_flag`` is set (the wizard's country step shows flags), it must
    also have a flag PNG uploaded on the website.
    """
    rows = (session.query(CareerNamePool.country, CareerNamePool.name_kind)
            .filter(CareerNamePool.is_active.is_(True))
            .distinct().all())
    kinds = {}
    for country, kind in rows:
        kinds.setdefault(country, set()).add(kind)
    countries = sorted(c for c, k in kinds.items() if {FIRST, SURNAME} <= k)
    if not require_flag:
        return countries
    try:
        from services.country_flag_service import list_country_flags, normalise_country
        have = {normalise_country(c) for c in list_country_flags()}
    except Exception:
        logger.exception("country flag listing failed")
        return countries
    if not have:
        # No flags uploaded yet — don't strand the wizard with an empty list.
        return countries
    return [c for c in countries if normalise_country(c) in have]


def available_letters(session, country, kind):
    """Uppercase letters that have at least one active name behind them."""
    rows = (session.query(CareerNamePool.letter)
            .filter(CareerNamePool.is_active.is_(True),
                    CareerNamePool.country == country,
                    CareerNamePool.name_kind == kind)
            .distinct().all())
    return sorted({r[0] for r in rows if r[0] in LETTERS})


def available_initials(session, country):
    """Letters offered on the wizard's first-name step."""
    return available_letters(session, country, FIRST)


def available_surname_letters(session, country, initial=None):
    """Letters offered on the wizard's surname step.

    ``initial`` is accepted so the caller can stay symmetrical with
    :func:`available_initials`; surnames are not filtered by it, because the two
    choices are independent and filtering would shrink the pool for no gain.
    """
    return available_letters(session, country, SURNAME)


def _values(session, country, kind, letter):
    rows = (_pool_query(session, country, kind)
            .filter(CareerNamePool.letter == letter.upper()).all())
    return [r.value for r in rows]


# ── Generation ──────────────────────────────────────────────────────────────

# How many candidates go into one ``IN (…)`` lookup. Big enough that a normal
# letter pair resolves in a single round trip, small enough to stay inside the
# parameter limits of every backend we run on.
_NAME_LOOKUP_CHUNK = 500


def _taken_among(session, candidates):
    """The lower-cased subset of ``candidates`` some player already carries.

    One ``IN`` query per chunk rather than one query per candidate: the
    per-candidate form ran ``lower(name) = …``, which cannot use
    ``ix_players_name`` and so scanned the whole ``players`` table every time —
    up to ``len(firsts) * len(surnames)`` scans on a single wizard tap.
    """
    taken = set()
    lowered = sorted({c.lower() for c in candidates})
    for start in range(0, len(lowered), _NAME_LOOKUP_CHUNK):
        chunk = lowered[start:start + _NAME_LOOKUP_CHUNK]
        rows = (session.query(Player.name)
                .filter(func.lower(Player.name).in_(chunk)).all())
        taken.update((r[0] or "").lower() for r in rows)
    return taken


def generate_name(session, country, initial, surname_letter):
    """Build a career name from the pool that no existing player uses.

    Returns the name, or ``None`` when every combination for these two letters
    is already taken — the wizard then asks for a different letter rather than
    creating a duplicate.
    """
    firsts = _values(session, country, FIRST, initial)
    surnames = _values(session, country, SURNAME, surname_letter)
    if not firsts or not surnames:
        return None

    # Every combination in random order, so the first free one is a random one
    # and an exhausted letter pair still resolves without a second pass.
    pairs = [(f, s) for f in firsts for s in surnames]
    random.shuffle(pairs)
    candidates = [f"{first} {surname}" for first, surname in pairs]

    taken = _taken_among(session, candidates)
    for candidate in candidates:
        if candidate.lower() not in taken:
            return candidate
    return None


def random_letters(session, country):
    """A random (initial, surname_letter) pair that can produce a name.

    Backs the wizard's "🔀 Surprise me" shortcut.
    """
    initials = available_initials(session, country)
    surnames = available_surname_letters(session, country)
    if not initials or not surnames:
        return None, None
    return random.choice(initials), random.choice(surnames)


# ── Pool editing (website) ──────────────────────────────────────────────────

def add_names(session, country, kind, values):
    """Add names to a country's pool. Returns ``(added, skipped)``.

    Caller commits. Existing entries are skipped rather than duplicated, so a
    bulk paste can be re-run safely.
    """
    country = " ".join((country or "").split())
    if not country:
        return 0, 0
    if kind not in (FIRST, SURNAME):
        return 0, 0

    existing = {v[0].lower() for v in
                session.query(CareerNamePool.value)
                .filter(CareerNamePool.country == country,
                        CareerNamePool.name_kind == kind).all()}
    added = skipped = 0
    for raw in values:
        value = normalise_value(raw)
        letter = letter_of(value)
        if not value or not letter or value.lower() in existing:
            skipped += 1
            continue
        session.add(CareerNamePool(country=country, name_kind=kind,
                                   value=value, letter=letter, is_active=True))
        existing.add(value.lower())
        added += 1
    return added, skipped


def remove_name(session, row_id):
    """Delete one pool entry. Caller commits. Returns whether a row went."""
    row = session.query(CareerNamePool).get(row_id)
    if not row:
        return False
    session.delete(row)
    return True


def pool_summary(session):
    """``[{country, firsts, surnames, usable}]`` for the website's overview."""
    rows = (session.query(CareerNamePool.country, CareerNamePool.name_kind,
                          func.count(CareerNamePool.id))
            .filter(CareerNamePool.is_active.is_(True))
            .group_by(CareerNamePool.country, CareerNamePool.name_kind).all())
    by_country = {}
    for country, kind, count in rows:
        entry = by_country.setdefault(country, {"country": country,
                                                "firsts": 0, "surnames": 0})
        entry["firsts" if kind == FIRST else "surnames"] = count
    for entry in by_country.values():
        entry["usable"] = bool(entry["firsts"] and entry["surnames"])
    return sorted(by_country.values(), key=lambda e: e["country"])

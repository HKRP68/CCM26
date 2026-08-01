"""Seed the Career Player name pool from data/players.json.

Splits each real player's name into a first name and a surname, files them under
that player's country, and writes them to ``career_name_pool``. The /cmucareer
wizard then recombines them into names that no existing player uses.

Idempotent: entries already in the pool are skipped, so this is safe to re-run
after editing the pool on the website (nothing you added by hand is removed).

    python seed_career_names.py           # add missing names
    python seed_career_names.py --reset   # wipe the pool first, then re-seed
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import get_session, init_db          # noqa: E402
from models import CareerNamePool                  # noqa: E402
from services.career_name_service import (         # noqa: E402
    FIRST, SURNAME, add_names,
)

DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "data", "players.json")

# Tokens that are part of a surname rather than a name in their own right, so
# "Kusal Janith Perera" files "Perera" and not "Janith".
_SURNAME_PARTICLES = {"van", "de", "der", "den", "du", "le", "la", "di", "da",
                      "dos", "del", "al", "bin", "ten", "ter"}


def split_name(full_name):
    """Return ``(first, surname)`` for a player name, or ``(None, None)``."""
    parts = [p for p in (full_name or "").replace(".", " ").split() if p]
    if len(parts) < 2:
        return None, None
    first = parts[0]
    # Walk back over any particles so multi-word surnames stay intact.
    index = len(parts) - 1
    while index > 1 and parts[index - 1].lower() in _SURNAME_PARTICLES:
        index -= 1
    surname = " ".join(parts[index:])
    if len(first) < 2 or len(surname) < 2:
        return None, None
    return first, surname


def collect(rows):
    """Return ``{country: {'first': {...}, 'surname': {...}}}`` from the JSON."""
    pools = {}
    for row in rows:
        country = " ".join((row.get("Country") or "").split())
        first, surname = split_name(row.get("Player Name"))
        if not country or not first or not surname:
            continue
        entry = pools.setdefault(country, {FIRST: set(), SURNAME: set()})
        entry[FIRST].add(first)
        entry[SURNAME].add(surname)
    return pools


def main(reset=False):
    init_db()
    with open(DATA_PATH, encoding="utf-8") as handle:
        rows = json.load(handle)
    pools = collect(rows)

    session = get_session()
    try:
        if reset:
            removed = session.query(CareerNamePool).delete()
            session.commit()
            print(f"Cleared {removed} existing pool entries.")

        total_added = total_skipped = 0
        for country in sorted(pools):
            for kind in (FIRST, SURNAME):
                added, skipped = add_names(session, country, kind,
                                           sorted(pools[country][kind]))
                total_added += added
                total_skipped += skipped
            session.commit()
            print(f"  {country}: {len(pools[country][FIRST])} first names, "
                  f"{len(pools[country][SURNAME])} surnames")

        print(f"\n✅ {len(pools)} countries — added {total_added}, "
              f"skipped {total_skipped} already present.")
    finally:
        session.close()


if __name__ == "__main__":
    main(reset="--reset" in sys.argv)

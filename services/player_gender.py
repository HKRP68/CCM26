"""Validate and apply the admin's two-column Player ID / Gender import."""

import csv
import io

from models import Player


GENDER_OPTIONS = ("Male", "Female", "Non-binary", "Other")
_GENDERS = {value.casefold(): value for value in GENDER_OPTIONS}
_GENDERS.update({"m": "Male", "f": "Female", "nb": "Non-binary",
                 "non binary": "Non-binary"})


def normalize_gender(value):
    key = (value or "").strip().casefold()
    if key in ("", "not set", "unspecified", "unknown"):
        return None
    if key not in _GENDERS:
        raise ValueError("Gender must be Male, Female, Non-binary, Other or Not set. M/F are also accepted.")
    return _GENDERS[key]


def parse_gender_rows(content):
    """Read CSV or pasted spreadsheet cells, validating before any writes.

    Blank genders leave existing values alone so a downloaded template can be
    filled a few rows at a time. An explicit 'Not set' clears a saved gender.
    """
    content = content.lstrip("\ufeff\r\n").rstrip("\r\n")
    if not content.strip():
        raise ValueError("Paste Player ID, Gender rows or upload a CSV file.")
    first_line = next(line for line in content.splitlines() if line.strip())
    delimiter = "\t" if "\t" in first_line else ","
    reader = csv.reader(io.StringIO(content), delimiter=delimiter, strict=True)
    values = {}
    first_row = True
    try:
        for row in reader:
            if not row or not any(cell.strip() for cell in row):
                continue
            row = [cell.strip() for cell in row]
            line = reader.line_num
            if first_row:
                first_row = False
                header = row[0].casefold().replace(" ", "").replace("_", "")
                if header in ("id", "playerid"):
                    if len(row) != 2 or row[1].casefold() != "gender":
                        raise ValueError(f"Row {line}: the header must be Player ID, Gender.")
                    continue
            if len(row) != 2:
                raise ValueError(f"Row {line}: enter exactly two columns: Player ID, Gender.")
            if (not row[0].isascii() or not row[0].isdecimal()
                    or not 0 < int(row[0]) <= 2147483647):
                raise ValueError(f"Row {line}: Player ID must be a positive whole number.")
            if not row[1]:
                continue
            player_id = int(row[0])
            if player_id in values:
                raise ValueError(f"Row {line}: Player ID {player_id} appears more than once.")
            try:
                values[player_id] = normalize_gender(row[1])
            except ValueError as exc:
                raise ValueError(f"Row {line}: {exc}") from exc
    except csv.Error as exc:
        raise ValueError(f"Invalid CSV near row {reader.line_num}: {exc}") from exc
    if not values:
        raise ValueError("No genders entered. Fill in at least one Gender cell; blank cells are skipped.")
    return values


def update_player_genders(db, values):
    """Update only the supplied IDs, leaving commit/rollback to the caller."""
    players = {}
    ids = list(values)
    # Stay under SQLite's parameter limit even for a whole-catalogue import.
    for start in range(0, len(ids), 500):
        players.update((player.id, player) for player in
                       db.query(Player).filter(Player.id.in_(ids[start:start + 500])).all())
    missing = sorted(set(ids) - players.keys())
    if missing:
        listed = ", ".join(str(player_id) for player_id in missing[:20])
        suffix = f" (and {len(missing) - 20} more)" if len(missing) > 20 else ""
        raise ValueError(f"Player IDs not found: {listed}{suffix}.")
    updated = 0
    for player_id, gender in values.items():
        player = players[player_id]
        if player.gender != gender:
            player.gender = gender
            updated += 1
    return updated, len(values) - updated

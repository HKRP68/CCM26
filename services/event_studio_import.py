"""Preview imports against the existing Challenge Data format and card catalog."""

import csv
import io

from sqlalchemy import func, select

from models import Player
from services.challenge_team_io import team_values, read_teams_json

MAX_IMPORT_ROWS = 640


def teams_import(text, current):
    by_name = {team["name"].casefold(): team for team in current}
    used = {team["key"] for team in current}
    rows = read_teams_json(text)
    if not 1 <= len(rows) <= 16:
        raise ValueError("Import between 1 and 16 teams.")
    result = dict(by_name)
    for number, raw in enumerate(rows, 1):
        try:
            values = team_values(raw)
            name = values.get("name")
            if not name:
                raise ValueError("a team name is required")
            key = name.casefold()
            if key in result:
                result[key] = {**result[key], **values}
            else:
                index = 1
                while f"import-{index}" in used:
                    index += 1
                team_key = f"import-{index}"
                used.add(team_key)
                result[key] = {**values, "key": team_key, "players": []}
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Team row {number}: {exc}") from exc
    return list(result.values())


def squad_import(db, text, teams):
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    headers = {header.strip().upper(): header for header in reader.fieldnames or []}
    if not {"TEAM NAME", "PLAYER NAME", "VERSION"} <= set(headers):
        raise ValueError("CSV needs TEAM NAME,PLAYER NAME,VERSION headers.")
    rows = []
    for row in reader:
        if len(rows) >= MAX_IMPORT_ROWS:
            raise ValueError(f"Import at most {MAX_IMPORT_ROWS} squad rows.")
        columns = [(row.get(headers[header]) or "").strip() for header in ("TEAM NAME", "PLAYER NAME", "VERSION")]
        if any(columns):
            rows.append((reader.line_num, *columns))
    if not rows:
        raise ValueError("The CSV has no squad rows.")
    names = {name.casefold() for _, _, name, _ in rows if name}
    candidates = {}
    for card in db.scalars(select(Player).where(func.lower(Player.name).in_(names),
                                               Player.is_active.is_(True), Player.is_career.is_(False))):
        candidates.setdefault(card.name.casefold(), []).append(card)
    by_name = {team["name"].casefold(): {**team} for team in teams}
    assigned, errors = {}, []
    for number, team_name, player_name, version in rows:
        team = by_name.get(team_name.casefold())
        matches = [card for card in candidates.get(player_name.casefold(), [])
                   if not version or (card.version or "Base").casefold() == version.casefold()]
        if team is None:
            errors.append(f"Row {number}: unknown team {team_name!r}")
        elif len(matches) != 1:
            errors.append(f"Row {number}: {'ambiguous' if matches else 'missing'} card {player_name!r} ({version or 'no version'})")
        else:
            selected = assigned.setdefault(team["key"], [])
            if matches[0].id in selected:
                errors.append(f"Row {number}: duplicate card {player_name!r}")
            else:
                selected.append(matches[0].id)
    if errors:
        raise ValueError("\n".join(errors[:50]) + (f"\n{len(errors) - 50} more errors." if len(errors) > 50 else ""))
    for team in by_name.values():
        if team["key"] in assigned:
            team["players"] = assigned[team["key"]]
            team.pop("xi", None)
    return list(by_name.values())

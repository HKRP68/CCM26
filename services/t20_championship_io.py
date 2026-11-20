"""Read and write World T20 team details and name-based squads."""
import csv
import io
import json
import re
from functools import lru_cache
from urllib.parse import unquote, urlsplit

from sqlalchemy import func
from sqlalchemy.orm import joinedload, selectinload

from services import challenge_team_io
from services.player_query import challenge_details_json

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MIMETYPES = {"csv": "text/csv", "json": "application/json"}
COLUMNS = ("team_name", "short_name", "logo_url", "primary_color", "secondary_color",
           "owner", "co_owner", "player_name")
CSV_ALIASES = {"name": "team_name", "logo": "logo_url", "team_logo": "logo_url",
               "primary_colour": "primary_color", "secondary_colour": "secondary_color",
               "team_owner": "owner", "owner_id": "owner",
               "team_co_owner": "co_owner", "co_owner_id": "co_owner"}
TEMPLATE = [
    {"team_name": "India", "short_name": "IND", "primary_color": "#1d4ed8",
     "secondary_color": "#facc15", "owner": "@owner_username",
     "co_owner": "@co_owner_username", "logo_url": "",
     "players": ["Virat Kohli", "Jasprit Bumrah"]},
    {"team_name": "Australia", "short_name": "AUS", "primary_color": "#ffcd00",
     "secondary_color": "#006341", "owner": "@another_owner",
     "players": ["Glenn Maxwell"]},
]


class SquadImportError(ValueError):
    def __init__(self, errors):
        self.errors = errors[:20]
        if len(errors) > 20:
            self.errors.append(f"{len(errors) - 20} more errors in this file.")
        super().__init__(self.errors[0])


def _csv_needs_quote(value):
    trimmed = value.lstrip()
    return value.startswith(("\t", "\r", "\n")) or trimmed.startswith(("=", "+", "-", "@", "'"))


def _spreadsheet_cell(value):
    value = "" if value is None else str(value)
    return "'" + value if _csv_needs_quote(value) else value


def format_file(rows, file_format):
    if file_format == "json":
        return json.dumps({"teams": rows}, ensure_ascii=False, indent=2) + "\n"
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(COLUMNS)
    for team in rows:
        details = [_spreadsheet_cell(team.get(key)) for key in COLUMNS[:-1]]
        for index, name in enumerate(team["players"] or [""]):
            cells = details if index == 0 else [details[0], *([""] * (len(details) - 1))]
            writer.writerow([*cells, _spreadsheet_cell(name)])
    # A BOM lets spreadsheet applications detect UTF-8 names on open.
    return "\ufeff" + output.getvalue()


def _csv_rows(text):
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    header = next((line for line in reader if any(cell.strip() for cell in line)), [])
    fields = [re.sub(r"[\s-]+", "_", cell.strip().lower()) for cell in header]
    fields = [CSV_ALIASES.get(field, field) for field in fields]
    if "team_name" not in fields or len(set(fields)) != len(fields) or set(fields) - set(COLUMNS):
        raise ValueError("Use the CSV template headers. The team_name column is required.")

    groups = {}
    for line in reader:
        if not any(cell.strip() for cell in line):
            continue
        if len(line) > len(fields):
            raise ValueError(f"CSV line {reader.line_num} has extra fields. Quote names that contain a comma.")
        values = {}
        for key, value in zip(fields, line):
            if value.startswith("'") and _csv_needs_quote(value[1:]):
                value = value[1:]
            if value.strip():
                values[key] = value.strip()
        name = values.pop("team_name", "")
        if not name:
            raise ValueError(f"CSV line {reader.line_num} needs a team name.")
        player = values.pop("player_name", "")
        group = groups.setdefault(name.casefold(), {"team_name": name, "players": []})
        for key, value in values.items():
            if key in group and group[key] != value:
                raise ValueError(f"CSV line {reader.line_num} has conflicting {key} values for {name}.")
            group[key] = value
        if player:
            group["players"].append(player)
    return list(groups.values())


def read_file(raw, file_format):
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("The file exceeds 2 MB. Split it into smaller files and try again.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Save the file as UTF-8 and try again.") from exc
    if not text.strip():
        raise ValueError("The file is empty. Add your teams to a template and try again.")
    if "\x00" in text:
        raise ValueError("The file contains invalid characters. Save it as UTF-8 and try again.")
    try:
        rows = _csv_rows(text) if file_format == "csv" else challenge_team_io.read_teams_json(text)
    except csv.Error as exc:
        raise ValueError(f"Could not read the CSV. Check the commas and quotes. {exc}.") from exc
    except ValueError as exc:
        if file_format == "json":
            raise ValueError("Use the JSON template with a team object or a list of teams.") from exc
        raise
    except RecursionError as exc:
        raise ValueError("The JSON is too deeply nested. Use the JSON template structure.") from exc
    if not rows:
        raise ValueError("The file has no teams to import.")
    return rows


def championship_teams(db, championship_id):
    from models import ChallengeTeam, T20ChampionshipTeam

    return (db.query(T20ChampionshipTeam).filter_by(championship_id=championship_id)
            .options(joinedload(T20ChampionshipTeam.team).selectinload(ChallengeTeam.players))
            .order_by(T20ChampionshipTeam.id).all())


def export_teams(entries):
    rows = challenge_team_io.export_teams(entry.team for entry in entries)
    for row, entry in zip(rows, entries):
        players = sorted(entry.team.players, key=lambda member: (member.sort_order, member.id))
        row.update(owner=str(entry.owner_id),
                   co_owner=str(entry.co_owner_id) if entry.co_owner_id else "",
                   players=[member.name for member in players])
    return rows


def captain_id(db, value, label, *, optional=False):
    from models import User

    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"Enter a Telegram ID or @username for the {label}.")
    value = str(value).strip()
    if not value and optional:
        return None
    if re.fullmatch(r"@[a-zA-Z0-9_]{1,32}", value):
        user = db.query(User).filter(func.lower(User.username) == value[1:].lower()).first()
        if user:
            return user.telegram_id
        raise ValueError(f"{value} must use /debut before you can add them by username.")
    if value.isdecimal() and len(value) <= 16 and int(value) > 0:
        return int(value)
    raise ValueError(f"Enter a valid Telegram ID or @username for the {label}.")


def _highest_card(db, name):
    from services.version_paginator import find_players_for_search
    from services.version_service import get_all_versions

    matches = find_players_for_search(db, name)
    if len(matches) != 1:
        detail = "use the full player name" if matches else "no matching player"
        raise ValueError(f"{name} · {detail}.")
    editions = get_all_versions(db, matches[0].id)
    active = [p for p in editions if p.is_active and not p.is_career]
    if not active:
        raise ValueError(f"{name} has no active player card.")
    return max(active, key=lambda card: (card.rating, card.parent_player_id is not None, card.id))


def _squad_changes(team, names, resolve):
    current = {p.name.casefold() for p in team.players} if team else set()
    seen = set()
    cards, errors = [], []
    for name in names:
        name = name.strip()
        key = name.casefold()
        if not key or key in current or key in seen:
            continue
        seen.add(key)
        if len(name) > 150:
            errors.append(f"{name[:75]}… exceeds the 150 character player-name limit.")
            continue
        try:
            card = resolve(name)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        key = card.name.casefold()
        if key not in current:
            current.add(key)
            cards.append(card)
    return cards, errors


def _append_players(team, cards):
    from models import ChallengePlayer

    home = (team.league.home_country or "").strip().lower()
    order = max((p.sort_order for p in team.players), default=0)
    for index, source in enumerate(cards, start=order + 1):
        overseas = bool(home and (source.country or "").strip().lower() != home)
        team.players.append(ChallengePlayer(
            source_player_id=source.id, name=source.name, sort_order=index, is_overseas=overseas,
            details_json=challenge_details_json(source, is_overseas=overseas,
                                                source_player_id=source.id)))


def add_players_by_name(db, team, names):
    cards, errors = _squad_changes(team, names, lambda name: _highest_card(db, name))
    if errors:
        raise ValueError(errors[0])
    _append_players(team, cards)


def import_teams(db, championship, rows):
    """Validate the file together, then merge by team name without deleting squad members."""
    from models import ChallengeTeam, T20ChampionshipTeam

    if championship.started_at:
        raise ValueError("Import squads before the championship starts.")
    entries = championship_teams(db, championship.id)
    existing = {entry.name.strip().casefold(): entry for entry in entries}

    @lru_cache(maxsize=None)
    def resolve(name):
        return _highest_card(db, name)

    seen = set()
    plans, errors = [], []
    for number, row in enumerate(rows, 1):
        label = f"Team {number}"
        try:
            values = challenge_team_io.team_values(row)
            name = values.pop("name", None)
            if not name:
                raise ValueError("A team name is required.")
            label = name
            key = name.casefold()
            if key in seen:
                raise ValueError("Include one squad per team in JSON.")
            seen.add(key)

            logo = values.get("logo_url")
            if logo:
                path = unquote(urlsplit(logo).path)
                if ("\\" in path or (logo.startswith("/") and
                        (not path.startswith("/static/") or ".." in path.split("/")))):
                    raise ValueError("Use a public HTTP(S) logo URL or a logo under /static/.")

            entry = existing.get(key)
            owner = row.get("owner", row.get("owner_id"))
            co_owner = row.get("co_owner", row.get("co_owner_id"))
            owner = owner.strip() if isinstance(owner, str) else owner
            co_owner = co_owner.strip() if isinstance(co_owner, str) else co_owner
            owner_id = (entry.owner_id if entry and owner in (None, "") else
                        captain_id(db, owner, "owner"))
            co_owner_id = (entry.co_owner_id if entry and co_owner in (None, "") else
                           captain_id(db, co_owner if co_owner is not None else "", "co-owner", optional=True))
            if owner_id == co_owner_id:
                raise ValueError("Use different IDs for the owner and co-owner.")
            names = row.get("players", [])
            if not isinstance(names, list) or any(not isinstance(p, str) for p in names):
                raise ValueError("Players must be a list of names.")
            team = entry.team if entry else None
            cards, player_errors = _squad_changes(team, names, resolve)
            errors.extend(f"{label} · {message}" for message in player_errors)
            plans.append((name, entry, values, owner_id, co_owner_id, cards))
        except ValueError as exc:
            errors.append(f"{label} · {exc}")

    if errors:
        raise SquadImportError(errors)

    created = updated = added = 0
    for name, entry, values, owner_id, co_owner_id, cards in plans:
        if entry is None:
            team = ChallengeTeam(league=championship.league, name=name, **values)
            db.add(T20ChampionshipTeam(championship=championship, team=team,
                                       owner_id=owner_id, co_owner_id=co_owner_id))
            created += 1
        else:
            team = entry.team
            if (cards or entry.owner_id != owner_id or entry.co_owner_id != co_owner_id
                    or any(getattr(team, key) != value for key, value in values.items())):
                updated += 1
            entry.owner_id, entry.co_owner_id = owner_id, co_owner_id
            for key, value in values.items():
                setattr(team, key, value)
        _append_players(team, cards)
        added += len(cards)
    return created, updated, added

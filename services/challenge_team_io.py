"""Simple, partial JSON imports and exports for league team details."""

import json
import re
from urllib.parse import urlsplit

from services.card_identity import normalise_hex


_ALIASES = {
    "teamname": "name", "name": "name",
    "shortname": "short_name",
    "logourl": "logo_url", "imageurl": "logo_url",
    "logo": "logo_url", "image": "logo_url", "logoimage": "logo_url",
}
for _field in ("primary", "secondary"):
    for _spelling in ("color", "colour"):
        for _suffix in ("", "code"):
            _ALIASES[f"{_field}{_spelling}{_suffix}"] = f"{_field}_color"


def read_teams_json(text):
    """Accept a team array, a single team, or an object containing ``teams``."""
    try:
        payload = json.loads(text.lstrip("\ufeff"))
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid JSON. Upload a JSON file or paste the example below.") from exc
    if isinstance(payload, dict):
        payload = payload.get("teams", [payload])
    if not isinstance(payload, list):
        raise ValueError("JSON must contain a team object or an array of teams.")
    return payload


def export_teams(teams):
    return [{
        "team_name": team.name,
        "short_name": team.short_name,
        "logo_url": team.logo_url,
        "primary_color": team.primary_color,
        "secondary_color": team.secondary_color,
    } for team in teams]


def team_values(row):
    if not isinstance(row, dict):
        raise ValueError("each team must be a JSON object")
    values = {}
    limits = {"name": 120, "short_name": 30, "logo_url": 500}
    for key, raw in row.items():
        field = _ALIASES.get(re.sub(r"[^a-z]", "", key.lower()))
        if not field or raw is None or raw == "":
            continue
        if not isinstance(raw, str):
            raise ValueError(f"{key} must be text")
        value = raw.strip()
        if not value:
            continue
        if field.endswith("_color"):
            value = normalise_hex(value)
            if not value:
                raise ValueError(f"{key} must be a hex colour such as #1d4ed8 or #abc")
        elif len(value) > limits[field]:
            raise ValueError(f"{key} must be at most {limits[field]} characters")
        if field == "logo_url":
            url = urlsplit(value)
            if not ((url.scheme in {"http", "https"} and url.netloc)
                    or (value.startswith("/") and not value.startswith("//"))):
                raise ValueError("logo/image must be an http(s) URL or a local /path")
        values[field] = value
    if not values:
        raise ValueError("no team details supplied")
    return values


def import_teams(db, league, rows):
    """Create or update league teams, preserving any omitted/blank fields."""
    from models import ChallengeTeam

    teams = db.query(ChallengeTeam).filter_by(league_id=league.id).all()
    by_name = {team.name.strip().casefold(): team for team in teams}
    next_sort = max((team.sort_order for team in teams), default=0)
    created = updated = 0
    errors = []
    for number, row in enumerate(rows, 1):
        try:
            values = team_values(row)
            name = values.get("name")
            team = by_name.get(name.casefold()) if name else None
            if not name and values.get("short_name"):
                short = values["short_name"].casefold()
                matches = [t for t in teams if (t.short_name or "").casefold() == short]
                if len(matches) > 1:
                    raise ValueError("short name matches several teams; include the team name")
                team = matches[0] if matches else by_name.get(short)
                name = values["short_name"]
            if not name:
                index = 1
                while f"team {index}" in by_name:
                    index += 1
                name = f"Team {index}"
            if team is None:
                next_sort += 1
                team = ChallengeTeam(league_id=league.id, name=name,
                                     sort_order=next_sort, is_active=True)
                db.add(team)
                teams.append(team)
                by_name[name.casefold()] = team
                created += 1
            else:
                # A case-insensitive match should keep its existing name.
                values.pop("name", None)
                updated += 1
            for field, value in values.items():
                setattr(team, field, value)
        except ValueError as exc:
            errors.append(f"Team {number}: {exc}")
    db.flush()
    return created, updated, errors

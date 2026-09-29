"""Build playable XIs from ``data/players.json`` (for the CLI and tests)."""

import json
import os

from engine.sim.match import Team

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAYERS_PATH = os.path.join(_ROOT, "data", "players.json")


def _to_engine(row, idx):
    return {
        "roster_id": f"{row.get('Country', 'X')[:3]}{idx}",
        "name": row.get("Player Name", f"Player {idx}"),
        "category": row.get("Category", "Batsman"),
        "bat_rating": int(row.get("Batting Rating") or 50),
        "bowl_rating": int(row.get("Bowling Rating") or 20),
        "rating": int(row.get("overall all") or 50),
        "bowl_style": row.get("Bowling Style", ""),
        "bat_hand": "Left" if "left" in str(row.get("Batting Style", "")).lower() else "Right",
    }


def load_rows(path=None):
    with open(path or PLAYERS_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def pick_xi(country, rows=None):
    """A balanced XI: 6 batters (incl. keeper/all-rounders), 5 bowlers (>=1 spinner)."""
    from engine.sim.config import get_config
    from engine.sim.player_adapter import is_spinner

    cfg = get_config()
    rows = [r for r in (rows or load_rows()) if r.get("Country") == country]
    if not rows:
        raise ValueError(f"no players for country {country!r} in data/players.json")
    seen, uniq = set(), []
    for r in sorted(rows, key=lambda r: -(r.get("overall all") or 0)):
        if r["Player Name"] in seen:
            continue
        seen.add(r["Player Name"])
        uniq.append(r)
    bowlers = [r for r in uniq if (r.get("Category") or "").lower() == "bowler"]
    spinners = [r for r in bowlers if is_spinner(r.get("Bowling Style"), cfg)]
    pacers = [r for r in bowlers if r not in spinners]
    attack = pacers[:3] + spinners[:1]
    attack += [r for r in bowlers if r not in attack][:5 - len(attack)]
    bats = [r for r in uniq if r not in bowlers]
    bats = sorted(bats, key=lambda r: -(r.get("Batting Rating") or 0))[:11 - len(attack)]
    xi = sorted(bats, key=lambda r: -(r.get("Batting Rating") or 0)) + \
        sorted(attack, key=lambda r: -(r.get("Batting Rating") or 0))
    return Team(name=country, players=[_to_engine(r, i) for i, r in enumerate(xi)],
                short=country[:3].upper())


def load_team_file(path):
    """A team from a JSON file: ``{"name": ..., "players": [engine player dicts]}``."""
    with open(path, "r", encoding="utf-8") as fh:
        d = json.load(fh)
    return Team(name=d.get("name", "Team"), players=d["players"], short=d.get("short", ""))

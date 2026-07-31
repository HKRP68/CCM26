"""Bot Team service — admin-managed teams users can play against in /vsbot.

Provides CRUD operations and helpers for picking AI-controlled deliveries/shots.
"""

import logging
from datetime import datetime
from sqlalchemy.exc import SQLAlchemyError

from models import BotTeam, BotTeamPlayer, Player
from services.player_service import not_career

logger = logging.getLogger(__name__)


def list_active_teams(session):
    """All active bot teams with their roster size."""
    teams = (session.query(BotTeam)
             .filter(BotTeam.is_active == True)
             .order_by(BotTeam.name).all())
    out = []
    for t in teams:
        count = (session.query(BotTeamPlayer)
                 .filter(BotTeamPlayer.bot_team_id == t.id).count())
        out.append((t, count))
    return out


def get_team_with_players(session, team_id):
    """Returns (team, [(BotTeamPlayer, Player), ...]) sorted by batting_order."""
    team = session.query(BotTeam).get(team_id)
    if not team:
        return None, []
    rows = (session.query(BotTeamPlayer, Player)
            .join(Player, BotTeamPlayer.player_id == Player.id)
            .filter(BotTeamPlayer.bot_team_id == team_id)
            .order_by(BotTeamPlayer.batting_order).all())
    return team, rows


def validate_team_xi(session, team_id):
    """Check team has the same XI rules as users.
    Returns (ok, list[err_strings])."""
    team, rows = get_team_with_players(session, team_id)
    if not team:
        return False, ["Team not found"]
    if len(rows) != 11:
        return False, [f"Team has {len(rows)} players, need exactly 11"]

    cats = [p.category for _, p in rows]
    counts = {
        "Batsman": cats.count("Batsman"),
        "Bowler": cats.count("Bowler"),
        "Wicket Keeper": cats.count("Wicket Keeper"),
        "All-rounder": cats.count("All-rounder"),
    }
    errs = []
    if counts["Batsman"] < 3 or counts["Batsman"] > 5:
        errs.append(f"3-5 Batsmen required (have {counts['Batsman']})")
    if counts["Bowler"] < 3 or counts["Bowler"] > 5:
        errs.append(f"3-5 Bowlers required (have {counts['Bowler']})")
    if counts["Wicket Keeper"] < 1 or counts["Wicket Keeper"] > 2:
        errs.append(f"1-2 Wicket Keepers required (have {counts['Wicket Keeper']})")
    if counts["All-rounder"] < 1 or counts["All-rounder"] > 3:
        errs.append(f"1-3 All-rounders required (have {counts['All-rounder']})")

    return len(errs) == 0, errs


def create_team(session, name, description="", difficulty="Medium"):
    if not name or not name.strip():
        return None, "Name cannot be empty"
    existing = session.query(BotTeam).filter(BotTeam.name == name.strip()).first()
    if existing:
        return None, f"Team '{name}' already exists"
    t = BotTeam(name=name.strip(), description=(description or "").strip(),
                difficulty=difficulty, is_active=True)
    session.add(t)
    session.flush()
    return t, None


def update_team(session, team_id, name=None, description=None,
                difficulty=None, is_active=None):
    t = session.query(BotTeam).get(team_id)
    if not t:
        return None, "Team not found"
    if name is not None:
        n = name.strip()
        if not n:
            return None, "Name cannot be empty"
        # Conflict check
        clash = session.query(BotTeam).filter(BotTeam.name == n,
                                              BotTeam.id != team_id).first()
        if clash:
            return None, f"Team '{n}' already exists"
        t.name = n
    if description is not None:
        t.description = description.strip()
    if difficulty is not None:
        t.difficulty = difficulty
    if is_active is not None:
        t.is_active = is_active
    return t, None


def delete_team(session, team_id):
    t = session.query(BotTeam).get(team_id)
    if not t:
        return False, "Team not found"
    session.query(BotTeamPlayer).filter(BotTeamPlayer.bot_team_id == team_id).delete()
    session.delete(t)
    return True, None


def add_player_to_team(session, team_id, player_id):
    """Append a player at the next batting position."""
    team = session.query(BotTeam).get(team_id)
    if not team:
        return None, "Team not found"
    player = session.query(Player).get(player_id)
    if not player:
        return None, "Player not found"
    if getattr(player, "is_career", False):
        return None, "Career Players belong to one user and can't join a bot team"

    existing = (session.query(BotTeamPlayer)
                .filter(BotTeamPlayer.bot_team_id == team_id,
                        BotTeamPlayer.player_id == player_id).first())
    if existing:
        return None, f"{player.name} already in team"

    count = (session.query(BotTeamPlayer)
             .filter(BotTeamPlayer.bot_team_id == team_id).count())
    if count >= 25:
        return None, "Team already has 25 players (max)"

    btp = BotTeamPlayer(
        bot_team_id=team_id, player_id=player_id,
        batting_order=count + 1, is_captain=False,
    )
    session.add(btp)
    session.flush()
    return btp, None


def remove_player_from_team(session, team_id, player_id):
    btp = (session.query(BotTeamPlayer)
           .filter(BotTeamPlayer.bot_team_id == team_id,
                   BotTeamPlayer.player_id == player_id).first())
    if not btp:
        return False, "Player not in this team"
    session.delete(btp)
    session.flush()
    # Renumber batting orders
    _renumber_batting_order(session, team_id)
    return True, None


def _renumber_batting_order(session, team_id):
    rows = (session.query(BotTeamPlayer)
            .filter(BotTeamPlayer.bot_team_id == team_id)
            .order_by(BotTeamPlayer.batting_order, BotTeamPlayer.id).all())
    for i, r in enumerate(rows, 1):
        if r.batting_order != i:
            r.batting_order = i


def reorder_player(session, team_id, player_id, new_position):
    """Move a player to a new batting position. Other players shift accordingly."""
    btp = (session.query(BotTeamPlayer)
           .filter(BotTeamPlayer.bot_team_id == team_id,
                   BotTeamPlayer.player_id == player_id).first())
    if not btp:
        return False, "Player not in this team"

    rows = (session.query(BotTeamPlayer)
            .filter(BotTeamPlayer.bot_team_id == team_id)
            .order_by(BotTeamPlayer.batting_order).all())

    new_position = max(1, min(len(rows), new_position))
    # Remove from current position, reinsert at new position
    rows = [r for r in rows if r.id != btp.id]
    rows.insert(new_position - 1, btp)
    for i, r in enumerate(rows, 1):
        r.batting_order = i
    return True, None


def set_captain(session, team_id, player_id):
    rows = (session.query(BotTeamPlayer)
            .filter(BotTeamPlayer.bot_team_id == team_id).all())
    target = None
    for r in rows:
        if r.player_id == player_id:
            target = r
            r.is_captain = True
        else:
            r.is_captain = False
    if not target:
        return False, "Player not in this team"
    return True, None


def bulk_add_players(session, team_id, player_names_or_ids):
    """Bulk-add players by name or id, one per line.
    Returns (added_count, skipped_list)."""
    team = session.query(BotTeam).get(team_id)
    if not team:
        return 0, ["Team not found"]
    added = 0
    skipped = []
    current_count = (session.query(BotTeamPlayer)
                     .filter(BotTeamPlayer.bot_team_id == team_id).count())
    for raw in player_names_or_ids:
        name = raw.strip()
        if not name:
            continue
        if current_count >= 25:
            skipped.append(f"{name} (team full)")
            continue
        # Try ID first, then name
        player = None
        if name.isdigit():
            player = session.query(Player).get(int(name))
        if not player:
            player = (not_career(session.query(Player))
                      .filter(Player.name.ilike(name)).first())
        if not player:
            player = (not_career(session.query(Player))
                      .filter(Player.name.ilike(f"%{name}%")).first())
        if not player:
            skipped.append(f"{name} (not found)")
            continue

        existing = (session.query(BotTeamPlayer)
                    .filter(BotTeamPlayer.bot_team_id == team_id,
                            BotTeamPlayer.player_id == player.id).first())
        if existing:
            skipped.append(f"{name} (duplicate)")
            continue

        current_count += 1
        btp = BotTeamPlayer(
            bot_team_id=team_id, player_id=player.id,
            batting_order=current_count, is_captain=False,
        )
        session.add(btp)
        added += 1
    session.flush()
    return added, skipped


def team_summary(session, team_id):
    """Stats summary for a team — avg rating, total OVR, captain name."""
    team, rows = get_team_with_players(session, team_id)
    if not team:
        return None
    if not rows:
        return {
            "team": team, "players": 0, "avg_rating": 0,
            "total_ovr": 0, "captain": None, "valid": False, "errors": [],
        }
    ratings = [p.rating for _, p in rows]
    captain_row = next((r for r in rows if r[0].is_captain), None)
    captain_name = captain_row[1].name if captain_row else None
    valid, errs = (True, []) if len(rows) != 11 else validate_team_xi(session, team_id)
    return {
        "team": team,
        "players": len(rows),
        "avg_rating": round(sum(ratings) / len(ratings), 1),
        "total_ovr": sum(ratings),
        "captain": captain_name,
        "valid": valid,
        "errors": errs,
    }

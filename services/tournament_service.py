"""Tournament service — N-participant points-table tournaments.

Unlike Tour (fixed 2-player challenge series), any number of users in a chat
can join a Tournament. Matches are attached one of two ways:

  1. Automatic (primary path): the match-start command was run with the
     `Tournament` keyword (e.g. `/cipl Tournament`, `/letsplay Tournament`).
     This resolves the chat's single active tournament at command time and
     stores it on `Match.tournament_id`. At match finalization, the existing
     non-fatal stats hooks call `attach_match_if_tagged()`, which reads that
     column back and attaches the match — no guessing, no ambiguity.
  2. Manual fallback (`/addtotournament`, or the admin panel "add match by
     Match No"): for modes not wired to the keyword, or chats with 2+
     simultaneously active tournaments where the keyword alone is ambiguous.

Joining is automatic: attaching a match auto-creates `TournamentTeam` rows
for both participants if they aren't already members.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, or_

from models import Tournament, TournamentTeam, TournamentMatch, Match, User, PlayerMatchStats, Player

logger = logging.getLogger(__name__)


def create_tournament(session, name, creator_user_id, chat_id=None,
                       points_win=2, points_loss=0, points_tie=1):
    """Create an open tournament and auto-join the creator.

    Returns (tournament, error_message).
    """
    if not name or not name.strip():
        return None, "Tournament needs a name."

    tournament = Tournament(
        name=name.strip(), chat_id=chat_id, created_by_user_id=creator_user_id,
        status="open", points_win=points_win, points_loss=points_loss,
        points_tie=points_tie, created_at=datetime.utcnow(),
    )
    session.add(tournament)
    session.flush()
    join_tournament(session, tournament.id, creator_user_id)
    return tournament, None


def join_tournament(session, tournament_id, user_id):
    """Idempotent TournamentTeam creation. Returns (team, error_message)."""
    tournament = session.query(Tournament).get(tournament_id)
    if not tournament:
        return None, "Tournament not found."
    if tournament.status == "completed":
        return None, "This tournament has already completed."

    team = (session.query(TournamentTeam)
            .filter(TournamentTeam.tournament_id == tournament_id,
                    TournamentTeam.user_id == user_id)
            .first())
    if team:
        return team, None

    team = TournamentTeam(tournament_id=tournament_id, user_id=user_id,
                           joined_at=datetime.utcnow())
    session.add(team)
    session.flush()
    return team, None


def get_tournament(session, tournament_id):
    return session.query(Tournament).get(tournament_id)


def get_open_tournaments(session, chat_id=None):
    q = session.query(Tournament).filter(Tournament.status.in_(["open", "active"]))
    if chat_id is not None:
        q = q.filter(Tournament.chat_id == chat_id)
    return q.order_by(Tournament.created_at.desc()).all()


def is_participant(session, tournament_id, user_id):
    return (session.query(TournamentTeam)
            .filter(TournamentTeam.tournament_id == tournament_id,
                    TournamentTeam.user_id == user_id)
            .first() is not None)


def _apply_match_to_points_table(session, tournament_id, match):
    """Update both sides' played/won/lost/tied/points for one attached match."""
    tournament = session.query(Tournament).get(tournament_id)
    if not tournament:
        return

    for uid in (match.user1_id, match.user2_id):
        team = (session.query(TournamentTeam)
                .filter(TournamentTeam.tournament_id == tournament_id,
                        TournamentTeam.user_id == uid)
                .first())
        if not team:
            continue
        team.played = (team.played or 0) + 1
        if match.winner_id == uid:
            team.won = (team.won or 0) + 1
            team.points = (team.points or 0) + tournament.points_win
        elif match.winner_id and match.winner_id != uid:
            team.lost = (team.lost or 0) + 1
            team.points = (team.points or 0) + tournament.points_loss
        else:
            team.tied = (team.tied or 0) + 1
            team.points = (team.points or 0) + tournament.points_tie


def attach_match(session, tournament_id, match_id, added_by_user_id=None):
    """Attach a completed match to a tournament. Returns (tournament_match, error_message)."""
    tournament = session.query(Tournament).get(tournament_id)
    if not tournament:
        return None, "Tournament not found."
    if tournament.status == "completed":
        return None, "This tournament has already completed."

    match = session.query(Match).get(match_id)
    if not match:
        return None, "Match not found."
    if match.status != "completed":
        return None, "Match hasn't finished yet."

    existing = session.query(TournamentMatch).filter(TournamentMatch.match_id == match_id).first()
    if existing:
        if existing.tournament_id == tournament_id:
            return existing, None
        return None, "This match is already attached to another tournament."

    join_tournament(session, tournament_id, match.user1_id)
    join_tournament(session, tournament_id, match.user2_id)

    tm = TournamentMatch(tournament_id=tournament_id, match_id=match_id,
                          added_by_user_id=added_by_user_id, added_at=datetime.utcnow())
    session.add(tm)

    if tournament.status == "open":
        tournament.status = "active"

    _apply_match_to_points_table(session, tournament_id, match)

    session.flush()
    return tm, None


def get_points_table(session, tournament_id):
    """Return TournamentTeam rows sorted by points desc, then wins desc."""
    return (session.query(TournamentTeam)
            .filter(TournamentTeam.tournament_id == tournament_id)
            .order_by(TournamentTeam.points.desc(), TournamentTeam.won.desc())
            .all())


def extract_tournament_intent(args):
    """Strip the literal "tournament" token out of a command's args list.

    Returns (cleaned_args, wants_tournament). Case-insensitive, so the keyword
    doesn't interfere with existing @username/reply parsing at call sites.
    """
    cleaned = []
    wants_tournament = False
    for a in (args or []):
        if isinstance(a, str) and a.strip().lower() == "tournament":
            wants_tournament = True
        else:
            cleaned.append(a)
    return cleaned, wants_tournament


def resolve_active_tournament_for_chat(session, chat_id):
    """Resolve the single open/active tournament for a chat.

    Returns (tournament, error_message). Errors when there are zero or 2+
    candidates so the player gets immediate feedback instead of the match
    silently not counting.
    """
    candidates = get_open_tournaments(session, chat_id=chat_id)
    if not candidates:
        return None, ("❌ No active tournament here — create one with "
                       "<code>/newtournament &lt;name&gt;</code>.")
    if len(candidates) > 1:
        return None, ("❌ This chat has multiple active tournaments — use "
                       "/addtotournament after the match to pick one, or "
                       "attach it from the admin panel.")
    return candidates[0], None


def attach_match_if_tagged(session, match_id):
    """Called from match-finalization hooks. No-op unless the match was
    tagged with a tournament_id at creation time (the `Tournament` keyword)."""
    match = session.query(Match).get(match_id)
    if not match or not match.tournament_id:
        return None, None
    return attach_match(session, match.tournament_id, match_id)


def get_user_recent_match_id(session, user_id, chat_id, window_minutes=120):
    """Find the user's most recent completed, unattached match in this chat.

    Powers the manual /addtotournament fallback for matches started without
    the keyword, or via modes that don't support it yet.
    """
    cutoff = datetime.utcnow() - timedelta(minutes=window_minutes)
    already_attached = {mid for (mid,) in session.query(TournamentMatch.match_id).all()}

    match = (session.query(Match)
             .filter(or_(Match.user1_id == user_id, Match.user2_id == user_id),
                     Match.chat_id == chat_id,
                     Match.status == "completed",
                     Match.completed_at >= cutoff)
             .order_by(Match.completed_at.desc())
             .first())
    if not match or match.id in already_attached:
        return None
    return match.id


def complete_tournament(session, tournament_id):
    tournament = session.query(Tournament).get(tournament_id)
    if not tournament:
        return None
    tournament.status = "completed"
    tournament.completed_at = datetime.utcnow()
    session.flush()
    return tournament


# ════════════════════════════════════════════════════════════════════
# Tournament leaderboard — 8 categories across all attached matches
# ════════════════════════════════════════════════════════════════════

def get_tournament_leaderboard(session, tournament_id, top_n=3, qualify_balls=12):
    """Aggregate player stats across all matches attached to a tournament.

    Returns dict with most_runs, most_wickets, most_sixes, most_fours
    (sums), highest_score, best_bowling (single-match max), best_strike_rate,
    best_economy (ratio aggregates with a minimum-balls qualifier).
    """
    match_ids = [
        mid for (mid,) in (session.query(TournamentMatch.match_id)
                           .filter(TournamentMatch.tournament_id == tournament_id)
                           .all())
    ]
    empty = {
        "most_runs": [], "most_wickets": [], "most_sixes": [], "most_fours": [],
        "highest_score": [], "best_bowling": [], "best_strike_rate": [],
        "best_economy": [], "total_matches_played": 0,
    }
    if not match_ids:
        return empty

    def _resolve_sum(rows):
        out = []
        for player_id, user_id, value in rows:
            if not value:
                continue
            player = session.query(Player).get(player_id)
            user = session.query(User).get(user_id)
            if not player or not user:
                continue
            out.append({
                "player_name": player.name, "value": int(value),
                "team_user_id": user.id,
                "team_name": user.team_name or f"@{user.username}'s XI",
                "username": user.username,
            })
        return out

    def _sum_category(column):
        rows = (session.query(
                    PlayerMatchStats.player_id, PlayerMatchStats.user_id,
                    func.sum(column))
                .filter(PlayerMatchStats.match_id.in_(match_ids))
                .group_by(PlayerMatchStats.player_id, PlayerMatchStats.user_id)
                .order_by(func.sum(column).desc())
                .limit(top_n)
                .all())
        return _resolve_sum(rows)

    most_runs = _sum_category(PlayerMatchStats.bat_runs)
    most_wickets = _sum_category(PlayerMatchStats.bowl_wickets)
    most_sixes = _sum_category(PlayerMatchStats.bat_sixes)
    most_fours = _sum_category(PlayerMatchStats.bat_fours)

    highest_rows = (session.query(
                        PlayerMatchStats.player_id, PlayerMatchStats.user_id,
                        PlayerMatchStats.bat_runs)
                    .filter(PlayerMatchStats.match_id.in_(match_ids))
                    .order_by(PlayerMatchStats.bat_runs.desc())
                    .limit(top_n)
                    .all())
    highest_score = _resolve_sum(highest_rows)

    bowling_rows = (session.query(
                        PlayerMatchStats.player_id, PlayerMatchStats.user_id,
                        PlayerMatchStats.bowl_wickets, PlayerMatchStats.bowl_runs)
                    .filter(PlayerMatchStats.match_id.in_(match_ids))
                    .order_by(PlayerMatchStats.bowl_wickets.desc(), PlayerMatchStats.bowl_runs.asc())
                    .limit(top_n)
                    .all())
    best_bowling = []
    for player_id, user_id, wickets, runs in bowling_rows:
        if not wickets:
            continue
        player = session.query(Player).get(player_id)
        user = session.query(User).get(user_id)
        if not player or not user:
            continue
        best_bowling.append({
            "player_name": player.name, "value": f"{wickets}/{runs}",
            "team_user_id": user.id,
            "team_name": user.team_name or f"@{user.username}'s XI",
            "username": user.username,
        })

    sr_rows = (session.query(
                    PlayerMatchStats.player_id, PlayerMatchStats.user_id,
                    func.sum(PlayerMatchStats.bat_runs), func.sum(PlayerMatchStats.bat_balls))
              .filter(PlayerMatchStats.match_id.in_(match_ids))
              .group_by(PlayerMatchStats.player_id, PlayerMatchStats.user_id)
              .having(func.sum(PlayerMatchStats.bat_balls) >= qualify_balls)
              .all())
    best_strike_rate = []
    for player_id, user_id, runs, balls in sr_rows:
        if not balls:
            continue
        player = session.query(Player).get(player_id)
        user = session.query(User).get(user_id)
        if not player or not user:
            continue
        best_strike_rate.append({
            "player_name": player.name, "value": round(runs * 100 / balls, 2),
            "team_user_id": user.id,
            "team_name": user.team_name or f"@{user.username}'s XI",
            "username": user.username,
        })
    best_strike_rate.sort(key=lambda d: d["value"], reverse=True)
    best_strike_rate = best_strike_rate[:top_n]

    econ_rows = (session.query(
                    PlayerMatchStats.player_id, PlayerMatchStats.user_id,
                    func.sum(PlayerMatchStats.bowl_runs), func.sum(PlayerMatchStats.bowl_balls))
                .filter(PlayerMatchStats.match_id.in_(match_ids))
                .group_by(PlayerMatchStats.player_id, PlayerMatchStats.user_id)
                .having(func.sum(PlayerMatchStats.bowl_balls) >= qualify_balls)
                .all())
    best_economy = []
    for player_id, user_id, runs, balls in econ_rows:
        if not balls:
            continue
        player = session.query(Player).get(player_id)
        user = session.query(User).get(user_id)
        if not player or not user:
            continue
        best_economy.append({
            "player_name": player.name, "value": round(runs * 6 / balls, 2),
            "team_user_id": user.id,
            "team_name": user.team_name or f"@{user.username}'s XI",
            "username": user.username,
        })
    best_economy.sort(key=lambda d: d["value"])
    best_economy = best_economy[:top_n]

    return {
        "most_runs": most_runs, "most_wickets": most_wickets,
        "most_sixes": most_sixes, "most_fours": most_fours,
        "highest_score": highest_score, "best_bowling": best_bowling,
        "best_strike_rate": best_strike_rate, "best_economy": best_economy,
        "total_matches_played": len(match_ids),
    }

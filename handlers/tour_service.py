"""Tour service — orchestrates multi-match challenges between two users.

Lifecycle:
  /cmtours @user2 → 3-step pickers (matches, overs) → create Tour (pending)
  user2 accepts within 30s → status='active', TourMatch rows created
  Players play matches one at a time via /mytours
  Each completed Match calls record_match_result() → updates Tour score
  Tour ends when all matches done (status='completed') or lifetime expires
"""

import logging
import random
from datetime import datetime, timedelta
from sqlalchemy import or_, func

from models import Tour, TourMatch, Match, User
from services.match_constants import STADIUMS, PITCH_TYPES

logger = logging.getLogger(__name__)

INVITE_EXPIRE_SECONDS = 30
DAYS_PER_MATCH = 2  # tour lifetime = match_count * DAYS_PER_MATCH days
ALLOWED_MATCH_COUNTS = [3, 4, 5, 6]
MIN_OVERS = 5
MAX_OVERS = 20


def is_user_in_active_tour(session, user_id):
    """Return the user's active or pending tour (Tour|None).

    Active = currently playing matches; pending = invite sent, not yet accepted.
    Both states block the user from starting another tour.
    """
    return (session.query(Tour)
            .filter(or_(Tour.user1_id == user_id, Tour.user2_id == user_id),
                    Tour.status.in_(["pending", "active"]))
            .first())


def create_tour(session, user1_id, user2_id, match_count, overs_per_match, chat_id=None):
    """Create a pending tour. Caller is responsible for sending the invite message.

    Returns (tour, error_message). On success error_message is None.
    """
    if match_count not in ALLOWED_MATCH_COUNTS:
        return None, f"Match count must be one of {ALLOWED_MATCH_COUNTS}."
    if overs_per_match < MIN_OVERS or overs_per_match > MAX_OVERS:
        return None, f"Overs must be between {MIN_OVERS} and {MAX_OVERS}."
    if user1_id == user2_id:
        return None, "You can't tour yourself."

    # Block if either user is already in a tour
    existing1 = is_user_in_active_tour(session, user1_id)
    if existing1:
        return None, "You're already in a tour. Finish that one first."
    existing2 = is_user_in_active_tour(session, user2_id)
    if existing2:
        return None, "Your opponent is already in a tour."

    now = datetime.utcnow()
    tour = Tour(
        user1_id=user1_id, user2_id=user2_id,
        chat_id=chat_id,
        match_count=match_count,
        overs_per_match=overs_per_match,
        status="pending",
        created_at=now,
        expires_at=now + timedelta(seconds=INVITE_EXPIRE_SECONDS),
        lifetime_expires_at=now + timedelta(days=match_count * DAYS_PER_MATCH),
        user1_wins=0, user2_wins=0,
    )
    session.add(tour)
    session.flush()
    return tour, None


def accept_tour(session, tour_id, accepter_user_id):
    """User2 accepts; create the TourMatch slots with pre-decided venues.

    Returns (tour, error_message).
    """
    tour = session.query(Tour).get(tour_id)
    if not tour:
        return None, "Tour not found."
    if tour.status != "pending":
        return None, "This tour is no longer pending."
    if tour.user2_id != accepter_user_id:
        return None, "Only the invited user can accept."
    if datetime.utcnow() > tour.expires_at:
        tour.status = "expired"
        session.flush()
        return None, "Invite expired."

    # Set status + create match slots, each with its own stadium+pitch
    now = datetime.utcnow()
    tour.status = "active"
    tour.accepted_at = now
    # Refresh lifetime now (so 2-day-per-match runs from acceptance)
    tour.lifetime_expires_at = now + timedelta(days=tour.match_count * DAYS_PER_MATCH)

    for i in range(1, tour.match_count + 1):
        tm = TourMatch(
            tour_id=tour.id,
            match_number=i,
            stadium=random.choice(STADIUMS),
            pitch_type=random.choice(PITCH_TYPES),
            status="pending",
        )
        session.add(tm)
    session.flush()
    return tour, None


def decline_tour(session, tour_id, decliner_user_id):
    tour = session.query(Tour).get(tour_id)
    if not tour:
        return None, "Tour not found."
    if tour.user2_id != decliner_user_id:
        return None, "Only the invited user can decline."
    if tour.status != "pending":
        return None, "Tour is no longer pending."
    tour.status = "declined"
    session.flush()
    return tour, None


def expire_pending_invite(session, tour_id):
    """Called by the 30s timer. Marks pending tour as expired (no-op if not pending)."""
    tour = session.query(Tour).get(tour_id)
    if tour and tour.status == "pending":
        tour.status = "expired"
        session.flush()
        return tour
    return None


def get_user_tours(session, user_id, *, include_finished=True):
    """Returns user's tours sorted by most recent first.

    If include_finished is False, only active/pending tours.
    """
    q = (session.query(Tour)
         .filter(or_(Tour.user1_id == user_id, Tour.user2_id == user_id))
         .order_by(Tour.created_at.desc()))
    if not include_finished:
        q = q.filter(Tour.status.in_(["pending", "active"]))
    return q.all()


def get_tour_matches(session, tour_id):
    """Return ordered list of TourMatch rows for a tour."""
    return (session.query(TourMatch)
            .filter(TourMatch.tour_id == tour_id)
            .order_by(TourMatch.match_number)
            .all())


def link_match_to_tour(session, tour_match_id, match_id):
    """Called when the user starts Match N: attach the actual Match record."""
    tm = session.query(TourMatch).get(tour_match_id)
    if not tm:
        return None
    tm.match_id = match_id
    tm.status = "playing"
    session.flush()
    return tm


def record_match_result(session, match_id, winner_user_id, *, forfeit=False):
    """Called by the match-end hook in handlers/match.py.

    If this match is part of a tour, update the TourMatch + Tour score,
    and finalize the tour if all matches are done.

    Returns the Tour if this match belongs to one, else None.
    """
    tm = (session.query(TourMatch)
          .filter(TourMatch.match_id == match_id).first())
    if not tm:
        return None  # standalone match, not in a tour

    tour = session.query(Tour).get(tm.tour_id)
    if not tour:
        return None
    if tour.status != "active":
        # Already wrapped up — don't double-count
        return tour

    tm.winner_id = winner_user_id
    tm.completed_at = datetime.utcnow()
    tm.status = "forfeit" if forfeit else "done"

    # Increment tour-level score
    if winner_user_id == tour.user1_id:
        tour.user1_wins = (tour.user1_wins or 0) + 1
    elif winner_user_id == tour.user2_id:
        tour.user2_wins = (tour.user2_wins or 0) + 1

    # Flush the TourMatch status change so the count query below sees it
    session.flush()

    # Check if all matches are done — if so finalize
    remaining = (session.query(func.count(TourMatch.id))
                 .filter(TourMatch.tour_id == tour.id,
                         TourMatch.status.in_(["pending", "playing"]))
                 .scalar())
    if remaining == 0:
        tour.status = "completed"
        tour.completed_at = datetime.utcnow()
        tour.winner_id = _decide_winner(tour)

    session.flush()
    return tour


def _decide_winner(tour):
    """Return winner user_id, or None for a tie."""
    if tour.user1_wins > tour.user2_wins: return tour.user1_id
    if tour.user2_wins > tour.user1_wins: return tour.user2_id
    return None  # tied


def expire_old_tours(session):
    """Job: mark any active tours whose lifetime has run out.

    Lifetime expiry doesn't auto-forfeit remaining matches — it just stamps
    the tour as completed and decides the winner based on matches finished so far.

    Returns count of tours expired.
    """
    now = datetime.utcnow()
    overdue = (session.query(Tour)
               .filter(Tour.status == "active",
                       Tour.lifetime_expires_at < now)
               .all())
    count = 0
    for tour in overdue:
        tour.status = "completed"  # Even if some matches unplayed, decide based on score
        tour.completed_at = now
        tour.winner_id = _decide_winner(tour)
        # Mark unplayed TourMatches as expired so they don't appear as playable
        (session.query(TourMatch)
         .filter(TourMatch.tour_id == tour.id,
                 TourMatch.status.in_(["pending", "playing"]))
         .update({"status": "expired"}, synchronize_session=False))
        count += 1
    if count:
        session.flush()
    return count


def expire_overdue_invites(session):
    """Job: pending tours past 30s without accept."""
    now = datetime.utcnow()
    overdue = (session.query(Tour)
               .filter(Tour.status == "pending",
                       Tour.expires_at < now)
               .all())
    for tour in overdue:
        tour.status = "expired"
    if overdue:
        session.flush()
    return len(overdue)


# ════════════════════════════════════════════════════════════════════
# Tour leaderboard — most runs / wickets across tour matches
# ════════════════════════════════════════════════════════════════════

def get_tour_stats(session, tour_id, top_n=3):
    """Aggregate player stats across all completed matches of a tour.

    Returns dict:
      most_runs: [(player_name, runs, team_user_id, team_name), ...]
      most_wickets: [(player_name, wickets, team_user_id, team_name), ...]
      total_matches_played: int
      total_runs: int (across both teams)
      total_wickets: int
    """
    from models import PlayerMatchStats, Player

    # Collect all match_ids for this tour
    match_ids = [
        mid for (mid,) in (session.query(TourMatch.match_id)
                           .filter(TourMatch.tour_id == tour_id,
                                   TourMatch.match_id.isnot(None),
                                   TourMatch.status.in_(["done", "forfeit"]))
                           .all())
        if mid is not None
    ]
    if not match_ids:
        return {
            "most_runs": [], "most_wickets": [],
            "total_matches_played": 0, "total_runs": 0, "total_wickets": 0,
        }

    # Aggregate runs per player
    runs_rows = (session.query(
        PlayerMatchStats.player_id,
        PlayerMatchStats.user_id,
        func.sum(PlayerMatchStats.bat_runs).label("total_runs"),
    )
        .filter(PlayerMatchStats.match_id.in_(match_ids))
        .group_by(PlayerMatchStats.player_id, PlayerMatchStats.user_id)
        .order_by(func.sum(PlayerMatchStats.bat_runs).desc())
        .limit(top_n)
        .all())

    wickets_rows = (session.query(
        PlayerMatchStats.player_id,
        PlayerMatchStats.user_id,
        func.sum(PlayerMatchStats.bowl_wickets).label("total_wkts"),
    )
        .filter(PlayerMatchStats.match_id.in_(match_ids))
        .group_by(PlayerMatchStats.player_id, PlayerMatchStats.user_id)
        .order_by(func.sum(PlayerMatchStats.bowl_wickets).desc())
        .limit(top_n)
        .all())

    # Resolve player+team names
    def _resolve(rows, value_key):
        out = []
        for r in rows:
            player = session.query(Player).get(r[0])
            user = session.query(User).get(r[1])
            if not player or not user:
                continue
            value = int(r[2] or 0)
            if value == 0:
                continue  # Skip players who didn't contribute
            team_name = user.team_name or f"@{user.username}'s XI"
            out.append({
                "player_name": player.name,
                "value": value,
                "team_user_id": user.id,
                "team_name": team_name,
                "username": user.username,
            })
        return out

    # Totals across the tour
    totals = (session.query(
        func.sum(PlayerMatchStats.bat_runs),
        func.sum(PlayerMatchStats.bowl_wickets),
    )
        .filter(PlayerMatchStats.match_id.in_(match_ids))
        .first())
    total_runs = int(totals[0] or 0)
    total_wickets = int(totals[1] or 0)

    return {
        "most_runs": _resolve(runs_rows, "total_runs"),
        "most_wickets": _resolve(wickets_rows, "total_wkts"),
        "total_matches_played": len(match_ids),
        "total_runs": total_runs,
        "total_wickets": total_wickets,
    }

"""Challenge League Tour service — best-of series between two users using fixed
Challenge-League teams.

Lifecycle:
  /cltour @guest → host picks league/team, guest picks team, host picks count
                   → create CLTour (pending)
  guest accepts within INVITE_EXPIRE_SECONDS → status='active', CLTourMatch rows
  Players play matches one at a time via /cltour (sequential "Play Match N")
  Each completed Match calls record_cl_match_result() → updates series score
  Series ends when a side clinches (or all matches done) → status='completed'
"""

import logging
from datetime import datetime, timedelta
from sqlalchemy import or_, func

from models import CLTour, CLTourMatch, ChallengeLeague, ChallengeTeam

logger = logging.getLogger(__name__)

INVITE_EXPIRE_SECONDS = 60
ALLOWED_MATCH_COUNTS = [3, 5, 7]


def is_user_in_active_cl_tour(session, user_id):
    """Return the user's active or *live* pending CL tour (CLTour|None).

    Active tours always block. A pending invite blocks only while it hasn't
    timed out — an overdue pending invite (its JobQueue expiry was lost on a
    restart, say) is treated as not blocking so users can never be locked out of
    starting a new tour. This self-heals without needing a startup sweep.
    """
    now = datetime.utcnow()
    candidates = (session.query(CLTour)
                  .filter(or_(CLTour.user1_id == user_id, CLTour.user2_id == user_id),
                          CLTour.status.in_(["pending", "active"]))
                  .all())
    for tour in candidates:
        if tour.status == "active":
            return tour
        # pending: only blocks if the invite window is still open
        if tour.expires_at is None or tour.expires_at > now:
            return tour
    return None


def create_cl_tour(session, host_id, guest_id, league_id, host_team_id,
                   guest_team_id, match_count, chat_id=None):
    """Create a pending CL tour. Caller sends the invite/agreement message.

    Returns (tour, error_message). On success error_message is None.
    """
    if match_count not in ALLOWED_MATCH_COUNTS:
        return None, f"Match count must be one of {ALLOWED_MATCH_COUNTS}."
    if host_id == guest_id:
        return None, "You can't start a tour with yourself."

    league = session.query(ChallengeLeague).get(league_id)
    if not league:
        return None, "League not found."
    host_team = session.query(ChallengeTeam).get(host_team_id)
    guest_team = session.query(ChallengeTeam).get(guest_team_id)
    if not host_team or host_team.league_id != league_id:
        return None, "Host team is not in this league."
    if not guest_team or guest_team.league_id != league_id:
        return None, "Guest team is not in this league."
    if host_team_id == guest_team_id and not league.same_team_allowed:
        return None, "Both players can't pick the same team in this league."

    if is_user_in_active_cl_tour(session, host_id):
        return None, "You're already in a CL tour. Finish that one first."
    if is_user_in_active_cl_tour(session, guest_id):
        return None, "Your opponent is already in a CL tour."

    now = datetime.utcnow()
    tour = CLTour(
        user1_id=host_id, user2_id=guest_id,
        chat_id=chat_id,
        league_id=league_id,
        host_team_id=host_team_id,
        guest_team_id=guest_team_id,
        match_count=match_count,
        status="pending",
        created_at=now,
        expires_at=now + timedelta(seconds=INVITE_EXPIRE_SECONDS),
        user1_wins=0, user2_wins=0,
    )
    session.add(tour)
    session.flush()
    return tour, None


def accept_cl_tour(session, tour_id, accepter_user_id):
    """Guest accepts; create the CLTourMatch slots. Returns (tour, error)."""
    tour = session.query(CLTour).get(tour_id)
    if not tour:
        return None, "Tour not found."
    if tour.status != "pending":
        return None, "This tour is no longer pending."
    if tour.user2_id != accepter_user_id:
        return None, "Only the invited user can accept."
    if tour.expires_at and datetime.utcnow() > tour.expires_at:
        tour.status = "expired"
        session.flush()
        return None, "Invite expired."

    now = datetime.utcnow()
    tour.status = "active"
    tour.accepted_at = now
    for i in range(1, tour.match_count + 1):
        session.add(CLTourMatch(
            cl_tour_id=tour.id,
            match_number=i,
            status="pending",
        ))
    session.flush()
    return tour, None


def decline_cl_tour(session, tour_id, decliner_user_id):
    tour = session.query(CLTour).get(tour_id)
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
    """Called by the invite timer. Marks pending tour expired (no-op otherwise)."""
    tour = session.query(CLTour).get(tour_id)
    if tour and tour.status == "pending":
        tour.status = "expired"
        session.flush()
        return tour
    return None


def get_user_cl_tours(session, user_id, *, include_finished=True):
    """User's CL tours, most recent first."""
    q = (session.query(CLTour)
         .filter(or_(CLTour.user1_id == user_id, CLTour.user2_id == user_id))
         .order_by(CLTour.created_at.desc()))
    if not include_finished:
        q = q.filter(CLTour.status.in_(["pending", "active"]))
    return q.all()


def get_active_cl_tour(session, user_id):
    """The user's single active CL tour, or None."""
    return (session.query(CLTour)
            .filter(or_(CLTour.user1_id == user_id, CLTour.user2_id == user_id),
                    CLTour.status == "active")
            .order_by(CLTour.created_at.desc())
            .first())


def get_cl_tour_matches(session, tour_id):
    """Ordered CLTourMatch rows for a tour."""
    return (session.query(CLTourMatch)
            .filter(CLTourMatch.cl_tour_id == tour_id)
            .order_by(CLTourMatch.match_number)
            .all())


def next_pending_match(session, tour_id):
    """The next sequential match still to be played, or None.

    Returns None while any match in the series is already ``playing`` — a
    best-of series runs strictly one match at a time. Otherwise two matches
    could be launched concurrently, and a result that clinches the series could
    mark an already-live later slot ``done`` while its real Match keeps running.
    """
    rows = (session.query(CLTourMatch)
            .filter(CLTourMatch.cl_tour_id == tour_id)
            .order_by(CLTourMatch.match_number)
            .all())
    if any(tm.status == "playing" for tm in rows):
        return None
    return next((tm for tm in rows if tm.status == "pending"), None)


def link_match_to_cl_tour(session, cl_tour_match_id, match_id):
    """When a tour match's /cipl match launches: attach the Match + mark playing."""
    tm = session.query(CLTourMatch).get(cl_tour_match_id)
    if not tm:
        return None
    tm.match_id = match_id
    tm.status = "playing"
    session.flush()
    return tm


def unlink_match_from_cl_tour(session, cl_tour_match_id):
    """Hand a series slot back when its /cipl match never became playable.

    The mirror of ``link_match_to_cl_tour``: a launch that committed a Match row
    and then died before the board appeared would otherwise leave the slot stuck
    on ``playing`` forever — and ``next_pending_match`` refuses to open the next
    slot while any slot is playing, so the whole tour would deadlock on a match
    that was never bowled. Only reverts a slot that is still ``playing``, so a
    finished result is never rewound. Caller commits.
    """
    tm = session.query(CLTourMatch).get(cl_tour_match_id)
    if not tm or tm.status != "playing":
        return None
    tm.match_id = None
    tm.status = "pending"
    session.flush()
    return tm


def record_cl_match_result(session, cl_tour_match_id, winner_user_id):
    """Called from the /cipl match-end hook for tour matches.

    Marks the CLTourMatch done, increments the series score, and finalizes the
    tour once a side clinches (or all matches are played). A tie reaching here
    (i.e. not resolved by a Super Over) leaves the match replayable.

    Returns the CLTour, or None.
    """
    tm = session.query(CLTourMatch).get(cl_tour_match_id)
    if not tm:
        return None
    if tm.status == "done":
        return session.query(CLTour).get(tm.cl_tour_id)  # don't double-count

    tour = session.query(CLTour).get(tm.cl_tour_id)
    if not tour or tour.status != "active":
        return tour

    if winner_user_id is None:
        # Tie not resolved by a Super Over: the slot was marked 'playing' at
        # launch, so reset it to a clean pending state or next_pending_match()
        # never offers it again and the series stalls.
        tm.match_id = None
        tm.winner_id = None
        tm.completed_at = None
        tm.status = "pending"
        session.flush()
        return tour

    tm.winner_id = winner_user_id
    tm.completed_at = datetime.utcnow()
    tm.status = "done"

    if winner_user_id == tour.user1_id:
        tour.user1_wins = (tour.user1_wins or 0) + 1
    elif winner_user_id == tour.user2_id:
        tour.user2_wins = (tour.user2_wins or 0) + 1

    session.flush()

    # Finalize when a side clinches the majority, or no matches remain.
    clinch = tour.match_count // 2 + 1
    remaining = (session.query(func.count(CLTourMatch.id))
                 .filter(CLTourMatch.cl_tour_id == tour.id,
                         CLTourMatch.status.in_(["pending", "playing"]))
                 .scalar())
    if (tour.user1_wins or 0) >= clinch or (tour.user2_wins or 0) >= clinch or remaining == 0:
        tour.status = "completed"
        tour.completed_at = datetime.utcnow()
        tour.winner_id = _decide_winner(tour)
        # Any unplayed matches become moot once the series is decided.
        (session.query(CLTourMatch)
         .filter(CLTourMatch.cl_tour_id == tour.id,
                 CLTourMatch.status.in_(["pending", "playing"]))
         .update({"status": "done"}, synchronize_session=False))

    session.flush()
    return tour


def _decide_winner(tour):
    """Winner user_id, or None for a tie."""
    if (tour.user1_wins or 0) > (tour.user2_wins or 0):
        return tour.user1_id
    if (tour.user2_wins or 0) > (tour.user1_wins or 0):
        return tour.user2_id
    return None


def series_leader(tour):
    """(leader_user_id|None, is_tie) for display purposes."""
    w = _decide_winner(tour)
    return w, w is None
